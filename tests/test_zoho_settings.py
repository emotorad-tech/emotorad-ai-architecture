"""Zoho settings and the start-up checks (spec 2026-10-05, section 1).

Zoho is off unless the refresh token is set. Then either every name the mode
needs is present and every check passes, or the mock is used exactly as when
Zoho is off. No string built here may hold a value, only names: the status
goes to /health and to the log.
"""

import unittest
from dataclasses import fields

from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.zoho import settings as zs
from emotorad_ai.zoho.settings import ZohoSettings, load_zoho_settings, startup_problem

REFRESH = "1000.refresh-DO-NOT-LEAK"
CLIENT_ID = "1000.CLIENTID-DO-NOT-LEAK"
SECRET = "client-secret-DO-NOT-LEAK"
SECRETS = (REFRESH, CLIENT_ID, SECRET)

TEST_ENV = {
    "EMOTORAD_ZOHO_REFRESH_TOKEN": REFRESH,
    "EMOTORAD_ZOHO_CLIENT_ID": CLIENT_ID,
    "EMOTORAD_ZOHO_CLIENT_SECRET": SECRET,
    "EMOTORAD_ZOHO_ORG_ID": "60000000001",
    "EMOTORAD_ZOHO_TEST_DEPARTMENT_ID": "4000000000001",
    "EMOTORAD_ZOHO_TEST_CONTACT_ID": "4000000000002",
    "EMOTORAD_AI_ENV": "stage",
}
LIVE_ENV = dict(
    TEST_ENV,
    EMOTORAD_ZOHO_DEPARTMENT_ID="4000000000003",
    EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID="4000000000004",
    EMOTORAD_ZOHO_LIVE="yes",
)


def without(env, *names):
    return {name: value for name, value in env.items() if name not in names}


def loaded(env):
    settings, status = load_zoho_settings(env)
    assert settings is not None, status
    return settings


class FakeTicketStore:
    """Only what the start-up check reads."""

    def __init__(self, indexed=True, error=None):
        self.indexed = indexed
        self.error = error
        self.asked = 0

    def has_unique_source_key(self):
        self.asked += 1
        if self.error is not None:
            raise self.error
        return self.indexed


def problem(settings, **changes):
    arguments = dict(region="ap-south-1", store_kind="mongodb", ticket_store=FakeTicketStore(),
                     dev_codes=False, otp_is_mock=False)
    arguments.update(changes)
    return startup_problem(settings, **arguments)


class SwitchTests(unittest.TestCase):
    def test_without_the_refresh_token_zoho_is_not_configured_whatever_else_is_set(self):
        self.assertEqual(load_zoho_settings(without(LIVE_ENV, zs.REFRESH_TOKEN)), (None, "not configured"))

    def test_a_blank_refresh_token_is_the_same_as_none(self):
        for blank in ("", "   ", "\n"):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(load_zoho_settings(dict(TEST_ENV, **{zs.REFRESH_TOKEN: blank})),
                                 (None, "not configured"))

    def test_an_empty_environment_is_not_configured(self):
        self.assertEqual(load_zoho_settings({}), (None, "not configured"))


