"""The Amiigo token check (docs/contracts/amiigo-support-chat.md, "Authentication").

Amiigo signs PASETO v4.public access tokens with its own Ed25519 key; we hold
only the public half, in EMOTORAD_AMIIGO_PUBLIC_KEY. These tests never see
Amiigo's real key: every token is minted with a keypair made here
(tests/amiigo_tokens.py), and the official PASETO test vectors pin the
signature framing itself.
"""

import base64
import hashlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.amiigo import auth as auth_module
from emotorad_ai.amiigo.auth import (
    CheckResult,
    Rider,
    TokenCheck,
    TokenInvalid,
    rider_from_header,
    token_check_from_env,
    verify_v4_public,
)
from emotorad_ai.contract import VERIFIED, Identity
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.registry import ToolContext, ToolRegistry
from emotorad_ai.tools.verification import (
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    VerificationStore,
    register_verification_tools,
)
from tests.amiigo_tokens import DROP, EMUSER_ID, HEADER, RIDER_PHONE, Keypair, b64, rfc3339

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=timezone.utc)
VECTORS = Path(__file__).resolve().parent / "data" / "paseto_v4_public_vectors.json"
LOGGER = "emotorad_ai.amiigo.auth"


def at(moment):
    return lambda: moment


def new_process():
    """amiigo_tokens_not_configured is logged once in a process; this makes the
    test the process's first."""
    return mock.patch.object(auth_module, "_not_configured_logged", False)


