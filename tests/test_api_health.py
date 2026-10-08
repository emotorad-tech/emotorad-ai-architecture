"""The health check is what the deploy workflow reads. It has to say which model
path is live and whether the config store was used, so a container that started
without its secret is visible in the workflow log rather than in a customer chat."""

import asyncio
import importlib
import os
import unittest
from unittest import mock

from emotorad_ai.zoho.settings import ENV_NAMES


def fresh_api(env):
    with mock.patch.dict(os.environ, env, clear=False):
        import emotorad_ai.api as api

        return importlib.reload(api)


def zoho_blank():
    """Every Zoho setting (spec 2026-10-05 section 9) blank, together with any
    other EMOTORAD_ZOHO_* name on this machine, so a laptop with the staging
    secret loaded still sees Zoho off where /health is pinned."""
    names = set(ENV_NAMES) | {name for name in os.environ if name.startswith("EMOTORAD_ZOHO_")}
    return {name: "" for name in names}


class HealthTests(unittest.TestCase):
    def test_offline_reports_no_secret(self):
        # Both video keys blanked, so the frames fallback is what is reported on
        # any machine, including one with a real key in its environment. Zoho
        # blanked for the same reason: off, it is the mock, and no ticket
        # counts appear while nothing is waiting.
        api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": "",
                              "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": "", "EMOTORAD_AMIGO_PG_DSN": "",
                              "EMOTORAD_AI_BUILD": "", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb",
                              "EMOTORAD_AMIIGO_PUBLIC_KEY": "", "EMOTORAD_OMS_API_KEY": "",
                              "EMOTORAD_WARRANTY_API_KEY": "", "EMOTORAD_MELT_ASK": "",
                              "EMOTORAD_SERIAL_ASK": ""}, **zoho_blank()))
        self.assertEqual(
            api.health(),
            {"status": "ok", "mode": "offline", "store": "memory", "secrets": "not configured", "media": "not configured",
             "guide_media": "0 of %d sendable" % len(api.GUIDE_MEDIA), "video_summary": "frames", "tracing": "off",
             "amigo": "not configured", "build": "unknown", "ip_location": "not configured",
             "photo_check": "off", "zoho": "not configured", "verification_sessions": "memory",
             "amiigo_receipts": "memory", "amiigo_tokens": "not configured", "zoho_webhook": "not configured",
             "warranty_source": "fixtures", "melt_ask": "off", "serial_ask": "off",
             "serial_read": "off", "jev": "off: mode offline"},
        )

    def test_health_says_why_the_melt_ask_is_off_when_it_is_switched_on(self):
        # Switched on where the pictures cannot be signed, it stays off and
        # says why.
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_MELT_ASK": "on", "EMOTORAD_AI_MEDIA_BUCKET": ""})
        # No bucket here, so no picture can be signed: off, and says why.
        self.assertEqual(api.health()["melt_ask"],
                         "off: unresolvable melt_battery_serial, melt_controller_label, melt_terminals")
        self.assertIsNone(api.runtime.melt_ask)
        self.assertEqual(len(api.GUIDE_MEDIA), 10)
        # 10 offered to the model (2 of them the library's, 6 the motor clips), 3 melt pictures,
        # 14 more library files and the odometer photo.
        self.assertEqual(len(api.CATALOGUE), 28)

    def test_the_runtime_is_given_the_melt_ask_when_it_is_on(self):
        from emotorad_ai import melt_ask

        # Shaped like the ask: the runtime hands its battery_melt to triage.
        sentinel = mock.Mock(spec=melt_ask.MeltAsk)
        with mock.patch.object(melt_ask, "from_env", return_value=(sentinel, "on")):
            api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_MELT_ASK": "on"})
        self.assertIs(api.runtime.melt_ask, sentinel)
        self.assertEqual(api.health()["melt_ask"], "on")

    def test_safety_reports_not_recorded_are_counted_once_there_are_any(self):
        # Spec 2026-10-05, section 6: safety_ticket_not_recorded is alarmed and
        # counted on /health. Absent at 0, so the pinned report above holds.
        api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline"}, **zoho_blank()))
        self.assertNotIn("safety_tickets_not_recorded", api.health())
        api.runtime.safety_not_recorded = 2
        self.assertEqual(api.health()["safety_tickets_not_recorded"], 2)

    def test_health_names_the_commit_it_was_built_from(self):
        # The deploy checks this, so a build that failed on the server and
        # left an older image running fails the deploy instead of passing it
        # (staging ran 22 September's image through two "deploys", 2026-09-30).
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_BUILD": "bf666748703b"})
        self.assertEqual(api.health()["build"], "bf666748703b")

    def test_with_no_bucket_no_guide_picture_is_offered(self):
        # The person's rule (2026-09-29): a picture the server cannot send is
        # never offered. With no bucket none can be, so the tool is not there.
        from emotorad_ai.tools.mocks import SEND_GUIDE_MEDIA

        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_MEDIA_BUCKET": ""})
        self.assertGreater(len(api.GUIDE_MEDIA), 0)
        self.assertEqual(api.SENDABLE_MEDIA, {})
        self.assertNotIn(SEND_GUIDE_MEDIA, api.registry.specs)

    def test_health_names_the_video_summariser(self):
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "OPENROUTER_API_KEY": "sk-or-test", "GEMINI_API_KEY": ""})
        self.assertEqual(api.health()["video_summary"], "openrouter")

    def test_health_says_whether_tracing_is_on(self):
        api = fresh_api({"EMOTORAD_AI_MODE": "offline", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""})
        self.assertEqual(api.health()["tracing"], "off")

    def test_tracing_keys_attach_the_sink_to_the_log(self):
        with mock.patch("emotorad_ai.tracing.langfuse_sink_from_env") as from_env:
            from_env.return_value = object()
            api = fresh_api({"EMOTORAD_AI_MODE": "offline", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
        self.assertEqual(api.health()["tracing"], "on")
        self.assertIn(from_env.return_value, api.log.sinks)

    def test_shutdown_flushes_the_tracing_sink(self):
        class Sink:
            flushed = 0

            def flush(self):
                self.flushed += 1

        sink = Sink()
        with mock.patch("emotorad_ai.tracing.langfuse_sink_from_env", return_value=sink):
            api = fresh_api({"EMOTORAD_AI_MODE": "offline"})

        async def run_lifespan():
            async with api._lifespan(api.app):
                pass

        asyncio.run(run_lifespan())
        self.assertEqual(sink.flushed, 1)

    def test_registry_offers_the_guide_media_tool(self):
        # The playground wires guide_media into build_registry so the model can
        # send pictures. Production has to do the same, or a deployed agent
        # can be asked for a picture and simply has no tool to send one with.
        # With a bucket that can sign links: without one no picture can be sent,
        # and the tool is rightly left out (test_with_no_bucket_...).
        class Signs:
            bucket = "fake"

            def presign_get(self, key):
                return "https://bucket.example/" + key

            def presign_put(self, key, mime, size):
                return {"url": "https://bucket.example/" + key, "headers": {}, "expires_in": 300}

            def get_bytes(self, key):
                return b""

            def head(self, key):
                return None

        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=Signs()), \
                mock.patch("emotorad_ai.media.store_for_resolve", return_value=Signs()):
            api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_MEDIA_BUCKET": "fake"})
        self.assertIn("send_guide_media", api.registry.specs)
        self.assertEqual(api.health()["guide_media"], "%d of %d sendable" % (len(api.GUIDE_MEDIA), len(api.GUIDE_MEDIA)))
        fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_MEDIA_BUCKET": ""})

    def test_a_secret_id_reports_loaded(self):
        api = fresh_api(
            {
                "EMOTORAD_AI_MODE": "offline",
                "EMOTORAD_AI_SECRET_ID": "/emotorad/stage/ai/app",
                "EMOTORAD_AI_CONFIG_EXPORTED": "5",
            }
        )
        self.assertEqual(api.health()["secrets"], "loaded")

    def test_a_secret_that_exported_nothing_reports_empty(self):
        api = fresh_api(
            {
                "EMOTORAD_AI_MODE": "offline",
                "EMOTORAD_AI_SECRET_ID": "/emotorad/stage/ai/app",
                "EMOTORAD_AI_CONFIG_EXPORTED": "0",
            }
        )
        self.assertEqual(api.health()["secrets"], "empty")

    def test_no_secret_id_with_export_count_zero_reports_not_configured(self):
        api = fresh_api(
            {
                "EMOTORAD_AI_MODE": "offline",
                "EMOTORAD_AI_SECRET_ID": "",
                "EMOTORAD_AI_CONFIG_EXPORTED": "0",
            }
        )
        self.assertEqual(api.health()["secrets"], "not configured")

    def test_anthropic_mode_without_a_key_fails_at_import(self):
        from emotorad_ai.llm import LLMConfigError

        with self.assertRaises(LLMConfigError):
            fresh_api({"EMOTORAD_AI_MODE": "anthropic", "ANTHROPIC_API_KEY": ""})

    @classmethod
    def tearDownClass(cls):
        fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_SECRET_ID": ""}, **zoho_blank()))


if __name__ == "__main__":
    unittest.main()