class TestModeTests(unittest.TestCase):
    def test_every_always_needed_name_gives_test_mode(self):
        settings, status = load_zoho_settings(TEST_ENV)
        self.assertEqual(status, "ok")
        self.assertIsInstance(settings, ZohoSettings)
        self.assertFalse(settings.live)
        self.assertEqual(settings.mode, "test")
        self.assertEqual(settings.active_department_id, "4000000000001")
        self.assertEqual(settings.test_contact_id, "4000000000002")
        self.assertIsNone(settings.department_id)
        self.assertIsNone(settings.unverified_contact_id)
        self.assertEqual(settings.environment, "stage")
        self.assertEqual(settings.org_id, "60000000001")
        self.assertEqual((settings.client_id, settings.client_secret, settings.refresh_token),
                         (CLIENT_ID, SECRET, REFRESH))

    def test_the_probe_only_values_have_their_defaults(self):
        settings = loaded(TEST_ENV)
        self.assertEqual(settings.priority_high, "High")
        self.assertEqual(settings.priority_medium, "Medium")
        self.assertEqual(settings.channel, "Chat")
        self.assertEqual(settings.credits_floor, 1000)
        self.assertEqual(settings.attachment_limit_bytes, 20 * 1024 * 1024)
        self.assertIsNone(settings.layout_id)

    def test_the_probe_only_values_can_be_set(self):
        settings = loaded(dict(TEST_ENV, **{
            zs.PRIORITY_HIGH: "P1", zs.PRIORITY_MEDIUM: "P3", zs.CHANNEL: "Web",
            zs.CREDITS_FLOOR: "2500", zs.ATTACHMENT_LIMIT_MB: "15", zs.LAYOUT_ID: "4000000000009",
        }))
        self.assertEqual((settings.priority_high, settings.priority_medium, settings.channel), ("P1", "P3", "Web"))
        self.assertEqual(settings.credits_floor, 2500)
        self.assertEqual(settings.attachment_limit_bytes, 15 * 1024 * 1024)
        self.assertEqual(settings.layout_id, "4000000000009")

    def test_values_are_read_without_surrounding_spaces(self):
        settings = loaded(dict(TEST_ENV, **{
            zs.ORG_ID: " 60000000001\n", zs.REFRESH_TOKEN: REFRESH + "\n", zs.LAYOUT_ID: " 4000000000009 ",
        }))
        self.assertEqual(settings.org_id, "60000000001")
        self.assertEqual(settings.refresh_token, REFRESH)
        self.assertEqual(settings.layout_id, "4000000000009")

    def test_a_real_department_without_live_stays_in_test_mode(self):
        settings, status = load_zoho_settings(without(LIVE_ENV, zs.LIVE))
        self.assertEqual(status, "ok")
        self.assertEqual(settings.mode, "test")
        self.assertEqual(settings.active_department_id, "4000000000001")

    def test_live_is_only_the_exact_word_yes(self):
        # Anything else is the test department: the safe way to read a mistake.
        for value in ("Yes", "YES", "true", "1", "y", "yes ", " yes", ""):
            with self.subTest(value=repr(value)):
                settings, status = load_zoho_settings(dict(LIVE_ENV, **{zs.LIVE: value}))
                self.assertEqual(status, "ok")
                self.assertFalse(settings.live)
                self.assertEqual(settings.mode, "test")


class LiveModeTests(unittest.TestCase):
    def test_live_sends_to_the_real_department(self):
        settings = loaded(LIVE_ENV)
        self.assertTrue(settings.live)
        self.assertEqual(settings.mode, "live")
        self.assertEqual(settings.active_department_id, "4000000000003")
        self.assertEqual(settings.unverified_contact_id, "4000000000004")
        self.assertEqual(settings.test_department_id, "4000000000001")

    def test_live_without_the_real_department_or_the_unverified_contact_is_misconfigured(self):
        env = without(LIVE_ENV, zs.DEPARTMENT_ID, zs.UNVERIFIED_CONTACT_ID)
        self.assertEqual(
            load_zoho_settings(env),
            (None, "misconfigured: missing EMOTORAD_ZOHO_DEPARTMENT_ID, EMOTORAD_ZOHO_UNVERIFIED_CONTACT_ID"),
        )

    def test_each_live_only_name_is_reported(self):
        for name in zs.LIVE_ONLY:
            with self.subTest(name=name):
                self.assertEqual(load_zoho_settings(without(LIVE_ENV, name)), (None, "misconfigured: missing %s" % name))


class MissingTests(unittest.TestCase):
    def test_each_always_needed_name_is_reported_by_name(self):
        for name in zs.ALWAYS:
            with self.subTest(name=name):
                self.assertEqual(load_zoho_settings(without(TEST_ENV, name)), (None, "misconfigured: missing %s" % name))

    def test_a_blank_value_counts_as_missing(self):
        self.assertEqual(load_zoho_settings(dict(TEST_ENV, **{zs.ORG_ID: "  "})),
                         (None, "misconfigured: missing EMOTORAD_ZOHO_ORG_ID"))

    def test_several_missing_names_come_in_one_fixed_order(self):
        env = without(TEST_ENV, zs.AI_ENV, zs.CLIENT_SECRET)
        self.assertEqual(load_zoho_settings(env),
                         (None, "misconfigured: missing EMOTORAD_ZOHO_CLIENT_SECRET, EMOTORAD_AI_ENV"))

    def test_the_deployment_name_is_required(self):
        # It prefixes every chat reference, so two deployments sharing the
        # Zoho organisation never adopt each other's tickets.
        self.assertEqual(load_zoho_settings(without(TEST_ENV, "EMOTORAD_AI_ENV")),
                         (None, "misconfigured: missing EMOTORAD_AI_ENV"))

    def test_the_probe_only_values_and_the_layout_are_never_required(self):
        optional = (zs.PRIORITY_HIGH, zs.PRIORITY_MEDIUM, zs.CHANNEL, zs.CREDITS_FLOOR,
                    zs.ATTACHMENT_LIMIT_MB, zs.LAYOUT_ID)
        for name in optional:
            self.assertNotIn(name, zs.ALWAYS + zs.LIVE_ONLY)
            self.assertNotIn(name, LIVE_ENV)
        self.assertEqual(load_zoho_settings(LIVE_ENV)[1], "ok")

    def test_a_blank_layout_is_no_layout(self):
        self.assertIsNone(loaded(dict(TEST_ENV, **{zs.LAYOUT_ID: "   "})).layout_id)


