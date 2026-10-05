"""Zoho on or off, decided once, and only in api.py (spec 2026-10-05,
sections 1 and 9).

Every misconfiguration leaves the mock in place, exactly as when Zoho is off,
and records nothing in `tickets`. The worker is built at import and started
only by the lifespan. The CLI, the live evaluation and the playground keep the
mock even with every Zoho setting present. Nothing here reaches Zoho: the
opener given to the wiring records each call and refuses it.
"""

import asyncio
import importlib
import io
import logging
import os
import pathlib
import threading
import unittest
import urllib.error
from contextlib import nullcontext, redirect_stdout
from types import SimpleNamespace
from unittest import mock

import mongomock

from emotorad_ai import cli, wiring
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore, StoreUnavailable
from emotorad_ai.live_eval.runner import scenario_registry
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.mongo import ensure_indexes
from emotorad_ai.tickets.clock import now_iso
from emotorad_ai.tickets.kinds import is_desk_reference
from emotorad_ai.tickets.record import new_record
from emotorad_ai.tickets.seam import TicketRouter
from emotorad_ai.tools.mocks import MockTicketSystem, build_registry
from emotorad_ai.zoho.settings import ENV_NAMES, STORE_UNREACHABLE
from emotorad_ai.zoho.wiring import ZohoWiring, build_zoho, ticket_health, zoho_status
from emotorad_ai.zoho.worker import THREAD_NAME, ZohoWorker
from tests.live_eval_helpers import TODAY as EVAL_TODAY
from tests.live_eval_helpers import scenario
from tests.test_chat_page_new_bot import load_launcher
from tests.test_runtime_persistence import TODAY, runtime_on, send

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Everything else api.py reads at import, blanked so this machine's own
# environment cannot change what is tested.
BASE = {
    "EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": "", "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "",
    "EMOTORAD_AMIGO_PG_DSN": "", "EMOTORAD_AI_MEDIA_BUCKET": "", "EMOTORAD_OMS_API_KEY": "",
    "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb",
}


def blank_zoho():
    """Every Zoho name blank, including any on this machine that settings.py does not know."""
    names = set(ENV_NAMES) | {name for name in os.environ if name.startswith("EMOTORAD_ZOHO_")}
    return {name: "" for name in names}


def zoho_env(live=False, **changes):
    """Every Zoho setting set to a fake value, in the test department unless `live`."""
    env = blank_zoho()
    env.update({
        "EMOTORAD_ZOHO_REFRESH_TOKEN": "refresh-test", "EMOTORAD_ZOHO_CLIENT_ID": "client-test",
        "EMOTORAD_ZOHO_CLIENT_SECRET": "secret-test", "EMOTORAD_ZOHO_ORG_ID": "org-test",
        "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID": "dept-test", "EMOTORAD_ZOHO_TEST_CONTACT_ID": "contact-test",
        "EMOTORAD_ZOHO_DEPARTMENT_ID": "dept-real", "EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID": "contact-unverified",
        "EMOTORAD_ZOHO_LIVE": "yes" if live else "no",
        "EMOTORAD_ZOHO_PRIORITY_HIGH": "High", "EMOTORAD_ZOHO_PRIORITY_MEDIUM": "Medium",
        "EMOTORAD_ZOHO_CHANNEL": "Chat", "EMOTORAD_ZOHO_CREDITS_FLOOR": "1000",
        "EMOTORAD_ZOHO_ATTACHMENT_LIMIT_MB": "20",
        "EMOTORAD_AI_ENV": "stage", "AWS_REGION": "ap-south-1", "EMOTORAD_AI_DEV_CODES": "",
    })
    env.update(changes)
    return env


def mongo_stores(indexed=True):
    client = mongomock.MongoClient()
    if indexed:
        ensure_indexes(client[Settings().mongo_db])
    return wiring.build_stores(Settings(store="mongodb"), client=client)


def memory_stores():
    return wiring.build_stores(Settings(store="memory"))


def refusing_opener(calls):
    def opener(request, timeout=None):
        calls.append(getattr(request, "full_url", request))
        raise urllib.error.URLError("no network in tests")

    return opener