class VectorTests(unittest.TestCase):
    """The official vectors (paseto-standard/test-vectors, v4.json)."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(VECTORS.read_text(encoding="utf-8"))

    def test_the_file_says_where_it_came_from_and_carries_its_licence(self):
        self.assertIn("https://github.com/paseto-standard/test-vectors", self.data["source"])
        # ISC asks for the copyright and permission notice in every copy.
        self.assertIn("ISC License", self.data["licence"])
        self.assertIn("Paragon Initiative Enterprises", self.data["licence"])
        self.assertIn("Permission to use, copy, modify, and/or distribute", self.data["licence"])
        kinds = {vector["expect-fail"] for vector in self.data["tests"]}
        self.assertEqual(kinds, {True, False})

    def test_no_vector_carries_a_secret_key(self):
        for vector in self.data["tests"]:
            self.assertFalse([name for name in vector if name.startswith("secret-key")], vector["name"])

    def test_every_success_vector_verifies(self):
        for vector in self.data["tests"]:
            if vector["expect-fail"]:
                continue
            with self.subTest(vector["name"]):
                message, footer = verify_v4_public(
                    vector["token"], bytes.fromhex(vector["public-key"]),
                    vector["implicit-assertion"].encode("utf-8"))
                self.assertEqual(message, vector["payload"].encode("utf-8"))
                self.assertEqual(footer, vector["footer"].encode("utf-8"))

    def test_every_fail_vector_is_refused(self):
        for vector in self.data["tests"]:
            if not vector["expect-fail"]:
                continue
            with self.subTest(vector["name"]):
                with self.assertRaises(TokenInvalid):
                    verify_v4_public(vector["token"], bytes.fromhex(vector["public-key"]),
                                     vector["implicit-assertion"].encode("utf-8"))
                result = TokenCheck(vector["public-key"], clock=at(NOW)).check(vector["token"])
                self.assertEqual((result.ok, result.error), (False, "token_invalid"))

    def test_the_implicit_assertion_is_signed_over(self):
        # 4-S-3 was signed with an implicit assertion; without it, it fails.
        vector = next(v for v in self.data["tests"] if v["name"] == "4-S-3")
        with self.assertRaises(TokenInvalid):
            verify_v4_public(vector["token"], bytes.fromhex(vector["public-key"]), b"")


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keypair()
        self.checker = TokenCheck(self.keys.public_hex, clock=at(NOW))

    def check(self, token):
        return self.checker.check(token)

    def assertRefused(self, token, error):
        result = self.check(token)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, error)
        self.assertIsNone(result.phone)
        self.assertIsNone(result.emuser_id)

    # -- the good token ----------------------------------------------------

    def test_an_access_token_gives_the_phone_and_the_emuser_id(self):
        result = self.check(self.keys.token(now=NOW))
        self.assertEqual(result, CheckResult(ok=True, error=None, phone=RIDER_PHONE, emuser_id=EMUSER_ID,
                                             expires_at=NOW + timedelta(hours=1)))

    def test_the_phone_is_normalised_as_the_runtime_keeps_a_verified_one(self):
        # "+91" and ten digits: what tools/verification.py stores for a proved
        # number, so the user key is the one a verified web chat gets.
        for given in ("+919700000010", "9700000010", "919700000010", "09700000010", "+91 97000-00010"):
            with self.subTest(given):
                self.assertEqual(self.check(self.keys.token(now=NOW, phone=given)).phone, "+919700000010")

    def test_the_user_key_is_the_one_a_verified_web_chat_with_that_number_gets(self):
        # The same person through the website: the verification tool stores the
        # proved number, and the runtime keys the conversation on it.
        for given in ("+919700000010", "9700000010", "919700000010", "09700000010", "+91 97000-00010"):
            with self.subTest(given):
                store = VerificationStore()
                registry = ToolRegistry()
                register_verification_tools(registry, store, code_factory=lambda: "111111")
                ctx = ToolContext(conversation_id="c1")
                registry.call(REQUEST_IDENTITY_VERIFICATION, {"phone": given}, ctx)
                registry.call(VERIFY_IDENTITY, {"code": "111111"}, ctx)
                web = Runtime._user_key(ResolvedIdentity(
                    persona="customer", method="verified",
                    identity=Identity(strength=VERIFIED, phone=store.verified_phone("c1"))))
                rider = rider_from_header("Bearer " + self.keys.token(now=NOW, phone=given), self.checker)
                self.assertEqual(rider.user_key, web)
                self.assertEqual(web, "PHONE#+919700000010")

    def test_a_foreign_number_is_never_read_as_an_indian_one(self):
        # Ten digits in all after the country code's "+": Singapore, the
        # Maldives, Lebanon. Read with the "+" dropped, each is a valid Indian
        # mobile, and the rider would get another person's chats and bikes.
        for phone in ("+6591234567", "+9607771234", "+9613123456", "+65 9123 4567", "+1 6505550123"):
            with self.subTest(phone=phone):
                self.assertRefused(self.keys.token(now=NOW, phone=phone), "token_invalid")

    def test_exp_in_another_offset_is_read_as_the_same_instant(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        exp = (NOW + timedelta(hours=1)).astimezone(ist)
        self.assertEqual(rfc3339(exp), "2026-10-06T16:30:00+05:30")
        result = self.check(self.keys.token(now=NOW, claims={"exp": rfc3339(exp)}))
        self.assertTrue(result.ok)
        self.assertEqual(result.expires_at, NOW + timedelta(hours=1))
        self.assertEqual(result.expires_at.utcoffset(), timedelta(0))

    def test_fractional_seconds_are_read(self):
        result = self.check(self.keys.token(now=NOW, claims={"exp": "2026-10-06T11:00:00.5Z"}))
        self.assertEqual(result.expires_at, NOW + timedelta(hours=1, milliseconds=500))

    # -- time --------------------------------------------------------------

    def test_an_expired_token_is_token_expired(self):
        token = self.keys.token(now=NOW - timedelta(hours=2), lifetime=timedelta(hours=1))
        self.assertRefused(token, "token_expired")

    def test_expiry_allows_sixty_seconds_of_clock_skew(self):
        def expiring(seconds_ago):
            return self.keys.token(now=NOW - timedelta(hours=1), claims={
                "exp": rfc3339(NOW - timedelta(seconds=seconds_ago))})

        self.assertTrue(self.check(expiring(59)).ok)
        self.assertRefused(expiring(60), "token_expired")
        self.assertRefused(expiring(61), "token_expired")

    def test_a_token_not_yet_valid_is_invalid(self):
        self.assertRefused(self.keys.token(now=NOW, not_before=NOW + timedelta(seconds=61)), "token_invalid")
        self.assertRefused(self.keys.token(now=NOW, not_before=NOW + timedelta(seconds=60)), "token_invalid")
        self.assertTrue(self.check(self.keys.token(now=NOW, not_before=NOW + timedelta(seconds=59))).ok)

    def test_nbf_is_optional(self):
        self.assertTrue(self.check(self.keys.token(now=NOW, claims={"nbf": DROP})).ok)

    def test_exp_missing_or_unreadable_is_invalid(self):
        for exp in (DROP, None, "", "tomorrow", 1791280800, "2026-10-06 11:00:00Z", "2026-10-06T11:00:00",
                    "2026-10-06", "2026-13-06T11:00:00Z", "2026-10-06T11:00:00+5:30",
                    "२०२६-10-06T11:00:00Z", "9999-12-31T23:59:59-23:59x"):
            with self.subTest(exp=exp):
                self.assertRefused(self.keys.token(now=NOW, claims={"exp": exp}), "token_invalid")

    def test_nbf_unreadable_is_invalid(self):
        for nbf in (None, "", "now", 0, "2026-10-06T10:00:00"):
            with self.subTest(nbf=nbf):
                self.assertRefused(self.keys.token(now=NOW, claims={"nbf": nbf}), "token_invalid")

    def test_a_time_at_the_edge_of_the_calendar_is_invalid_not_an_error(self):
        # Converting this to UTC overflows the calendar.
        self.assertRefused(self.keys.token(now=NOW, claims={"exp": "9999-12-31T23:59:59-23:59"}), "token_invalid")

    # -- signature and framing ---------------------------------------------

    def test_a_token_signed_with_another_key_is_invalid(self):
        self.assertRefused(Keypair().token(now=NOW), "token_invalid")

    def test_a_tampered_body_is_invalid(self):
        token = self.keys.token(now=NOW)
        at_index = len(HEADER) + 12
        swapped = "B" if token[at_index] == "A" else "A"
        self.assertRefused(token[:at_index] + swapped + token[at_index + 1:], "token_invalid")

    def test_a_tampered_footer_is_invalid(self):
        signed_with_footer = self.keys.token(now=NOW, footer=b'{"kid":"amiigo-1"}')
        self.assertTrue(self.check(signed_with_footer).ok)
        body = signed_with_footer.rsplit(".", 1)[0]
        self.assertRefused(body + "." + b64(b'{"kid":"amiigo-2"}'), "token_invalid")
        self.assertRefused(body, "token_invalid")
        self.assertRefused(body + ".", "token_invalid")
        self.assertRefused(self.keys.token(now=NOW) + "." + b64(b'{"kid":"amiigo-1"}'), "token_invalid")

    def test_a_truncated_token_is_invalid(self):
        token = self.keys.token(now=NOW)
        for cut in (token[:-1], token[:-4], token[:len(HEADER) + 40], token[:len(HEADER)], "v4.public"):
            with self.subTest(length=len(cut)):
                self.assertRefused(cut, "token_invalid")

    def test_only_v4_public_is_accepted(self):
        token = self.keys.token(now=NOW)
        rest = token[len(HEADER):]
        for header in ("v4.local.", "v3.public.", "v2.public.", "V4.PUBLIC.", "v4.public:", ""):
            with self.subTest(header=header):
                self.assertRefused(header + rest, "token_invalid")

    def test_an_empty_token_is_missing(self):
        self.assertRefused(None, "token_missing")
        self.assertRefused("", "token_missing")

    def test_a_body_in_non_canonical_base64_is_invalid(self):
        # 7 bytes of message and 64 of signature: 71 is not a multiple of 3, so
        # the last base64 character carries unused bits. A lenient decoder
        # reads a token with those bits set as the same bytes; it is not the
        # token that was signed.
        token = self.keys.sign(b'{"a":1}')
        self.assertTrue(verify_v4_public(token, self.keys.public_bytes))
        last = token[-1]
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        decode = base64.urlsafe_b64decode
        body = token[len(HEADER):]
        padding = "=" * (-len(body) % 4)
        twins = [c for c in alphabet if c != last and decode(body[:-1] + c + padding) == decode(body + padding)]
        self.assertTrue(twins)
        for twin in twins:
            with self.assertRaises(TokenInvalid):
                verify_v4_public(token[:-1] + twin, self.keys.public_bytes)

    def test_malformed_input_is_invalid_and_never_raises(self):
        token = self.keys.token(now=NOW)
        junk = [
            "v4.public.!!!!", "v4.public.a.b.c", token + "=", token + "==", token + " ", " " + token,
            "v4.public.हिन्दी", "\x00", "v4.public." + "A" * 85,
            token.replace("-", "+").replace("_", "/") if ("-" in token or "_" in token) else token + "+",
            self.keys.sign(b"\xff\xfe\xfd"),            # not UTF-8
            self.keys.sign(b"hello"),                    # not JSON
            self.keys.sign(b"[1, 2]"),                   # not an object
            self.keys.sign(b""),                         # nothing signed
            self.keys.sign(b"[" * 100000 + b"]" * 100000),  # nested past the parser's depth
        ]
        for value in junk:
            with self.subTest(value=value[:40]):
                self.assertRefused(value, "token_invalid")
        for value in (12345, b"v4.public.x", ["v4.public."]):
            with self.subTest(value=value):
                self.assertRefused(value, "token_invalid")

    # -- the claims Amiigo puts in the payload -------------------------------

    def test_only_an_access_token_is_allowed(self):
        for kind in ("refresh", "otp", "otp-resend", "email", "child-otp", "web-otp", "", "ACCESS", " access",
                     None, 1, DROP):
            with self.subTest(kind=kind):
                self.assertRefused(self.keys.token(now=NOW, token_type=kind), "token_type_not_allowed")

    def test_no_phone_or_a_malformed_phone_is_invalid(self):
        for phone in (DROP, None, "", "   ", "12345", "+449700000010", "5700000010", "97000000101", 9700000010,
                      "+91९७००००००१०", "9७००००००१०", "+6591234567", "+9607771234", "+9613123456"):
            with self.subTest(phone=phone):
                self.assertRefused(self.keys.token(now=NOW, phone=phone), "token_invalid")

    def test_the_payload_claim_must_be_a_json_object_in_a_string(self):
        for payload in (DROP, None, {"phone": RIDER_PHONE, "token_type": "access"}, "not json", "[1, 2]",
                        '"access"', ""):
            with self.subTest(payload=payload):
                self.assertRefused(self.keys.token(now=NOW, claims={"payload": payload}), "token_invalid")

    def test_no_emuser_id_still_identifies_the_rider(self):
        for emuser_id in (DROP, None, 42, ""):
            with self.subTest(emuser_id=emuser_id):
                result = self.check(self.keys.token(now=NOW, emuser_id=emuser_id))
                self.assertTrue(result.ok)
                self.assertEqual(result.phone, RIDER_PHONE)
                self.assertIsNone(result.emuser_id)

    # -- what leaves the check -----------------------------------------------

    def test_a_check_logs_nothing(self):
        with self.assertNoLogs("emotorad_ai", level="DEBUG"):
            self.check(self.keys.token(now=NOW))
            self.check(Keypair().token(now=NOW))
            self.check("v4.public.!!!!")

    def test_the_result_does_not_show_the_phone_or_the_emuser_id_in_its_repr(self):
        shown = repr(self.check(self.keys.token(now=NOW)))
        self.assertNotIn("9700000010", shown)
        self.assertNotIn(EMUSER_ID, shown)


class NotConfiguredTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keypair()

    def test_without_a_usable_key_the_check_is_off_and_refuses_everything(self):
        for value in (None, "", "   ", "abc", "zz" * 32, self.keys.public_hex[:-2], self.keys.public_hex + "00",
                      "0x" + self.keys.public_hex[2:], "٠" * 64):
            with self.subTest(value=value):
                with new_process(), self.assertLogs(LOGGER, level="WARNING") as logged:
                    checker = TokenCheck(value, clock=at(NOW))
                self.assertFalse(checker.enabled)
                self.assertEqual(checker.check(self.keys.token(now=NOW)),
                                 CheckResult(ok=False, error="token_invalid", phone=None, emuser_id=None,
                                             expires_at=None))
                self.assertEqual(len(logged.records), 1)
                self.assertIn("amiigo_tokens_not_configured", logged.output[0])
                # The value is never logged, whatever it was.
                if value and value.strip():
                    self.assertNotIn(value.strip(), logged.output[0])

    def test_not_configured_is_logged_once_in_a_process_never_per_check(self):
        with new_process():
            with self.assertLogs(LOGGER, level="WARNING") as logged:
                checker = TokenCheck(None)
            self.assertEqual(len(logged.records), 1)
            with self.assertNoLogs(LOGGER, level="DEBUG"):
                for _ in range(3):
                    checker.check(self.keys.token())
                # api.py is reloaded many times by the suite: still once.
                self.assertFalse(TokenCheck("").enabled)

    def test_a_configured_check_starts_quietly(self):
        with self.assertNoLogs(LOGGER, level="DEBUG"):
            checker = TokenCheck(self.keys.public_hex)
        self.assertTrue(checker.enabled)

    def test_a_key_pasted_with_capitals_or_a_newline_is_read(self):
        checker = TokenCheck(" " + self.keys.public_hex.upper() + "\n", clock=at(NOW))
        self.assertTrue(checker.enabled)
        self.assertTrue(checker.check(self.keys.token(now=NOW)).ok)

    def test_the_key_comes_from_the_environment(self):
        self.assertTrue(token_check_from_env({"EMOTORAD_AMIIGO_PUBLIC_KEY": self.keys.public_hex}).enabled)
        with new_process(), self.assertLogs(LOGGER, level="WARNING"):
            self.assertFalse(token_check_from_env({}).enabled)
        with mock.patch.dict(os.environ, {"EMOTORAD_AMIIGO_PUBLIC_KEY": self.keys.public_hex}):
            self.assertTrue(token_check_from_env().enabled)

    def test_the_real_clock_is_used_by_default(self):
        checker = TokenCheck(self.keys.public_hex)
        self.assertTrue(checker.check(self.keys.token()).ok)
        expired = self.keys.token(now=datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertEqual(checker.check(expired).error, "token_expired")


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.keys = Keypair()
        self.checker = TokenCheck(self.keys.public_hex, clock=at(NOW))
        self.token = self.keys.token(now=NOW)

    def test_a_bearer_token_gives_the_rider(self):
        rider = rider_from_header("Bearer " + self.token, self.checker)
        self.assertIsInstance(rider, Rider)
        self.assertEqual(rider.phone, RIDER_PHONE)
        self.assertEqual(rider.user_key, "PHONE#" + RIDER_PHONE)
        self.assertEqual(rider.emuser_id, EMUSER_ID)
        self.assertEqual(rider.expires_at, NOW + timedelta(hours=1))
        self.assertEqual(rider.rider_hash, hashlib.sha256(("PHONE#" + RIDER_PHONE).encode("utf-8")).hexdigest()[:12])
        self.assertEqual(len(rider.rider_hash), 12)

    def test_the_scheme_is_not_case_sensitive(self):
        for scheme in ("bearer", "BEARER", "Bearer"):
            with self.subTest(scheme=scheme):
                self.assertIsInstance(rider_from_header(scheme + " " + self.token, self.checker), Rider)

    def test_no_token_is_token_missing(self):
        for header in (None, "", "   ", "Bearer", "Bearer   ", "bearer "):
            with self.subTest(header=header):
                self.assertEqual(rider_from_header(header, self.checker), "token_missing")

    def test_any_other_scheme_is_token_invalid(self):
        for header in ("Basic dXNlcjpwYXNz", "Token " + self.token, self.token, "Bearer" + self.token,
                       "Bearer " + self.token + " extra"):
            with self.subTest(header=header[:20]):
                self.assertEqual(rider_from_header(header, self.checker), "token_invalid")

    def test_a_refused_token_gives_its_code(self):
        expired = self.keys.token(now=NOW - timedelta(hours=2))
        refresh = self.keys.token(now=NOW, token_type="refresh")
        self.assertEqual(rider_from_header("Bearer " + expired, self.checker), "token_expired")
        self.assertEqual(rider_from_header("Bearer " + refresh, self.checker), "token_type_not_allowed")
        self.assertEqual(rider_from_header("Bearer " + Keypair().token(now=NOW), self.checker), "token_invalid")

    def test_with_the_check_off_a_token_is_never_accepted(self):
        with new_process(), self.assertLogs(LOGGER, level="WARNING"):
            off = TokenCheck(None)
        self.assertEqual(rider_from_header("Bearer " + self.token, off), "token_invalid")

    def test_a_rider_is_frozen_and_does_not_show_the_phone_in_its_repr(self):
        rider = rider_from_header("Bearer " + self.token, self.checker)
        with self.assertRaises(AttributeError):
            rider.phone = "+919999999999"
        shown = repr(rider)
        self.assertNotIn("9700000010", shown)
        self.assertNotIn(EMUSER_ID, shown)
        self.assertIn(rider.rider_hash, shown)


class HealthTests(unittest.TestCase):
    """/health says whether Amiigo tokens can be checked, never the key."""

    @staticmethod
    def api_with(key):
        from tests.test_api_health import fresh_api, zoho_blank

        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=None):
            return fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AMIIGO_PUBLIC_KEY": key},
                                  **zoho_blank()))

    def test_health_shows_whether_amiigo_tokens_are_checked(self):
        with new_process(), self.assertLogs(LOGGER, level="WARNING"):
            api = self.api_with("")
        self.assertEqual(TestClient(api.app).get("/health").json()["amiigo_tokens"], "not configured")
        self.assertFalse(api.AMIIGO_TOKENS.enabled)

        key = Keypair().public_hex
        api = self.api_with(key)
        report = TestClient(api.app).get("/health").json()
        self.assertEqual(report["amiigo_tokens"], "on")
        self.assertNotIn(key, json.dumps(report))
        self.assertTrue(api.AMIIGO_TOKENS.enabled)

    @classmethod
    def tearDownClass(cls):
        cls.api_with("")


if __name__ == "__main__":
    unittest.main()