class NoCustomFieldTests(unittest.TestCase):
    """The Desk's text custom fields are at their limit (Amendment B1), so no
    custom field name is a setting and none is ever required."""

    def test_no_custom_field_name_is_read_or_kept(self):
        self.assertFalse([name for name in zs.ENV_NAMES if "_CF_" in name])
        self.assertFalse([f.name for f in fields(ZohoSettings) if f.name.startswith("cf_")])
        # A name still set in an old secret is ignored, not required and not an error.
        old = dict(TEST_ENV, EMOTORAD_ZOHO_CF_CHAT_REFERENCE="cf_chat_reference", EMOTORAD_ZOHO_CF_SOURCE="cf_source")
        self.assertEqual(load_zoho_settings(old)[1], "ok")


class NumberTests(unittest.TestCase):
    def test_a_number_setting_that_is_not_a_usable_whole_number_is_misconfigured(self):
        cases = [
            (zs.CREDITS_FLOOR, "lots"), (zs.CREDITS_FLOOR, "1.5"), (zs.CREDITS_FLOOR, "-1"),
            (zs.CREDITS_FLOOR, "1_000"), (zs.ATTACHMENT_LIMIT_MB, "0"), (zs.ATTACHMENT_LIMIT_MB, "२०"),
            (zs.ATTACHMENT_LIMIT_MB, "20MB"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.assertEqual(load_zoho_settings(dict(TEST_ENV, **{name: value})),
                                 (None, "misconfigured: bad number: %s" % name))

    def test_both_bad_numbers_are_named_in_one_fixed_order(self):
        env = dict(TEST_ENV, **{zs.ATTACHMENT_LIMIT_MB: "big", zs.CREDITS_FLOOR: "lots"})
        self.assertEqual(load_zoho_settings(env),
                         (None, "misconfigured: bad number: %s, %s" % (zs.CREDITS_FLOOR, zs.ATTACHMENT_LIMIT_MB)))

    def test_a_credits_floor_of_zero_is_allowed(self):
        self.assertEqual(loaded(dict(TEST_ENV, **{zs.CREDITS_FLOOR: "0"})).credits_floor, 0)

    def test_plain_ascii_numbers_are_read(self):
        settings = loaded(dict(TEST_ENV, **{zs.CREDITS_FLOOR: "250", zs.ATTACHMENT_LIMIT_MB: "1"}))
        self.assertEqual((settings.credits_floor, settings.attachment_limit_bytes), (250, 1024 * 1024))


class SecretTests(unittest.TestCase):
    def test_no_returned_string_holds_a_value(self):
        envs = [
            TEST_ENV, LIVE_ENV, without(TEST_ENV, zs.ORG_ID, zs.CLIENT_SECRET), without(LIVE_ENV, zs.DEPARTMENT_ID),
            dict(TEST_ENV, **{zs.CREDITS_FLOOR: "lots"}), without(TEST_ENV, zs.REFRESH_TOKEN),
        ]
        for env in envs:
            settings, status = load_zoho_settings(env)
            shown = [status, repr(settings), str(settings)]
            if settings is not None:
                for changes in ({"region": "eu-central-1"}, {"store_kind": "memory"}, {"ticket_store": None},
                                {"ticket_store": FakeTicketStore(indexed=False)}, {"dev_codes": True}):
                    shown.append(problem(settings, **changes) or "")
            for text in shown:
                for secret in SECRETS:
                    self.assertNotIn(secret, text)

    def test_repr_leaves_out_the_credential_fields(self):
        text = repr(loaded(TEST_ENV))
        for name in ("client_id", "client_secret", "refresh_token"):
            self.assertNotIn(name, text)
        self.assertIn("org_id='60000000001'", text)


class StartupTests(unittest.TestCase):
    def test_a_sound_setup_has_no_problem(self):
        store = FakeTicketStore()
        self.assertIsNone(problem(loaded(TEST_ENV), ticket_store=store))
        self.assertIsNone(problem(loaded(LIVE_ENV)))
        self.assertEqual(store.asked, 1)

    def test_an_eu_region_is_refused_before_anything_else(self):
        for region in ("eu-central-1", "eu-west-1", "EU-NORTH-1", " eu-south-2"):
            with self.subTest(region=region):
                self.assertEqual(problem(loaded(TEST_ENV), region=region, store_kind="memory", ticket_store=None),
                                 "not allowed in this region")

    def test_an_eu_region_never_reads_the_index(self):
        store = FakeTicketStore()
        self.assertEqual(problem(loaded(TEST_ENV), region="eu-central-1", ticket_store=store),
                         "not allowed in this region")
        self.assertEqual(store.asked, 0)

    def test_other_regions_pass(self):
        for region in ("ap-south-1", "us-east-1", ""):
            with self.subTest(region=region):
                self.assertIsNone(problem(loaded(TEST_ENV), region=region))

    def test_a_store_that_is_not_mongodb_is_refused_without_asking_for_its_index(self):
        store = FakeTicketStore()
        self.assertEqual(problem(loaded(TEST_ENV), store_kind="memory", ticket_store=store),
                         "misconfigured: store is not mongodb")
        self.assertEqual(store.asked, 0)

    def test_no_ticket_store_counts_as_a_store_that_is_not_mongodb(self):
        # Tests and tools build Stores without tickets; it must not crash at import.
        self.assertEqual(problem(loaded(TEST_ENV), ticket_store=None), "misconfigured: store is not mongodb")
        self.assertEqual(problem(loaded(TEST_ENV), store_kind="memory", ticket_store=None),
                         "misconfigured: store is not mongodb")

    def test_a_missing_unique_index_is_refused(self):
        self.assertEqual(problem(loaded(TEST_ENV), ticket_store=FakeTicketStore(indexed=False)),
                         "misconfigured: tickets index missing")

    def test_an_index_list_that_cannot_be_read_is_left_to_the_caller(self):
        # build_zoho maps StoreUnavailable to STORE_UNREACHABLE (Task 10), so a
        # database that is down is not reported as a missing index.
        store = FakeTicketStore(error=StoreUnavailable("mongodb+srv://user:password@host is down"))
        with self.assertRaises(StoreUnavailable):
            problem(loaded(TEST_ENV), ticket_store=store)

    def test_the_unreachable_store_status_is_one_fixed_string(self):
        self.assertEqual(zs.STORE_UNREACHABLE, "misconfigured: store unreachable")

    def test_live_is_refused_while_verification_is_a_test_one(self):
        settings = loaded(LIVE_ENV)
        refused = "misconfigured: live refused: test verification in use"
        self.assertEqual(problem(settings, dev_codes=True), refused)
        self.assertEqual(problem(settings, otp_is_mock=True), refused)
        self.assertEqual(problem(settings, dev_codes=True, otp_is_mock=True), refused)

    def test_the_test_department_runs_with_test_verification(self):
        # Staging runs dev codes and the mock OTP sender today
        # (deploy-staging.yml), and its tickets go to the test department.
        self.assertIsNone(problem(loaded(TEST_ENV), dev_codes=True, otp_is_mock=True))

    def test_every_problem_reads_as_a_health_value(self):
        settings = loaded(LIVE_ENV)
        found = [
            problem(settings, region="eu-west-1"), problem(settings, store_kind="memory"),
            problem(settings, ticket_store=None), problem(settings, ticket_store=FakeTicketStore(indexed=False)),
            problem(settings, dev_codes=True), zs.STORE_UNREACHABLE,
        ]
        for text in found:
            self.assertTrue(text == zs.NOT_ALLOWED_IN_REGION or text.startswith("misconfigured: "), text)


class NamesTests(unittest.TestCase):
    def test_env_names_lists_every_zoho_name_once(self):
        names = {value for value in vars(zs).values() if isinstance(value, str) and value.startswith("EMOTORAD_ZOHO_")}
        self.assertEqual(set(zs.ENV_NAMES), names)
        self.assertEqual(len(zs.ENV_NAMES), len(set(zs.ENV_NAMES)))
        self.assertEqual(len(zs.ENV_NAMES), 16)

    def test_the_layout_and_the_probe_only_values_are_in_the_list(self):
        for name in (zs.LAYOUT_ID, zs.PRIORITY_HIGH, zs.PRIORITY_MEDIUM, zs.CHANNEL,
                     zs.CREDITS_FLOOR, zs.ATTACHMENT_LIMIT_MB):
            self.assertIn(name, zs.ENV_NAMES)
        self.assertEqual(zs.LAYOUT_ID, "EMOTORAD_ZOHO_LAYOUT_ID")

    def test_the_deployment_name_is_not_a_zoho_name(self):
        # The deploy sets it for the traces too, so blanking "every Zoho name" must not touch it.
        self.assertNotIn(zs.AI_ENV, zs.ENV_NAMES)


if __name__ == "__main__":
    unittest.main()