def wire(env, stores, store_kind="mongodb", otp_is_mock=False, log=None, calls=None):
    return build_zoho(env, ticket_store=stores.tickets, store_kind=store_kind, conversations=stores.conversations,
                      media_reader=None, log=log if log is not None else EventLog(path=None),
                      otp_is_mock=otp_is_mock, opener=refusing_opener(calls if calls is not None else []))


def nothing_recorded(store):
    return store.listing("test") == [] and store.listing("live") == []


def zoho_threads():
    return [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]


def a_record(store, mode="test"):
    reference, now = store.next_reference(), now_iso()
    return store.insert(new_record(
        reference=reference, chat_reference="stage:" + reference, source_key="conv-1:%s:handover:%s" % (now, reference),
        mode=mode, kind="support", conversation_id="conv-1", started_at=now, cluster_id=None, channel="web",
        phone="+919999999999", identity="verified", category="battery_charging", ai_severity="normal",
        summary="Charger LED stays off.", claims={}, bike=None, coverage=None, customer_name=None, created_at=now,
    ))["_id"]


def reload_api(env, client=None):
    """emotorad_ai.api imported again with `env`, with its stores on mongomock
    when a client is given. The patch covers the first import as well, which
    happens here when this module runs on its own."""
    real = wiring.build_stores

    def on_mongomock(settings, log=None):
        return real(settings, log=log, client=client)

    on_mock = mock.patch.object(wiring, "build_stores", on_mongomock) if client is not None else nullcontext()
    # The import warns that no guide picture can be sent without a bucket.
    quiet = mock.patch.object(logging.getLogger("emotorad_ai.api"), "disabled", True)
    with mock.patch.dict(os.environ, env, clear=False), on_mock, quiet:
        import emotorad_ai.api as api

        return importlib.reload(api)


class OffTests(unittest.TestCase):
    def test_without_the_refresh_token_zoho_is_off_and_nothing_is_alarmed(self):
        log = EventLog(path=None)
        result = wire(dict(zoho_env(), EMOTORAD_ZOHO_REFRESH_TOKEN=""), mongo_stores(), log=log)
        self.assertEqual((result.status, result.router, result.worker), ("not configured", None, None))
        self.assertEqual([e for e in log.events if e["event"] == "zoho_misconfigured"], [])


class MisconfiguredTests(unittest.TestCase):
    CASES = (
        ("misconfigured: missing EMOTORAD_ZOHO_ORG_ID", {"EMOTORAD_ZOHO_ORG_ID": ""}, {}),
        ("misconfigured: missing EMOTORAD_AI_ENV", {"EMOTORAD_AI_ENV": ""}, {}),
        ("misconfigured: bad number: EMOTORAD_ZOHO_CREDITS_FLOOR", {"EMOTORAD_ZOHO_CREDITS_FLOOR": "lots"}, {}),
        ("not allowed in this region", {"AWS_REGION": "eu-central-1"}, {}),
        ("misconfigured: store is not mongodb", {}, {"store": "memory"}),
        ("misconfigured: tickets index missing", {}, {"indexed": False}),
        ("misconfigured: live refused: test verification in use",
         {"EMOTORAD_ZOHO_LIVE": "yes", "EMOTORAD_AI_DEV_CODES": "1"}, {}),
        ("misconfigured: live refused: test verification in use", {"EMOTORAD_ZOHO_LIVE": "yes"},
         {"otp_is_mock": True}),
    )

    def test_each_reason_falls_back_to_the_mock_and_records_nothing(self):
        for reason, env_changes, how in self.CASES:
            with self.subTest(reason=reason, how=how):
                memory = how.get("store") == "memory"
                stores = memory_stores() if memory else mongo_stores(indexed=how.get("indexed", True))
                log, calls = EventLog(path=None), []
                result = wire(zoho_env(**env_changes), stores, store_kind="memory" if memory else "mongodb",
                              otp_is_mock=how.get("otp_is_mock", False), log=log, calls=calls)
                self.assertEqual((result.status, result.router, result.worker), (reason, None, None))
                self.assertEqual([e["reason"] for e in log.events if e["event"] == "zoho_misconfigured"], [reason])
                self.assertEqual([e["level"] for e in log.events if e["event"] == "zoho_misconfigured"], ["error"])
                # A signed-in customer's safety report still gets its ticket,
                # from the mock, and nothing reaches the tickets collection.
                registry = build_registry(today=TODAY, ticket_system=result.router)
                runtime = runtime_on(InMemoryConversationStore(), [], registry=registry)
                answer = send(runtime, "my battery is swollen")
                self.assertIs(type(registry.tickets), MockTicketSystem)
                self.assertTrue(answer.ticket_id)
                self.assertFalse(is_desk_reference(answer.ticket_id))
                self.assertEqual(runtime.llm.requests, [])
                self.assertTrue(nothing_recorded(stores.tickets))
                self.assertEqual(calls, [])

    def test_a_reason_names_settings_never_their_values(self):
        log = EventLog(path=None)
        wire(zoho_env(EMOTORAD_ZOHO_ORG_ID=""), mongo_stores(), log=log)
        logged = repr(log.events)
        for value in ("refresh-test", "client-test", "secret-test"):
            self.assertNotIn(value, logged)

    def test_a_store_that_cannot_be_read_at_start_keeps_the_mock(self):
        def unreadable():
            raise StoreUnavailable("MongoDB index_information failed (ServerSelectionTimeoutError)")

        log = EventLog(path=None)
        result = build_zoho(zoho_env(), ticket_store=SimpleNamespace(has_unique_source_key=unreadable),
                            store_kind="mongodb", conversations=InMemoryConversationStore(), media_reader=None,
                            log=log, otp_is_mock=False, opener=refusing_opener([]))
        self.assertEqual((result.status, result.router, result.worker),
                         ("misconfigured: store unreachable", None, None))
        self.assertEqual(result.status, STORE_UNREACHABLE)
        self.assertEqual([e["reason"] for e in log.events if e["event"] == "zoho_misconfigured"],
                         ["misconfigured: store unreachable"])

    def test_stores_with_no_tickets_store_keep_the_mock_and_do_not_crash(self):
        # Stores built without one (tests/test_audit_edges.py does) have
        # nowhere to keep a record, so this counts as not mongodb.
        log = EventLog(path=None)
        result = build_zoho(zoho_env(), ticket_store=None, store_kind="mongodb",
                            conversations=InMemoryConversationStore(), media_reader=None, log=log,
                            otp_is_mock=False, opener=refusing_opener([]))
        self.assertEqual((result.status, result.router, result.worker, result.store),
                         ("misconfigured: store is not mongodb", None, None, None))
        self.assertEqual(ticket_health(result, now_iso()), {})


class OnTests(unittest.TestCase):
    def test_with_everything_in_place_customers_get_desk_and_the_worker_waits(self):
        calls, stores = [], mongo_stores()
        result = wire(zoho_env(), stores, calls=calls)
        self.assertEqual(result.status, "test department")
        self.assertIsInstance(result.router, TicketRouter)
        self.assertIsInstance(result.worker, ZohoWorker)
        self.assertIs(result.store, stores.tickets)
        self.assertIs(result.router.store, stores.tickets)
        self.assertEqual(result.router.desk.mode, "test")
        self.assertEqual(result.router.desk.environment, "stage")
        self.assertFalse(result.worker.status["running"])
        self.assertEqual(zoho_threads(), [])
        self.assertEqual(calls, [])

    def test_live_needs_real_verification_and_then_says_live(self):
        result = wire(zoho_env(live=True), mongo_stores(), otp_is_mock=False)
        self.assertEqual(result.status, "live")
        self.assertEqual(result.worker.settings.mode, "live")
        self.assertEqual(result.router.desk.mode, "live")

    def test_zoho_is_never_called_inside_a_turn(self):
        calls, stores = [], mongo_stores()
        result = wire(zoho_env(), stores, calls=calls)
        registry = build_registry(today=TODAY, ticket_system=result.router)
        runtime = runtime_on(InMemoryConversationStore(), [], registry=registry)
        answer = send(runtime, "my battery is swollen")
        self.assertTrue(is_desk_reference(answer.ticket_id))
        self.assertIsNotNone(stores.tickets.get(answer.ticket_id))
        self.assertEqual(runtime.llm.requests, [])
        self.assertEqual(calls, [])
        self.assertEqual(zoho_threads(), [])

    def test_the_end_of_a_turn_wakes_the_worker(self):
        # A recorded ticket is first sent after the reply, not at the next pass.
        with mock.patch.object(ZohoWorker, "wake") as wake:
            result = wire(zoho_env(), mongo_stores())
        registry = build_registry(today=TODAY, ticket_system=result.router)
        runtime = runtime_on(InMemoryConversationStore(), [], registry=registry)
        wake.assert_not_called()
        send(runtime, "my battery is swollen")
        wake.assert_called()
        self.assertEqual(zoho_threads(), [])


class HealthFieldTests(unittest.TestCase):
    def test_a_failure_the_worker_met_replaces_the_start_up_status(self):
        result = wire(zoho_env(), mongo_stores())
        self.assertEqual(zoho_status(result), "test department")
        result.worker.status["failing"] = "token refused: invalid_client_secret"
        self.assertEqual(zoho_status(result), "token refused: invalid_client_secret")
        result.worker.status["failing"] = None
        self.assertEqual(zoho_status(result), "test department")

    def test_with_zoho_off_the_counts_appear_only_when_records_are_outstanding(self):
        stores = memory_stores()
        off = ZohoWiring(status="not configured", store=stores.tickets)
        self.assertEqual(ticket_health(off, now_iso()), {})
        a_record(stores.tickets)
        report = ticket_health(off, now_iso())
        self.assertEqual((report["tickets_waiting"], report["tickets_stuck"], report["tickets_held"]), (1, 0, 0))
        self.assertEqual(report["zoho_worker"], "off")
        self.assertIn("oldest_due_seconds", report)

    def test_with_zoho_off_a_live_record_still_counts_as_held(self):
        stores = memory_stores()
        a_record(stores.tickets, mode="live")
        report = ticket_health(ZohoWiring(status="not configured", store=stores.tickets), now_iso())
        self.assertEqual((report["tickets_waiting"], report["tickets_held"]), (0, 1))

    def test_with_zoho_on_the_counts_and_the_worker_are_shown(self):
        result = wire(zoho_env(), mongo_stores())
        report = ticket_health(result, now_iso())
        self.assertEqual(report["tickets_waiting"], 0)
        self.assertEqual(report["zoho_worker"]["running"], False)

    def test_a_store_that_cannot_be_read_says_so_and_does_not_fail_health(self):
        def unreadable(mode, now):
            raise StoreUnavailable("MongoDB count_documents failed (ServerSelectionTimeoutError)")

        wiring_off = ZohoWiring(status="not configured", store=SimpleNamespace(counts=unreadable))
        self.assertEqual(ticket_health(wiring_off, now_iso()), {"tickets": "store unavailable"})


class ApiTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        reload_api(dict(BASE, EMOTORAD_STORE="memory", **blank_zoho()))

    def zoho_on_api(self, **changes):
        client = mongomock.MongoClient()
        ensure_indexes(client[Settings().mongo_db])
        return reload_api(dict(BASE, EMOTORAD_STORE="mongodb", **zoho_env(**changes)), client=client)

    def test_importing_and_reloading_the_api_starts_no_worker(self):
        self.zoho_on_api()
        api = self.zoho_on_api()
        self.assertIsInstance(api.ZOHO.worker, ZohoWorker)
        self.assertFalse(api.ZOHO.worker.status["running"])
        self.assertEqual(zoho_threads(), [])
        self.assertIsInstance(api.registry.tickets, TicketRouter)
        self.assertIs(api.registry.tickets.store, api.stores.tickets)
        report = api.health()
        self.assertEqual(report["zoho"], "test department")
        self.assertEqual(report["tickets_waiting"], 0)

    def test_with_the_oms_key_customers_still_get_desk(self):
        # The registry's other branch, taken with the live OMS key. Nothing is
        # looked up at import, so no request is made.
        api = self.zoho_on_api(EMOTORAD_OMS_API_KEY="oms-test")
        self.assertIsInstance(api.registry.tickets, TicketRouter)

    def test_health_counts_a_waiting_ticket(self):
        api = self.zoho_on_api()
        a_record(api.stores.tickets)
        self.assertEqual(api.health()["tickets_waiting"], 1)

    def test_only_the_lifespan_starts_and_stops_the_worker(self):
        api = self.zoho_on_api()
        seen = []

        async def serve():
            async with api._lifespan(api.app):
                seen.append((api.ZOHO.worker.status["running"], len(zoho_threads())))

        asyncio.run(serve())
        self.assertEqual(seen, [(True, 1)])
        self.assertFalse(api.ZOHO.worker.status["running"])
        self.assertEqual(zoho_threads(), [])

    def test_an_eu_region_keeps_the_mock_and_says_so(self):
        api = self.zoho_on_api(AWS_REGION="eu-central-1")
        self.assertIs(type(api.registry.tickets), MockTicketSystem)
        self.assertIsNone(api.ZOHO.worker)
        self.assertEqual(api.health()["zoho"], "not allowed in this region")
        self.assertTrue(nothing_recorded(api.stores.tickets))

    def test_with_zoho_off_the_lifespan_starts_nothing(self):
        api = reload_api(dict(BASE, EMOTORAD_STORE="memory", **blank_zoho()))
        self.assertIsNone(api.ZOHO.worker)
        self.assertIs(type(api.registry.tickets), MockTicketSystem)

        async def serve():
            async with api._lifespan(api.app):
                self.assertEqual(zoho_threads(), [])

        asyncio.run(serve())
        self.assertEqual(api.health()["zoho"], "not configured")


class EntryPointTests(unittest.TestCase):
    """api.py is the only place that chooses Zoho (spec section 9).
    docker/start.py passes its whole environment to Streamlit, so the
    playground sees every Zoho setting too."""

    def test_the_cli_keeps_the_mock_with_every_zoho_setting_present(self):
        built, real = [], cli.build_registry

        def capture(**kwargs):
            registry = real(**kwargs)
            built.append(registry)
            return registry

        with mock.patch.dict(os.environ, zoho_env(), clear=False), \
                mock.patch.object(cli, "build_registry", capture), redirect_stdout(io.StringIO()):
            code = cli.main(["--offline", "--store", "memory", "--channel", "amiigo", "--session", "sess-amiigo-test",
                             "hi"])
        self.assertEqual(code, 0)
        [registry] = built
        self.assertIs(type(registry.tickets), MockTicketSystem)
        self.assertEqual(zoho_threads(), [])

    def test_the_live_evaluation_keeps_the_mock(self):
        with mock.patch.dict(os.environ, zoho_env(), clear=False):
            registry = scenario_registry(scenario(), EVAL_TODAY)
        self.assertIs(type(registry.tickets), MockTicketSystem)

    def test_a_default_registry_is_the_mock_whatever_the_environment(self):
        with mock.patch.dict(os.environ, zoho_env(), clear=False):
            self.assertIs(type(build_registry().tickets), MockTicketSystem)

    def test_no_entry_point_but_the_api_reaches_for_zoho(self):
        reaching = ("build_zoho", "TicketRouter", "DeskTicketSystem", "zoho.wiring", "from .zoho", "tickets.seam")
        package = ROOT / "src" / "emotorad_ai"
        entry_points = [package / "cli.py", package / "playground.py", *sorted((package / "live_eval").glob("*.py")),
                        ROOT / "scripts" / "live_eval.py", ROOT / "docker" / "start.py"]
        for path in entry_points:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual([name for name in reaching if name in text], [])
        self.assertIn("build_zoho", (package / "api.py").read_text(encoding="utf-8"))

    def test_the_local_chat_page_never_passes_a_zoho_setting(self):
        launcher = load_launcher()
        given = dict(zoho_env(), EMOTORAD_ZOHO_SOMETHING_NEW="x")
        env = launcher.server_env(given, mode="offline", store="memory")
        self.assertEqual([name for name in env if name.startswith("EMOTORAD_ZOHO_")], [])
        for name in ENV_NAMES:
            self.assertIn(name, launcher.WITHHELD)
        self.assertIn("EMOTORAD_OMS_API_KEY", launcher.WITHHELD)
        lines = "\n".join(launcher.status_lines(given, "offline", "memory"))
        self.assertIn("tickets: the mock, never Zoho Desk (EMOTORAD_ZOHO_* settings are set and are ignored here)",
                      lines)
        self.assertNotIn("refresh-test", lines)
        self.assertNotIn("secret-test", lines)

    def test_the_local_chat_page_says_nothing_is_ignored_without_zoho_settings(self):
        lines = load_launcher().status_lines(blank_zoho(), "offline", "memory")
        self.assertIn("tickets: the mock, never Zoho Desk", lines)


if __name__ == "__main__":
    unittest.main()
