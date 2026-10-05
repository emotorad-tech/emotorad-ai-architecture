"""The Zoho scripts a person runs, and the alarm stack (spec 2026-10-05, sections 8 and 10).

The scripts reach the real Zoho, so none runs against it here. What is tested
is every part that decides something: the consent address and its scopes, the
code exchange's India rule, the masking of what is printed and saved, the
department guard, the first create without custom fields and what a refusal
says, the look-up the worker shares, the listing for the support lead, the
runbook and contract wording, and the alarm stack against the events the code
emits.

Zoho is a pair of small doubles with DeskHTTP.call's signature: FakeAccounts
answers the accounts server in order, FakeHTTP answers Desk by method and
path, and FakeDesk stands in for DeskClient.contact_tickets. Test data is
fake: +919999999999 and the fixtures' frame numbers.
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import urllib.parse
import zlib
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
ZOHO_SCRIPTS = ROOT / "scripts" / "zoho"
if str(ZOHO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ZOHO_SCRIPTS))

import _common  # noqa: E402
from emotorad_ai.tickets.clock import now_iso  # noqa: E402
from emotorad_ai.tickets.record import new_record  # noqa: E402
from emotorad_ai.tickets.store import InMemoryTicketStore  # noqa: E402
from emotorad_ai.zoho.auth import ACCOUNTS_URL, TOKEN_URL  # noqa: E402
from emotorad_ai.zoho.desk import DESK_URL  # noqa: E402
from emotorad_ai.zoho.errors import ZohoAuthExpired, ZohoRejected, ZohoUnavailable, ZohoUnknownOutcome  # noqa: E402
from emotorad_ai.zoho.payload import ticket_payload  # noqa: E402
from emotorad_ai.zoho.settings import ENV_NAMES  # noqa: E402
from tests.fake_zoho import SHAPES as DRAFTS, shape  # noqa: E402
from tests.test_zoho_desk import ShapeChecks  # noqa: E402

SCRIPTS =("_common", "consent_url", "exchange_code", "probe", "test_ticket", "revoke", "tickets_report")
REDIRECT = "https://example.test/zoho/callback"
INDIA = {"access_token": "1000.access.value", "refresh_token": "1000.refresh.value",
         "scope": "Desk.tickets.READ Desk.basic.READ", "api_domain": "https://www.zohoapis.in",
         "token_type": "Bearer", "expires_in": 3600}
TEST_DEPARTMENT = "Inkodop technologies Pvt.Ltd"
DEPARTMENTS = {"data": [{"id": "111", "name": TEST_DEPARTMENT, "isEnabled": True},
                        {"id": "222", "name": "Service", "isEnabled": True}]}
CONTACT_ID = "4000000000002"
TICKET_ID = "1892000000123001"
TICKET_ARGS = ["--org-id", "60001234567", "--contact-id", CONTACT_ID, "--test-department-id", "111"]
PERSONAL = ("Ananya", "Rao", "ananya.rao@example.com", "9999999999", "9876543210",
            "Ravi", "Kumar", "ravi.k@example.com")


def load(name):
    spec = importlib.util.spec_from_file_location("zoho_script_" + name, ZOHO_SCRIPTS / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Screen:
    """What a script printed."""

    def __init__(self):
        self.lines = []

    def out(self, line):
        self.lines.append(str(line))

    @property
    def text(self):
        return "\n".join(self.lines)


def answers(*values):
    """Hidden input, answered in order."""
    queue = list(values)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return queue.pop(0)

    ask.asked = asked
    return ask


def never(prompt):
    raise AssertionError("asked for %r" % prompt)


class FakeAccounts:
    """Zoho's accounts server as DeskHTTP.call reaches it: answers in order,
    and keeps each form that was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.last_credits_remaining = None

    def call(self, method, url, headers, body=None, *, write, timeout=None, classify=True):
        self.calls.append({"method": method, "url": url, "write": write, "classify": classify,
                           "form": dict(urllib.parse.parse_qsl((body or b"").decode("utf-8")))})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeHTTP:
    """DeskHTTP.call for a whole script run, answering by method and path.

    A route's reply is a (status, body) pair, an exception to raise, a list of
    those (taken in order, the last one kept) or a function of the query. A
    call nobody scripted fails the test."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.last_credits_remaining = 4321

    def call(self, method, url, headers, body=None, *, write, timeout=None, classify=True):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append({"method": method, "host": parts.netloc, "path": parts.path, "query": query,
                           "headers": dict(headers), "body": body, "write": write, "classify": classify})
        if (method, parts.path) not in self.routes:
            raise AssertionError("unscripted call: %s %s" % (method, parts.path))
        reply = self.routes[(method, parts.path)]
        if callable(reply):
            reply = reply(query)
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def to(self, method, path):
        return [call for call in self.calls if (call["method"], call["path"]) == (method, path)]


class FakeDesk:
    """DeskClient.contact_tickets, one listing per call."""

    def __init__(self, *listings):
        self.listings = list(listings)
        self.calls = []

    def contact_tickets(self, contact_id, department_id, limit=50):
        self.calls.append((contact_id, department_id))
        return self.listings.pop(0)


def zoho_ticket(chat_reference, number="1201"):
    return {"id": TICKET_ID, "ticketNumber": number, "departmentId": "111",
            "subject": "[AI chat] Battery: charging - EMX Plus [%s]" % chat_reference,
            "createdTime": "2026-10-05T10:00:00.000Z"}


class ScopesAndConsentTests(unittest.TestCase):
    def test_the_eight_scopes_comma_separated(self):
        self.assertEqual(
            _common.scope_string(),
            "Desk.tickets.CREATE,Desk.tickets.UPDATE,Desk.tickets.READ,Desk.search.READ,"
            "Desk.contacts.READ,Desk.contacts.CREATE,Desk.basic.READ,Desk.settings.READ",
        )

    def test_no_scope_for_uploads_is_asked_for(self):
        self.assertNotIn("Desk.basic.CREATE", _common.SCOPES)

    def test_the_hosts_are_the_services_own(self):
        self.assertEqual(_common.ACCOUNTS_URL, ACCOUNTS_URL)
        self.assertEqual(_common.DESK_URL, DESK_URL)
        self.assertEqual(_common.TOKEN_URL, TOKEN_URL)
        self.assertEqual(_common.AUTH_URL, "https://accounts.zoho.in/oauth/v2/auth")
        self.assertEqual(_common.REVOKE_URL, "https://accounts.zoho.in/oauth/v2/revoke/token")

    def test_the_consent_address_is_indias_with_offline_access_and_a_fresh_consent(self):
        url = _common.consent_url("1000.TESTCLIENT", REDIRECT)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "accounts.zoho.in", "/oauth/v2/auth"))
        self.assertEqual(dict(urllib.parse.parse_qsl(parts.query)), {
            "response_type": "code", "client_id": "1000.TESTCLIENT", "scope": _common.scope_string(),
            "redirect_uri": REDIRECT, "access_type": "offline", "prompt": "consent",
        })
        # Commas as Zoho documents them, not %2C.
        self.assertIn("scope=Desk.tickets.CREATE,Desk.tickets.UPDATE,", url)

    def test_the_script_asks_for_the_client_id_hidden_and_prints_the_address(self):
        screen = Screen()
        ask = answers("1000.TESTCLIENT")
        rc = load("consent_url").main(["--redirect-uri", REDIRECT], ask=ask, out=screen.out)
        self.assertEqual(rc, 0)
        self.assertEqual(ask.asked, ["Zoho client id: "])
        [address] = [line for line in screen.lines if line.startswith("https://")]
        self.assertEqual(address, _common.consent_url("1000.TESTCLIENT", REDIRECT))

    def test_the_redirect_address_is_trimmed_and_shown_beside_the_link(self):
        # A pasted space or line end is the commonest "Invalid Redirect Uri".
        screen = Screen()
        rc = load("consent_url").main(["--redirect-uri", "  %s \n" % REDIRECT], ask=answers("1000.TESTCLIENT"),
                                      out=screen.out)
        self.assertEqual(rc, 0)
        [address] = [line for line in screen.lines if line.startswith("https://")]
        self.assertEqual(address, _common.consent_url("1000.TESTCLIENT", REDIRECT))
        shown = screen.lines.index("Redirect address: %s" % REDIRECT)
        self.assertEqual(abs(shown - screen.lines.index(address)), 1)
        self.assertIn("character for character", screen.text)
        self.assertIn("Invalid Redirect Uri", screen.text)

    def test_an_empty_redirect_address_is_refused_before_the_client_id_is_asked_for(self):
        for empty in ("", "   ", "\n"):
            with self.subTest(empty=empty):
                screen = Screen()
                rc = load("consent_url").main(["--redirect-uri", empty], ask=never, out=screen.out)
                self.assertEqual(rc, 1)
                self.assertIn("registered on the client, character for character", screen.text)
                self.assertFalse([line for line in screen.lines if line.startswith("https://")])


class ExchangeTests(unittest.TestCase):
    def exchange(self, *replies, argv=("--redirect-uri", REDIRECT)):
        accounts = FakeAccounts(*replies)
        screen = Screen()
        ask = answers("1000.TESTCLIENT", "test-client-secret", "1000.grantcode")
        rc = load("exchange_code").main(list(argv), ask=ask, http=accounts, out=screen.out)
        return rc, accounts, screen

    def test_the_docstring_says_it_is_not_part_of_the_setup_while_the_token_is_shared(self):
        doc = " ".join((load("exchange_code").__doc__ or "").split())
        self.assertIn("Not part of the setup while the chatbot shares the OMS's Zoho token", doc)
        self.assertIn("a future client of our own", doc)

    def test_server_based_sends_the_redirect_address(self):
        rc, accounts, _ = self.exchange((200, INDIA))
        self.assertEqual(rc, 0)
        [call] = accounts.calls
        self.assertEqual((call["method"], call["url"], call["write"]), ("POST", _common.TOKEN_URL, True))
        self.assertEqual(call["form"], {
            "grant_type": "authorization_code", "client_id": "1000.TESTCLIENT",
            "client_secret": "test-client-secret", "code": "1000.grantcode", "redirect_uri": REDIRECT,
        })

    def test_the_accounts_server_is_read_by_its_body_not_its_status(self):
        # Zoho answers a refusal with HTTP 200 and an "error" field, and a
        # token-endpoint 400 must reach the script as an answer, not as a
        # ZohoRejected the script never reads.
        _, accounts, _ = self.exchange((200, INDIA))
        self.assertIs(accounts.calls[0]["classify"], False)

    def test_a_self_client_sends_no_redirect_address(self):
        rc, accounts, _ = self.exchange((200, INDIA), argv=("--self-client",))
        self.assertEqual(rc, 0)
        self.assertNotIn("redirect_uri", accounts.calls[0]["form"])

    def test_an_india_answer_shows_the_refresh_token_once_and_nothing_else_secret(self):
        rc, _, screen = self.exchange((200, INDIA))
        self.assertEqual(rc, 0)
        self.assertEqual(screen.text.count("1000.refresh.value"), 1)
        self.assertNotIn("1000.access.value", screen.text)
        self.assertNotIn("test-client-secret", screen.text)

    def test_another_data_centre_is_refused_and_its_token_revoked_unseen(self):
        elsewhere = dict(INDIA, api_domain="https://www.zohoapis.com")
        rc, accounts, screen = self.exchange((200, elsewhere), (200, {"status": "success"}))
        self.assertEqual(rc, 1)
        self.assertNotIn("1000.refresh.value", screen.text)
        self.assertEqual(accounts.calls[1]["url"], _common.REVOKE_URL)
        self.assertEqual(accounts.calls[1]["form"], {"token": "1000.refresh.value"})

    def test_a_refused_code_prints_the_error_name_only(self):
        rc, accounts, screen = self.exchange((200, {"error": "invalid_code"}))
        self.assertEqual(rc, 1)
        self.assertIn("invalid_code", screen.text)
        self.assertEqual(len(accounts.calls), 1)

    def test_no_refresh_token_explains_offline_access(self):
        rc, _, screen = self.exchange((200, {k: v for k, v in INDIA.items() if k != "refresh_token"}))
        self.assertEqual(rc, 1)
        self.assertIn("access_type=offline", screen.text)

    def test_the_redirect_address_is_trimmed_and_shown_before_the_exchange(self):
        rc, accounts, screen = self.exchange((200, INDIA), argv=("--redirect-uri", " %s\n" % REDIRECT))
        self.assertEqual(rc, 0)
        self.assertEqual(accounts.calls[0]["form"]["redirect_uri"], REDIRECT)
        self.assertEqual(screen.lines[0], "Redirect address: %s" % REDIRECT)

    def test_an_empty_redirect_address_is_refused_before_any_secret(self):
        screen = Screen()
        accounts = FakeAccounts()
        rc = load("exchange_code").main(["--redirect-uri", "  "], ask=never, http=accounts, out=screen.out)
        self.assertEqual(rc, 1)
        self.assertEqual(accounts.calls, [])
        self.assertIn("registered on the client, character for character", screen.text)

    def test_a_refused_code_says_the_redirect_address_must_match_exactly(self):
        _, _, screen = self.exchange((200, {"error": "invalid_redirect_uri"}))
        self.assertIn("error=invalid_redirect_uri", screen.text)
        self.assertIn("Redirect address: %s" % REDIRECT, screen.text)
        self.assertIn("registered on the client, character for character", screen.text)
        # A Self Client has no redirect address, so nothing is said about one.
        _, _, screen = self.exchange((200, {"error": "invalid_code"}), argv=("--self-client",))
        self.assertNotIn("redirect", screen.text.lower())

    def test_a_network_error_names_its_class_and_says_to_try_again_not_that_the_code_was_refused(self):
        for failure in (ZohoUnavailable("Zoho could not be reached (URLError); nothing was sent", error="network"),
                        ZohoUnknownOutcome("a write to Zoho had no answer (TimeoutError)", error="timeout")):
            with self.subTest(failure=type(failure).__name__):
                rc, accounts, screen = self.exchange(failure)
                self.assertEqual(rc, 1)
                self.assertEqual(len(accounts.calls), 1)
                self.assertIn(type(failure).__name__, screen.text)
                self.assertIn("error=%s" % failure.error, screen.text)
                self.assertIn("run exchange_code.py again", screen.text)
                self.assertNotIn("refused", screen.text)

    def test_a_server_error_says_to_try_again_not_that_the_code_was_refused(self):
        for status, body in ((500, {}), (503, {"error": "Service Unavailable"}), (502, None)):
            with self.subTest(status=status):
                rc, _, screen = self.exchange((status, body or {}))
                self.assertEqual(rc, 1)
                self.assertIn("Zoho answered %d, a server error" % status, screen.text)
                self.assertIn("run exchange_code.py again", screen.text)
                self.assertNotIn("refused", screen.text)
                self.assertNotIn("India", screen.text)


class IndiaAndScopesTests(unittest.TestCase):
    def test_only_the_india_data_centre_passes(self):
        self.assertTrue(_common.says_india({"api_domain": "https://www.zohoapis.in"}))
        self.assertTrue(_common.says_india({"api_domain": "https://www.zohoapis.in", "location": "in"}))
        for answer in ({"api_domain": "https://www.zohoapis.com"}, {"api_domain": "https://www.zohoapis.eu"},
                       {"api_domain": "https://www.zohoapis.in.example.com"}, {"location": "us"},
                       {"api_domain": "https://www.zohoapis.in", "location": "eu"}, {}):
            self.assertFalse(_common.says_india(answer), answer)

    def test_granted_scopes_with_spaces_or_commas(self):
        self.assertEqual(_common.granted_scopes({"scope": "Desk.tickets.READ Desk.basic.READ"}),
                         ["Desk.tickets.READ", "Desk.basic.READ"])
        self.assertEqual(_common.granted_scopes({"scope": "Desk.tickets.READ,Desk.basic.READ"}),
                         ["Desk.tickets.READ", "Desk.basic.READ"])
        self.assertEqual(_common.granted_scopes({}), [])


class AskSecretTests(unittest.TestCase):
    def test_an_empty_answer_stops_the_script(self):
        with self.assertRaises(SystemExit):
            _common.ask_secret("Zoho client secret: ", ask=lambda prompt: "  ")

    def test_the_answer_is_trimmed(self):
        self.assertEqual(_common.ask_secret("Zoho client id: ", ask=lambda prompt: " 1000.X \n"), "1000.X")


class RevokeTests(unittest.TestCase):
    """The chatbot shares the OMS's refresh token (Sachin's decision, 5 October
    2026), so revoking it would stop the OMS's ticketing and AFS dispatch. The
    script refuses unless told the token is a client of our own's."""

    def test_without_own_client_it_says_why_and_asks_for_nothing_and_posts_nothing(self):
        accounts = FakeAccounts()
        screen = Screen()
        rc = load("revoke").main([], ask=never, typed=never, http=accounts, out=screen.out)
        self.assertNotEqual(rc, 0)
        self.assertEqual(accounts.calls, [])
        text = " ".join(screen.text.split())
        for needle in ("shares the OMS's", "AFS dispatch", "EMOTORAD_ZOHO_REFRESH_TOKEN",
                       "docs/runbooks/config-store.md, section 7", "--own-client"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_docstring_says_it_refuses_without_own_client(self):
        doc = " ".join((load("revoke").__doc__ or "").split())
        self.assertIn("--own-client", doc)
        self.assertIn("shares the OMS's", doc)
        self.assertIn("never revoke", doc.lower())

    def test_the_token_goes_as_a_form_field_to_indias_revoke_address(self):
        accounts = FakeAccounts((200, {"status": "success"}))
        screen = Screen()
        rc = load("revoke").main(["--own-client"], ask=answers("1000.refresh.value"), typed=lambda prompt: "REVOKE",
                                 http=accounts, out=screen.out)
        self.assertEqual(rc, 0)
        [call] = accounts.calls
        self.assertEqual((call["method"], call["url"]), ("POST", "https://accounts.zoho.in/oauth/v2/revoke/token"))
        self.assertEqual(call["form"], {"token": "1000.refresh.value"})
        self.assertIs(call["classify"], False)
        self.assertNotIn("1000.refresh.value", call["url"])
        self.assertNotIn("1000.refresh.value", screen.text)

    def test_nothing_is_revoked_without_typing_revoke(self):
        accounts = FakeAccounts()
        rc = load("revoke").main(["--own-client"], ask=answers("1000.refresh.value"), typed=lambda prompt: "yes",
                                 http=accounts, out=Screen().out)
        self.assertEqual(rc, 1)
        self.assertEqual(accounts.calls, [])

    def test_a_refusal_names_the_error(self):
        screen = Screen()
        rc = load("revoke").main(["--own-client"], ask=answers("1000.refresh.value"), typed=lambda prompt: "REVOKE",
                                 http=FakeAccounts((200, {"error": "invalid_token"})), out=screen.out)
        self.assertEqual(rc, 1)
        self.assertIn("invalid_token", screen.text)


TICKET_ANSWER = {
    "id": TICKET_ID, "ticketNumber": "1201", "departmentId": "1892000000006907",
    "subject": "[AI chat] Battery: charging - EMX Plus [stage:EM-TEST-7]",
    "description": "Call Ananya on 9876543210 or ananya.rao@example.com",
    "email": "ananya.rao@example.com", "phone": "+919999999999", "priority": "Medium",
    "channel": "Chat", "status": "Open",
    "contact": {"id": "1892000000099001", "firstName": "Ananya", "lastName": "Rao",
                "email": "ananya.rao@example.com", "phone": "+919999999999", "mobile": "9876543210", "type": None},
    "assignee": {"id": "1892000000011111", "firstName": "Ravi", "lastName": "Kumar",
                 "email": "ravi.k@example.com", "name": "Ravi Kumar"},
    "createdTime": "2026-10-05T10:00:00.000Z",
}
# One ticket layout as Zoho describes it: the layout itself, then its fields.
LAYOUT = {"id": "9001", "layoutName": "Service layout", "isDefaultLayout": True, "status": "ACTIVE",
          "sections": [{"name": "Ticket Information", "fields": [
              {"apiName": "priority", "displayLabel": "Priority", "type": "Picklist", "isMandatory": False,
               "allowedValues": ["High", "Medium", "Low"]},
              {"apiName": "cf_dealer_principle_name", "displayLabel": "Dealer Principle Name", "type": "Picklist",
               "isCustomField": True, "allowedValues": ["Ravi Motors - D001"], "defaultValue": "Ravi Motors - D001"},
              {"apiName": "cf_warranty_status", "displayLabel": "Warranty Status", "type": "Picklist",
               "isCustomField": True, "isMandatory": True, "allowedValues": ["In warranty", "Out of warranty"]},
              {"apiName": "cf_account", "displayLabel": "Account", "type": "Picklist",
               "isCustomField": True, "isMandatory": True, "allowedValues": ["Kumar Cycles"]},
              {"apiName": "status", "displayLabel": "Status", "type": "Picklist",
               "allowedValues": [{"value": "Open", "id": "1"}]},
          ]}]}
LAYOUT_DETAILS = [LAYOUT]


def field(masked, api_name):
    """One field of a masked layout, by its API name."""
    for section in masked[0]["sections"]:
        for item in section["fields"]:
            if item["apiName"] == api_name:
                return item
    raise AssertionError("no field %s" % api_name)


class MaskingTests(unittest.TestCase):
    def test_no_phone_email_or_name_survives(self):
        written = json.dumps(_common.mask(TICKET_ANSWER))
        for personal in PERSONAL:
            self.assertNotIn(personal, written)

    def test_ids_numbers_and_enums_survive_and_every_key_stays(self):
        masked = _common.mask(TICKET_ANSWER)
        self.assertEqual(set(masked), set(TICKET_ANSWER))
        self.assertEqual(masked["id"], TICKET_ID)
        self.assertEqual(masked["ticketNumber"], "1201")
        self.assertEqual((masked["priority"], masked["channel"], masked["status"]), ("Medium", "Chat", "Open"))
        # The chatbot's own subject is kept: it is how a ticket is adopted.
        self.assertEqual(masked["subject"], "[AI chat] Battery: charging - EMX Plus [stage:EM-TEST-7]")
        self.assertEqual(masked["contact"]["id"], "1892000000099001")
        self.assertEqual(masked["contact"]["firstName"], "<str>")
        self.assertIsNone(masked["contact"]["type"])
        self.assertEqual(masked["phone"], "<str>")

    def test_only_a_subject_the_chatbot_wrote_is_kept(self):
        for ours in ("[AI chat] SAFETY - bike not given [stage:EM-1000001]",
                     "[Unverified] [AI chat] Intake - bike not given [prod:EM-1000002]"):
            self.assertEqual(_common.mask({"subject": ours}), {"subject": ours})
        for theirs in ("Call Ananya Rao about her bike", "Re: [AI chat] Battery [stage:EM-1000001]",
                       " [AI chat] Battery [stage:EM-1000001]"):
            self.assertEqual(_common.mask({"subject": theirs}), {"subject": "<str>"}, theirs)
        # Inside a person's object nothing but the id and type is kept.
        self.assertEqual(_common.mask({"contact": {"subject": "[AI chat] x [stage:EM-1000001]"}}),
                         {"contact": {"subject": "<str>"}})

    def test_a_contact_answer_keeps_only_its_id_and_type(self):
        masked = _common.mask({"id": "5", "type": "END_USER", "name": "Ananya Rao", "firstName": "Ananya"}, person=True)
        self.assertEqual(masked, {"id": "5", "type": "END_USER", "name": "<str>", "firstName": "<str>"})

    def test_a_pick_list_of_people_is_counted_not_written(self):
        masked = _common.mask(LAYOUT_DETAILS)
        self.assertEqual(field(masked, "priority")["allowedValues"], ["High", "Medium", "Low"])
        self.assertEqual(field(masked, "cf_dealer_principle_name")["allowedValues"]["count"], 1)
        self.assertEqual(field(masked, "cf_dealer_principle_name")["defaultValue"], "<withheld>")
        self.assertEqual(field(masked, "status")["allowedValues"], [{"value": "Open", "id": "1"}])
        self.assertEqual(masked[0]["sections"][0]["name"], "Ticket Information")
        written = json.dumps(masked)
        self.assertNotIn("Ravi", written)

    def test_an_account_pick_list_is_counted_too(self):
        # An account is a customer or a dealer, so its values are not listed.
        masked = _common.mask(LAYOUT_DETAILS)
        self.assertEqual(field(masked, "cf_account")["allowedValues"]["count"], 1)
        self.assertNotIn("Kumar", json.dumps(masked))

    def test_a_product_or_model_name_pick_list_keeps_its_values(self):
        # B4: the support lead chooses the bot's value from the probe's list,
        # and "Product Name" is the first of the required fields.
        for api_name, label in (("cf_product_name", "Product Name"), ("cf_model_name", "Model Name"),
                                ("cf_issue_name", "Name of the issue"), ("cf_mechanical_issue", "Mechanical Issue"),
                                ("cf_technical_issue", "Technical Issue")):
            with self.subTest(label=label):
                masked = _common.mask({"apiName": api_name, "displayLabel": label, "type": "Picklist",
                                       "allowedValues": ["EMX Plus", "T-Rex Air"], "defaultValue": "EMX Plus"})
                self.assertEqual(masked["allowedValues"], ["EMX Plus", "T-Rex Air"])
                self.assertEqual(masked["defaultValue"], "EMX Plus")

    def test_a_pick_list_that_names_people_is_still_withheld(self):
        for api_name, label in (("cf_contact_name", "Contact Name"), ("firstName", "First Name"),
                                ("lastName", "Last Name"), ("cf_customer_name", "Customer Name"),
                                ("cf_dealer", "Dealer"), ("cf_franchise", "Franchise"), ("cf_agent", "Agent"),
                                ("cf_owner", "Owner"), ("cf_account", "Account"), ("cf_mobile", "Mobile"),
                                ("cf_phone", "Phone"), ("cf_email", "Email"), ("cf_address", "Address"),
                                ("cf_dealer_principle_name", "Dealer Principle Name"),
                                ("cf_full_name", "Full Name"), ("cf_contact_person", "Contact Person"),
                                ("cf_technician_name", "Technician Name"), ("cf_mechanic", "Mechanic"),
                                ("cf_rider_name", "Rider Name"), ("cf_employee", "Employee")):
            with self.subTest(label=label):
                masked = _common.mask({"apiName": api_name, "displayLabel": label, "type": "Picklist",
                                       "allowedValues": ["Ravi Kumar"], "defaultValue": "Ravi Kumar"})
                self.assertEqual(masked["allowedValues"]["count"], 1)
                self.assertEqual(masked["defaultValue"], "<withheld>")

    def test_a_layout_keeps_its_id_name_default_flag_and_status(self):
        masked = _common.mask(LAYOUT_DETAILS)[0]
        self.assertEqual((masked["id"], masked["layoutName"], masked["isDefaultLayout"], masked["status"]),
                         ("9001", "Service layout", True, "ACTIVE"))
        item = field(_common.mask(LAYOUT_DETAILS), "cf_warranty_status")
        self.assertEqual((item["type"], item["isMandatory"], item["isCustomField"]), ("Picklist", True, True))

    def test_tokens_are_never_written(self):
        masked = _common.mask(INDIA)
        self.assertEqual((masked["access_token"], masked["refresh_token"]), ("[secret]", "[secret]"))
        self.assertEqual(masked["api_domain"], "https://www.zohoapis.in")
        self.assertEqual(masked["expires_in"], 3600)

    def test_a_saved_shape_carries_its_source_and_round_trips_department_names(self):
        with tempfile.TemporaryDirectory() as shapes:
            path = _common.save_shape("departments", DEPARTMENTS, "captured in a test", Path(shapes))
            self.assertEqual(path.name, "zoho-departments.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["_source"], "captured in a test")
            self.assertEqual(_common.department_names(Path(shapes)), {"111": TEST_DEPARTMENT, "222": "Service"})
            empty = _common.save_shape("contact-search", None, "a 204", Path(shapes))
            self.assertEqual(json.loads(empty.read_text(encoding="utf-8")), {"_source": "a 204", "body": None})


class ProbeOutputTests(unittest.TestCase):
    def setUp(self):
        self.probe = load("probe")

    def lines(self):
        return "\n".join(self.probe.layout_lines(_common.mask(LAYOUT)))

    def test_a_layout_is_introduced_by_its_id_name_and_default_flag(self):
        self.assertIn('layout 9001 "Service layout": the default layout', self.lines())
        other = dict(LAYOUT, isDefaultLayout=False)
        self.assertIn("not the default layout", "\n".join(self.probe.layout_lines(_common.mask(other))))

    def test_field_lines_show_api_name_label_type_flags_and_kept_values_only(self):
        text = self.lines()
        self.assertRegex(text, r"priority\s+Priority\s+Picklist\s+values: High, Medium, Low")
        self.assertRegex(text, r"cf_warranty_status\s+Warranty Status\s+Picklist\s+mandatory custom "
                               r"values: In warranty, Out of warranty")
        self.assertEqual(text.count("1 values withheld"), 2)
        self.assertNotIn("Ravi", text)
        self.assertNotIn("Kumar", text)

    def test_the_required_custom_fields_are_summed_up(self):
        self.assertIn("2 required custom field(s): cf_warranty_status, cf_account", self.lines())
        plain = {"id": "9", "layoutName": "Plain", "sections": [{"name": "S", "fields": [
            {"apiName": "subject", "displayLabel": "Subject", "isSystemMandatory": True}]}]}
        self.assertIn("0 required custom field(s)", "\n".join(self.probe.layout_lines(_common.mask(plain))))

    def test_only_active_layouts_are_read(self):
        listed = [{"id": "1", "status": "ACTIVE"}, {"id": "2", "status": "active"}, {"id": "3"},
                  {"id": "4", "status": "INACTIVE"}, {"id": "5", "status": "DRAFT"}]
        self.assertEqual([row["id"] for row in self.probe.active(listed)], ["1", "2", "3"])

    def test_a_contact_is_described_without_its_number(self):
        self.assertEqual(self.probe.describe_contact({"id": "5", "phone": "+919999999999"}), "found, has a number")
        self.assertEqual(self.probe.describe_contact({"id": "6"}), "found, no number")
        self.assertEqual(self.probe.describe_contact(None), "not found")


def probe_routes(contact=None):
    """What the probe reads, as Desk answers it."""
    def layouts(query):
        if query.get("module") == "contacts":
            return 200, {"data": [{"id": "9100", "layoutName": "Contact layout", "isDefaultLayout": True,
                                   "status": "ACTIVE"}]}
        return 200, {"data": [{"id": "9001", "layoutName": "Service layout", "isDefaultLayout": True,
                               "status": "ACTIVE"},
                              {"id": "9002", "layoutName": "Old layout", "isDefaultLayout": False,
                               "status": "INACTIVE"}]}

    return {
        ("POST", "/oauth/v2/token"): (200, INDIA),
        ("GET", "/api/v1/organizations"): (200, {"data": [{"id": "60001234567", "companyName": "EMotorad",
                                                            "edition": "PROFESSIONAL"}]}),
        ("GET", "/api/v1/departments"): (200, DEPARTMENTS),
        ("GET", "/api/v1/layouts"): layouts,
        ("GET", "/api/v1/layouts/9001"): (200, LAYOUT),
        ("GET", "/api/v1/layouts/9100"): (200, {"id": "9100", "layoutName": "Contact layout", "sections": [
            {"name": "Contact Information", "fields": [
                {"apiName": "lastName", "displayLabel": "Last Name", "type": "Text", "isSystemMandatory": True}]}]}),
        ("GET", "/api/v1/channels"): (200, {"data": [{"name": "Chat"}, {"name": "Email"}]}),
        ("GET", "/api/v1/contacts/%s" % CONTACT_ID): (200, contact or {
            "id": CONTACT_ID, "firstName": "Ananya", "lastName": "Rao", "email": "ananya.rao@example.com",
            "phone": "+919999999999", "mobile": "9876543210", "type": None}),
    }


class ProbeRunTests(unittest.TestCase):
    ARGS = ["--org-id", "60001234567", "--test-department-id", "111", "--test-contact-id", CONTACT_ID,
            "--department-id", "222"]

    def run_probe(self, routes=None, argv=None):
        http = FakeHTTP(routes or probe_routes())
        screen = Screen()
        shapes = tempfile.TemporaryDirectory()
        self.addCleanup(shapes.cleanup)
        ask = answers("1000.TESTCLIENT", "test-client-secret", "1000.refresh.value")
        rc = load("probe").main(argv or self.ARGS, ask=ask, http=http, out=screen.out, shapes_dir=Path(shapes.name))
        return rc, http, screen, Path(shapes.name)

    def test_it_reads_both_departments_layouts_and_writes_masked_shapes(self):
        rc, http, screen, shapes = self.run_probe()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(path.name for path in shapes.iterdir()), [
            "zoho-channels.json", "zoho-contact-layout.json", "zoho-contacts.json", "zoho-departments.json",
            "zoho-organizations.json", "zoho-ticket-layouts-real.json", "zoho-ticket-layouts-test.json",
            "zoho-token.json"])
        self.assertEqual({call["query"].get("departmentId") for call in http.to("GET", "/api/v1/layouts")
                          if call["query"].get("module") == "tickets"}, {"111", "222"})
        self.assertIn("test department 111: %s" % TEST_DEPARTMENT, screen.text)
        self.assertIn("real department 222: Service", screen.text)
        self.assertIn("API credits left today: 4321", screen.text)

    def test_every_active_layout_is_listed_and_an_inactive_one_is_not_read(self):
        _, http, screen, _ = self.run_probe()
        self.assertEqual(screen.text.count('layout 9001 "Service layout": the default layout'), 2)
        self.assertEqual(screen.text.count("2 required custom field(s): cf_warranty_status, cf_account"), 2)
        self.assertEqual(http.to("GET", "/api/v1/layouts/9002"), [])

    def test_the_saved_layout_shape_holds_the_layout_id_default_flag_and_fields(self):
        _, _, _, shapes = self.run_probe()
        saved = json.loads((shapes / "zoho-ticket-layouts-test.json").read_text(encoding="utf-8"))
        [detail] = saved["details"]
        self.assertEqual((detail["id"], detail["isDefaultLayout"]), ("9001", True))
        self.assertEqual(field([detail], "cf_warranty_status")["allowedValues"], ["In warranty", "Out of warranty"])
        self.assertEqual(field([detail], "cf_dealer_principle_name")["allowedValues"]["count"], 1)

    def test_nothing_personal_or_secret_reaches_the_screen_or_a_saved_file(self):
        _, _, screen, shapes = self.run_probe()
        written = screen.text + "".join(path.read_text(encoding="utf-8") for path in shapes.iterdir())
        for private in PERSONAL + ("1000.access.value", "1000.refresh.value", "test-client-secret", "Ravi Motors"):
            self.assertNotIn(private, written)

    def test_missing_scopes_are_named(self):
        _, _, screen, _ = self.run_probe()
        self.assertIn("scopes MISSING: Desk.tickets.CREATE", screen.text)

    def test_the_token_is_asked_for_twice_at_most_and_the_accounts_answer_is_read_by_its_body(self):
        _, http, _, _ = self.run_probe()
        tokens = http.to("POST", "/oauth/v2/token")
        self.assertEqual(len(tokens), 2)
        self.assertTrue(all(call["classify"] is False for call in tokens))

    def test_the_unverified_contact_is_read_and_a_contact_id_that_is_not_a_number_is_skipped(self):
        routes = probe_routes()
        routes[("GET", "/api/v1/contacts/4000000000004")] = (200, {"id": "4000000000004", "type": None})
        _, http, screen, _ = self.run_probe(routes, self.ARGS + ["--unverified-contact-id", "4000000000004"])
        self.assertIn("unverified contact 4000000000004: found, no number", screen.text)
        _, http, screen, _ = self.run_probe(probe_routes(), self.ARGS + ["--unverified-contact-id", "x/../y"])
        self.assertIn("unverified contact id is not a Zoho id", screen.text)
        self.assertEqual([call["path"] for call in http.calls if "contacts/" in call["path"]
                          and "layouts" not in call["path"]], ["/api/v1/contacts/%s" % CONTACT_ID])

    def test_a_token_that_cannot_see_the_organisation_stops_it(self):
        routes = probe_routes()
        routes[("GET", "/api/v1/organizations")] = (200, {"data": [{"id": "1", "companyName": "Other"}]})
        rc, http, screen, _ = self.run_probe(routes)
        self.assertEqual(rc, 1)
        self.assertIn("cannot see organisation 60001234567", screen.text)
        self.assertEqual(http.to("GET", "/api/v1/departments"), [])

    def test_another_data_centre_stops_it_before_any_desk_call(self):
        routes = probe_routes()
        routes[("POST", "/oauth/v2/token")] = (200, dict(INDIA, api_domain="https://www.zohoapis.com"))
        rc, http, _, _ = self.run_probe(routes)
        self.assertEqual(rc, 1)
        self.assertEqual([call["host"] for call in http.calls], ["accounts.zoho.in"])


class ScriptClientTests(unittest.TestCase):
    def client(self, routes):
        http = FakeHTTP(routes)
        settings = _common.script_settings(client_id="test-id", client_secret="test-secret",
                                           refresh_token="test-token", org_id="60001234567")
        return _common.ScriptClient(settings, http), http

    def test_a_read_carries_the_desk_clients_headers_and_goes_to_the_india_desk(self):
        client, http = self.client({("POST", "/oauth/v2/token"): (200, {"access_token": "tok", "expires_in": 3600}),
                                    ("GET", "/api/v1/departments"): (200, {"data": []})})
        client.get("/api/v1/departments", {"limit": 100})
        [read] = http.to("GET", "/api/v1/departments")
        self.assertEqual(read["host"], "desk.zoho.in")
        self.assertEqual(read["query"], {"limit": "100"})
        self.assertEqual(read["headers"]["Authorization"], "Zoho-oauthtoken tok")
        self.assertEqual(read["headers"]["orgId"], "60001234567")
        self.assertIs(read["write"], False)

    def test_desk_answers_are_kept_for_the_shapes_and_token_answers_never(self):
        client, _ = self.client({("POST", "/oauth/v2/token"): (200, {"access_token": "tok", "expires_in": 3600}),
                                 ("GET", "/api/v1/departments"): (200, {"data": [{"id": "111"}]})})
        client.get("/api/v1/departments")
        self.assertEqual(client.answer("GET", "/api/v1/departments"), {"data": [{"id": "111"}]})
        self.assertIsNone(client.answer("POST", "/oauth/v2/token"))
        self.assertIsNone(client.answer("POST", "/api/v1/tickets"))

    def test_the_organisation_list_goes_without_an_organisation_id(self):
        client, http = self.client({("POST", "/oauth/v2/token"): (200, {"access_token": "tok", "expires_in": 3600}),
                                    ("GET", "/api/v1/organizations"): (200, {"data": []})})
        client.get("/api/v1/organizations", org=False)
        self.assertNotIn("orgId", http.to("GET", "/api/v1/organizations")[0]["headers"])

    def test_the_search_wildcard_stays_as_typed(self):
        client, http = self.client({("POST", "/oauth/v2/token"): (200, {"access_token": "tok", "expires_in": 3600}),
                                    ("GET", "/api/v1/contacts/search"): (204, None)})
        client.get("/api/v1/contacts/search", {"phone": "*9999999999"})
        [read] = http.to("GET", "/api/v1/contacts/search")
        self.assertEqual(read["query"], {"phone": "*9999999999"})

    def test_an_expired_token_is_refreshed_once_and_the_read_tried_once_more(self):
        client, http = self.client({
            ("POST", "/oauth/v2/token"): [(200, {"access_token": "one", "expires_in": 3600}),
                                          (200, {"access_token": "two", "expires_in": 3600})],
            ("GET", "/api/v1/channels"): [ZohoAuthExpired("expired", error="INVALID_OAUTH"),
                                          (200, {"data": [{"name": "Chat"}]})],
        })
        self.assertEqual(client.get("/api/v1/channels"), {"data": [{"name": "Chat"}]})
        self.assertEqual([call["headers"]["Authorization"] for call in http.to("GET", "/api/v1/channels")],
                         ["Zoho-oauthtoken one", "Zoho-oauthtoken two"])

    def test_a_second_expiry_is_raised(self):
        client, _ = self.client({
            ("POST", "/oauth/v2/token"): (200, {"access_token": "tok", "expires_in": 3600}),
            ("GET", "/api/v1/channels"): ZohoAuthExpired("expired", error="INVALID_OAUTH"),
        })
        with self.assertRaises(ZohoAuthExpired):
            client.get("/api/v1/channels")


class DepartmentGuardTests(unittest.TestCase):
    NAMES = {"111": TEST_DEPARTMENT, "222": "Service"}

    def refusal(self, department, real=False, typed=TEST_DEPARTMENT, test="111"):
        return _common.department_refusal(self.NAMES, department, test, real, typed)

    def test_the_configured_test_department_is_allowed_when_its_name_is_typed(self):
        self.assertIsNone(self.refusal("111"))
        self.assertIsNone(self.refusal("111", typed="  %s  " % TEST_DEPARTMENT))

    def test_the_test_department_needs_its_exact_name_typed(self):
        for typed in ("", None, "inkodop technologies pvt.ltd", "Service"):
            self.assertIsNotNone(self.refusal("111", typed=typed), typed)

    def test_any_other_department_is_refused_without_the_flag_whatever_is_typed(self):
        message = self.refusal("222", typed="Service")
        self.assertIn("not the configured test department", message)
        self.assertIn("111", message)

    def test_a_department_the_probe_never_saw_is_refused(self):
        self.assertIn("probe.py", self.refusal("333"))
        self.assertIn("probe.py", _common.department_refusal({}, "111", "111", False, TEST_DEPARTMENT))

    def test_the_real_department_needs_its_exact_name_typed(self):
        self.assertIsNone(self.refusal("222", real=True, typed="Service"))
        self.assertIsNotNone(self.refusal("222", real=True, typed="service"))
        self.assertIsNotNone(self.refusal("222", real=True, typed=""))
        self.assertIsNotNone(self.refusal("222", real=True, typed=None))

    def test_the_real_department_flag_never_names_the_test_one(self):
        self.assertIn("test department", self.refusal("111", real=True, typed=TEST_DEPARTMENT))

    def test_ids_are_compared_as_text(self):
        self.assertIsNone(_common.department_refusal(self.NAMES, 111, "111", False, TEST_DEPARTMENT))

    def test_the_message_never_repeats_what_was_typed(self):
        self.assertNotIn("a secret-looking name", self.refusal("111", typed="a secret-looking name"))

    def test_no_department_name_is_written_into_a_script(self):
        for name in SCRIPTS:
            text = (ZOHO_SCRIPTS / ("%s.py" % name)).read_text(encoding="utf-8")
            for written in ("Inkodop", "AI chatbot test"):
                self.assertNotIn(written, text, name)

    def test_the_script_refuses_before_asking_for_any_secret(self):
        cases = (
            # Not the configured test department, however its name is typed.
            ("222", False, "Service"),
            # The real-department flag on the test department.
            ("111", True, TEST_DEPARTMENT),
            # The real department without its name.
            ("222", True, "service"),
            # The test department without its name.
            ("111", False, "wrong"),
            # A department the probe never saw.
            ("333", False, "anything"),
        )
        for department, real, typed in cases:
            with self.subTest(department=department, real=real, typed=typed):
                with tempfile.TemporaryDirectory() as shapes:
                    _common.save_shape("departments", DEPARTMENTS, "test", Path(shapes))
                    screen = Screen()
                    accounts = FakeAccounts()
                    argv = TICKET_ARGS + ["--department-id", department] + (["--real-department"] if real else [])
                    rc = load("test_ticket").main(argv, ask=never, typed=lambda prompt, typed=typed: typed,
                                                  http=accounts, out=screen.out, shapes_dir=Path(shapes))
                self.assertEqual(rc, 1)
                self.assertTrue(screen.lines)
                self.assertEqual(accounts.calls, [])

    def test_the_name_is_always_asked_for(self):
        prompts = []
        with tempfile.TemporaryDirectory() as shapes:
            _common.save_shape("departments", DEPARTMENTS, "test", Path(shapes))
            for real, department in ((False, "111"), (True, "222")):
                load("test_ticket").main(
                    TICKET_ARGS + ["--department-id", department] + (["--real-department"] if real else []),
                    ask=never, typed=lambda prompt: prompts.append(prompt) or "", http=FakeAccounts(),
                    out=Screen().out, shapes_dir=Path(shapes))
        self.assertEqual(len(prompts), 2)

    def test_a_guard_that_passes_goes_on_to_ask_for_the_secrets(self):
        class Reached(Exception):
            pass

        def ask(prompt):
            raise Reached(prompt)

        with tempfile.TemporaryDirectory() as shapes:
            _common.save_shape("departments", DEPARTMENTS, "test", Path(shapes))
            with self.assertRaises(Reached):
                load("test_ticket").main(TICKET_ARGS + ["--department-id", "111"], ask=ask,
                                         typed=lambda prompt: TEST_DEPARTMENT, http=FakeAccounts(),
                                         out=Screen().out, shapes_dir=Path(shapes))


class LookUpTests(unittest.TestCase):
    REFERENCE = "stage:EM-TEST-7"

    def setUp(self):
        self.script = load("test_ticket")
        self.sleeps = []

    def look(self, *listings):
        desk = FakeDesk(*listings)
        found = self.script.look_up(desk, CONTACT_ID, "111", self.REFERENCE,
                                    sleep=self.sleeps.append, out=Screen().out)
        return desk, found

    def test_found_at_once_makes_no_second_ticket(self):
        desk, found = self.look([zoho_ticket(self.REFERENCE)], [zoho_ticket(self.REFERENCE)])
        self.assertEqual([t["ticketNumber"] for t in found], ["1201", "1201"])
        self.assertFalse(self.script.needs_second_create(found))
        self.assertEqual(self.sleeps, [120])
        self.assertEqual(desk.calls, [(CONTACT_ID, "111"), (CONTACT_ID, "111")])

    def test_found_only_after_two_minutes_makes_no_second_ticket(self):
        _, found = self.look([], [zoho_ticket(self.REFERENCE)])
        self.assertIsNone(found[0])
        self.assertFalse(self.script.needs_second_create(found))

    def test_never_found_makes_a_second_ticket(self):
        _, found = self.look([], [])
        self.assertTrue(self.script.needs_second_create(found))

    def test_a_near_miss_reference_is_not_adopted(self):
        _, found = self.look([zoho_ticket("stage:EM-TEST-70")], [zoho_ticket("stage:EM-TEST-70")])
        self.assertEqual(found, [None, None])
        self.assertTrue(self.script.needs_second_create(found))

    def test_the_reference_has_to_end_the_subject(self):
        elsewhere = dict(zoho_ticket(self.REFERENCE), subject="[%s] [AI chat] Battery" % self.REFERENCE)
        _, found = self.look([elsewhere], [elsewhere])
        self.assertEqual(found, [None, None])

    def test_a_ticket_list_with_no_subjects_is_not_adopted(self):
        bare = {key: value for key, value in zoho_ticket(self.REFERENCE).items() if key != "subject"}
        _, found = self.look([bare], [bare])
        self.assertEqual(found, [None, None])


class TestTicketShapeTests(unittest.TestCase):
    def setUp(self):
        self.script = load("test_ticket")

    def settings(self, live=False, department="111", **extra):
        return _common.script_settings(
            client_id="test-id", client_secret="test-secret", refresh_token="test-token", org_id="60001234567",
            department_id=department, contact_id="C1", live=live, **extra)

    def test_the_test_ticket_is_built_as_the_worker_builds_one(self):
        record = self.script.fake_record(7, "stage", "test")
        self.assertEqual((record["_id"], record["chat_reference"], record["mode"]),
                         ("EM-TEST-7", "stage:EM-TEST-7", "test"))
        payload = ticket_payload(record, self.settings(), "C1")
        self.assertEqual(str(payload["departmentId"]), "111")
        self.assertEqual(payload["contactId"], "C1")
        self.assertEqual(self.script.FRAME_NUMBER, "EMXP2025004417")
        self.assertIn(self.script.FRAME_NUMBER, payload["description"])

    def test_the_chat_reference_ends_the_subject_and_no_custom_field_is_sent(self):
        record = self.script.fake_record(7, "stage", "test")
        payload = ticket_payload(record, self.settings(), "C1")
        self.assertTrue(payload["subject"].startswith("[AI chat] "))
        self.assertTrue(payload["subject"].endswith(" [stage:EM-TEST-7]"))
        self.assertNotIn("cf", payload)
        self.assertNotIn("customFields", payload)
        self.assertNotIn("layoutId", payload)

    def test_a_layout_is_sent_only_when_one_is_chosen(self):
        record = self.script.fake_record(7, "stage", "test")
        payload = ticket_payload(record, self.settings(layout_id="555"), "C1")
        self.assertEqual(payload["layoutId"], "555")

    def test_a_real_department_ticket_goes_to_the_department_named(self):
        record = self.script.fake_record(8, "stage", "live")
        payload = ticket_payload(record, self.settings(live=True, department="222"), "C1")
        self.assertEqual(str(payload["departmentId"]), "222")

    def test_the_small_image_is_a_real_png(self):
        data = self.script.tiny_png()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", data[16:24]), (8, 8))
        idat_length = struct.unpack(">I", data[33:37])[0]
        self.assertEqual(data[37:41], b"IDAT")
        self.assertEqual(len(zlib.decompress(data[41:41 + idat_length])), 8 * (8 + 1))

    def test_a_filler_file_has_the_size_asked_for(self):
        self.assertEqual(len(self.script.filler(1)), 1024 * 1024)

    def test_the_uploads_start_with_the_reference_and_straddle_twenty_megabytes(self):
        sizes = [(name, len(data)) for name, data, _ in self.script.uploads("EM-TEST-7")]
        self.assertEqual([name for name, _ in sizes],
                         ["EM-TEST-7-small.png", "EM-TEST-7-19mb.bin", "EM-TEST-7-26mb.bin"])
        self.assertLess(sizes[1][1], 20 * 1024 * 1024)
        self.assertGreater(sizes[2][1], 20 * 1024 * 1024)


class TestTicketRunTests(unittest.TestCase):
    """The whole script, with Desk answered by FakeHTTP."""

    SECOND = "1892000000123002"

    def setUp(self):
        self.script = load("test_ticket")
        self.shapes = tempfile.TemporaryDirectory()
        self.addCleanup(self.shapes.cleanup)
        _common.save_shape("departments", DEPARTMENTS, "test", Path(self.shapes.name))
        self.sleeps = []

    def ticket_read_back(self, description=None):
        return (200, dict(
            zoho_ticket("stage:EM-TEST-7"),
            description=description or "Reference: EM-TEST-7\nSource: AI chatbot\nBike: EMX Plus, frame number %s"
                                       % self.script.FRAME_NUMBER))

    def routes(self, replace=None):
        base = {
            ("POST", "/oauth/v2/token"): (200, {"access_token": "tok", "expires_in": 3600}),
            ("GET", "/api/v1/contacts/search"): (200, {"data": [{"id": CONTACT_ID, "mobile": "+919999999999"}]}),
            ("POST", "/api/v1/tickets"): (200, {"id": TICKET_ID, "ticketNumber": "1201",
                                                "webUrl": "https://desk.zoho.in/agent/x/tickets/1"}),
            ("GET", "/api/v1/tickets/%s" % TICKET_ID): self.ticket_read_back(),
            ("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID): (200, {"data": [zoho_ticket("stage:EM-TEST-7")]}),
            ("POST", "/api/v1/tickets/%s/comments" % TICKET_ID): (200, {"id": "77"}),
            ("GET", "/api/v1/tickets/%s/comments/77" % TICKET_ID): (200, {"id": "77", "isPublic": False}),
            ("POST", "/api/v1/tickets/%s/attachments" % TICKET_ID): (200, {"id": "88"}),
            ("GET", "/api/v1/tickets/%s/attachments" % TICKET_ID): (200, {"data": [{"id": "88"}]}),
        }
        base.update(replace or {})
        return base

    def run_script(self, routes, department="111", real=False, typed=None):
        http = FakeHTTP(routes)
        screen = Screen()
        argv = TICKET_ARGS + ["--department-id", department, "--n", "7"] + (["--real-department"] if real else [])
        name = typed or (("Service" if real else TEST_DEPARTMENT))
        small_only = lambda reference: [("%s-small.png" % reference, self.script.tiny_png(), "image/png")]  # noqa: E731
        with mock.patch.object(self.script, "uploads", small_only):
            rc = self.script.main(argv, ask=answers("1000.TESTCLIENT", "test-client-secret", "1000.refresh.value"),
                                  typed=lambda prompt: name, http=http, out=screen.out,
                                  sleep=self.sleeps.append, shapes_dir=Path(self.shapes.name))
        return rc, http, screen

    def posted(self, http):
        return [json.loads(call["body"].decode("utf-8")) for call in http.to("POST", "/api/v1/tickets")]

    def test_the_first_ticket_carries_no_custom_field_and_ends_its_subject_with_the_reference(self):
        rc, http, _ = self.run_script(self.routes())
        self.assertEqual(rc, 0)
        [payload] = self.posted(http)
        self.assertNotIn("cf", payload)
        self.assertTrue(payload["subject"].endswith(" [stage:EM-TEST-7]"))
        self.assertEqual(payload["departmentId"], "111")
        self.assertEqual(payload["contactId"], CONTACT_ID)

    def test_the_read_back_checks_the_subject_the_frame_number_and_the_lines(self):
        _, _, screen = self.run_script(self.routes())
        self.assertIn("chat reference ends the subject: yes", screen.text)
        self.assertIn("frame number in the description: yes", screen.text)
        self.assertIn("lines kept in the description: yes", screen.text)
        self.assertIn("subjects in the contact's ticket list: yes", screen.text)

    def test_a_description_read_back_with_its_lines_run_together_is_reported(self):
        flat = self.ticket_read_back("Reference: EM-TEST-7 Source: AI chatbot Bike: EMX Plus")
        _, _, screen = self.run_script(self.routes({("GET", "/api/v1/tickets/%s" % TICKET_ID): flat}))
        self.assertIn("lines kept in the description: NO", screen.text)
        self.assertIn("frame number in the description: NO", screen.text)

    def test_a_subject_read_back_without_the_reference_is_reported(self):
        other = (200, dict(zoho_ticket("stage:EM-TEST-9"), description="x"))
        _, _, screen = self.run_script(self.routes({("GET", "/api/v1/tickets/%s" % TICKET_ID): other}))
        self.assertIn("chat reference ends the subject: NO", screen.text)

    def test_a_contact_ticket_list_with_no_subjects_is_reported(self):
        bare = {key: value for key, value in zoho_ticket("stage:EM-TEST-7").items() if key != "subject"}
        _, _, screen = self.run_script(self.routes({
            ("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID): (200, {"data": [bare]})}))
        self.assertIn("subjects in the contact's ticket list: NO", screen.text)

    def test_found_by_the_look_up_makes_no_second_ticket_and_the_rest_runs(self):
        rc, http, screen = self.run_script(self.routes())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.posted(http)), 1)
        self.assertEqual(self.sleeps, [120])
        self.assertIn("no second ticket was made", screen.text)
        self.assertEqual(len(http.to("POST", "/api/v1/tickets/%s/comments" % TICKET_ID)), 1)
        self.assertEqual(len(http.to("POST", "/api/v1/tickets/%s/attachments" % TICKET_ID)), 1)
        saved = sorted(path.name for path in Path(self.shapes.name).iterdir())
        for name in ("zoho-ticket.json", "zoho-contact-tickets.json", "zoho-contact-search.json",
                     "zoho-comment.json", "zoho-attachment.json", "zoho-attachments.json"):
            self.assertIn(name, saved)
        self.assertIn("Close ticket #1201 in Desk now.", screen.text)

    def test_the_first_uploads_own_answer_is_the_attachment_shape(self):
        routes = self.routes({("POST", "/api/v1/tickets/%s/attachments" % TICKET_ID): (200, {"id": "88", "size": "75"}),
                              ("GET", "/api/v1/tickets/%s/attachments" % TICKET_ID): (200, {"data": [{"id": "1"}]})})
        self.run_script(routes)
        folder = Path(self.shapes.name)
        attachment = json.loads((folder / "zoho-attachment.json").read_text(encoding="utf-8"))
        self.assertEqual((attachment["id"], attachment["size"]), ("88", "75"))
        listed = json.loads((folder / "zoho-attachments.json").read_text(encoding="utf-8"))
        self.assertEqual(listed["data"], [{"id": "1"}])

    def test_an_empty_list_leaves_the_file_already_there(self):
        folder = Path(self.shapes.name)
        for name in ("contact-search", "contact-tickets"):
            (folder / ("zoho-%s.json" % name)).write_text('{"_source": "the draft"}', encoding="utf-8")
        routes = self.routes({("GET", "/api/v1/contacts/search"): (204, None),
                              ("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID): [
                                  (204, None), (200, {"data": [zoho_ticket("stage:EM-TEST-7")]})]})
        rc, _, screen = self.run_script(routes)
        self.assertEqual(rc, 0)
        for name in ("contact-search", "contact-tickets"):
            with self.subTest(name=name):
                self.assertEqual((folder / ("zoho-%s.json" % name)).read_text(encoding="utf-8"),
                                 '{"_source": "the draft"}')
                self.assertIn("Zoho listed nothing for zoho-%s.json, so the file already there is kept." % name,
                              screen.text)

    def test_never_found_makes_a_second_ticket_in_the_test_department(self):
        empty = (200, {"data": []})
        routes = self.routes({("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID): empty,
                                ("POST", "/api/v1/tickets"): [
                                    (200, {"id": TICKET_ID, "ticketNumber": "1201", "webUrl": None}),
                                    (200, {"id": self.SECOND, "ticketNumber": "1202", "webUrl": None})]})
        rc, http, screen = self.run_script(routes)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.posted(http)), 2)
        self.assertIn("a second ticket was made (#1202)", screen.text)

    def test_the_real_department_makes_one_ticket_and_stops_after_the_look_up(self):
        empty = (200, {"data": []})
        rc, http, screen = self.run_script(
            self.routes({("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID): empty}), department="222", real=True)
        self.assertEqual(rc, 0)
        [payload] = self.posted(http)
        self.assertEqual(payload["departmentId"], "222")
        self.assertIn("No second ticket in the real department", screen.text)
        self.assertEqual(http.to("POST", "/api/v1/tickets/%s/comments" % TICKET_ID), [])
        self.assertEqual(http.to("POST", "/api/v1/tickets/%s/attachments" % TICKET_ID), [])
        self.assertIn("Close #1201 in Desk", screen.text)

    def test_nothing_secret_is_printed(self):
        _, _, screen = self.run_script(self.routes())
        for secret in ("1000.TESTCLIENT", "test-client-secret", "1000.refresh.value", "tok"):
            self.assertNotRegex(screen.text, r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(secret))

    def test_zoho_enforcing_required_fields_stops_after_naming_them(self):
        rejected = ZohoRejected("Zoho answered 422 INVALID_DATA naming cf_product_name, cf_location",
                                error="INVALID_DATA", fields=("cf_product_name", "cf_location"))
        rc, http, screen = self.run_script(self.routes({("POST", "/api/v1/tickets"): rejected}))
        self.assertEqual(rc, 1)
        self.assertIn("error=INVALID_DATA", screen.text)
        self.assertIn("cf_product_name, cf_location", screen.text)
        self.assertIn("Zoho enforces the layout's required fields: the support lead must choose a value for "
                      "each before any further part ships.", screen.text)
        self.assertNotIn("part C", screen.text)
        # Nothing was made, so nothing is read back, looked up or attached.
        self.assertEqual(len(self.posted(http)), 1)
        self.assertEqual(http.to("GET", "/api/v1/tickets/%s" % TICKET_ID), [])
        self.assertEqual(http.to("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID), [])
        self.assertNotIn("Close ticket", screen.text)

    def test_the_refusal_is_saved_as_error_code_and_field_names_only(self):
        rejected = ZohoRejected("x", error="INVALID_DATA", fields=("cf_product_name", "cf_location"))
        self.run_script(self.routes({("POST", "/api/v1/tickets"): rejected}))
        saved = json.loads((Path(self.shapes.name) / "zoho-ticket-rejected.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["errorCode"], "INVALID_DATA")
        self.assertEqual([item["fieldName"] for item in saved["errors"]], ["cf_product_name", "cf_location"])
        self.assertIn("_source", saved)

    def test_a_refusal_naming_only_fields_we_sent_is_not_a_layout_finding(self):
        rejected = ZohoRejected("x", error="INVALID_DATA", fields=("contactId",))
        rc, _, screen = self.run_script(self.routes({("POST", "/api/v1/tickets"): rejected}))
        self.assertEqual(rc, 1)
        self.assertIn("contactId", screen.text)
        self.assertNotIn("Zoho enforces the layout's required fields", screen.text)
        self.assertIn("Nothing was created", screen.text)

    def test_a_create_with_no_answer_says_a_ticket_may_exist_and_how_to_find_it(self):
        unknown = ZohoUnknownOutcome("a write to Zoho had no answer (TimeoutError)", error="timeout")
        rc, http, screen = self.run_script(self.routes({("POST", "/api/v1/tickets"): unknown}))
        self.assertEqual(rc, 1)
        self.assertIn("error=timeout", screen.text)
        self.assertIn("may have been created", screen.text)
        self.assertIn("[stage:EM-TEST-7]", screen.text)
        self.assertNotIn("#None", screen.text)
        # Nothing more is sent after a create nobody confirmed.
        self.assertEqual(len(self.posted(http)), 1)
        self.assertEqual(http.to("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID), [])

    def test_a_second_create_with_no_answer_names_the_first_and_the_reference(self):
        unknown = ZohoUnknownOutcome("a write to Zoho had no answer (TimeoutError)", error="timeout")
        routes = self.routes({("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID): (200, {"data": []}),
                              ("POST", "/api/v1/tickets"): [
                                  (200, {"id": TICKET_ID, "ticketNumber": "1201", "webUrl": None}), unknown]})
        rc, _, screen = self.run_script(routes)
        self.assertEqual(rc, 1)
        self.assertIn("may have been created", screen.text)
        self.assertIn("[stage:EM-TEST-7]", screen.text)
        self.assertIn("#1201", screen.text)

    def test_a_failure_after_the_ticket_exists_says_which_one_to_close(self):
        failing = ZohoRejected("x", error="INVALID_DATA", fields=("content",))
        rc, _, screen = self.run_script(self.routes({("POST", "/api/v1/tickets/%s/comments" % TICKET_ID): failing}))
        self.assertEqual(rc, 1)
        self.assertIn("Close ticket #1201 in Desk.", screen.text)

    def test_a_contact_id_that_is_not_a_number_is_refused_before_any_secret(self):
        screen = Screen()
        argv = ["--org-id", "60001234567", "--contact-id", "C1/../x", "--test-department-id", "111",
                "--department-id", "111"]
        rc = self.script.main(argv, ask=never, typed=lambda prompt: TEST_DEPARTMENT, http=FakeAccounts(),
                              out=screen.out, shapes_dir=Path(self.shapes.name))
        self.assertEqual(rc, 1)
        self.assertIn("--contact-id is not a Zoho id", screen.text)

    def test_a_chosen_layout_goes_on_the_ticket(self):
        http = FakeHTTP(self.routes())
        with mock.patch.object(self.script, "uploads", lambda reference: []):
            self.script.main(TICKET_ARGS + ["--department-id", "111", "--n", "7", "--layout-id", "555"],
                             ask=answers("a", "b", "c"), typed=lambda prompt: TEST_DEPARTMENT, http=http,
                             out=Screen().out, sleep=self.sleeps.append, shapes_dir=Path(self.shapes.name))
        self.assertEqual(self.posted(http)[0]["layoutId"], "555")


class CapturedShapeTests(ShapeChecks, unittest.TestCase):
    """Part 1's captures replace the drafts the suite answers from, so they must
    pass the same checks (the final review, scripts-docs Important 1). probe.py
    and test_ticket.py run here against a Desk that answers with the committed
    drafts, writing into a copy of docs/api-shapes, and ShapeChecks runs again
    on what they wrote. The ticket read back also carries Zoho's "cf" object,
    as a real answer does, with a value that names a dealer."""

    SCRIPT_WRITES = ("zoho-ticket.json", "zoho-contact-search.json", "zoho-contact-tickets.json",
                     "zoho-comment.json", "zoho-attachment.json", "zoho-token.json")
    EARLIER_ATTACHMENT = "4000000008001"

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.shapes_dir = cls._folder.name
        for path in Path(DRAFTS).glob("zoho-*.json"):
            shutil.copy(path, cls.shapes_dir)
        token = dict(shape("zoho-token.json")["refresh"], api_domain="https://www.zohoapis.in",
                     scope=" ".join(_common.SCOPES))
        routes = probe_routes()
        routes[("POST", "/oauth/v2/token")] = (200, token)
        cls.probe_rc = load("probe").main(
            ["--org-id", "60001234567", "--test-department-id", "111", "--test-contact-id", CONTACT_ID],
            ask=answers("1000.TESTCLIENT", "test-client-secret", "1000.refresh.value"), http=FakeHTTP(routes),
            out=Screen().out, shapes_dir=Path(cls.shapes_dir))

        ticket = shape("zoho-ticket.json")
        comment = shape("zoho-comment.json")
        attachment = shape("zoho-attachment.json")
        read_back = dict(ticket, cf={"cf_dealer_principle_name": "Ravi Motors - D001"})
        on_ticket = "/api/v1/tickets/%s" % ticket["id"]
        script = load("test_ticket")
        routes = {
            ("POST", "/oauth/v2/token"): (200, token),
            ("GET", "/api/v1/contacts/search"): (200, shape("zoho-contact-search.json")),
            ("POST", "/api/v1/tickets"): (200, ticket),
            ("GET", on_ticket): (200, read_back),
            ("GET", "/api/v1/contacts/%s/tickets" % CONTACT_ID): (200, shape("zoho-contact-tickets.json")),
            ("POST", on_ticket + "/comments"): (200, comment),
            ("GET", "%s/comments/%s" % (on_ticket, comment["id"])): (200, comment),
            ("POST", on_ticket + "/attachments"): (200, attachment),
            # Another file first, so the list's first entry is not the upload's own answer.
            ("GET", on_ticket + "/attachments"): (200, {"data": [dict(attachment, id=cls.EARLIER_ATTACHMENT),
                                                                  attachment]}),
        }
        small_only = lambda reference: [("%s-small.png" % reference, script.tiny_png(), "image/png")]  # noqa: E731
        with mock.patch.object(script, "uploads", small_only):
            cls.ticket_rc = script.main(
                TICKET_ARGS + ["--department-id", "111", "--n", "7"],
                ask=answers("1000.TESTCLIENT", "test-client-secret", "1000.refresh.value"),
                typed=lambda prompt: TEST_DEPARTMENT, http=FakeHTTP(routes), out=Screen().out,
                sleep=lambda seconds: None, shapes_dir=Path(cls.shapes_dir))

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def test_both_scripts_ran_to_the_end(self):
        self.assertEqual((self.probe_rc, self.ticket_rc), (0, 0))

    def test_every_draft_a_script_replaces_is_now_a_capture_and_the_error_bodies_stay(self):
        for name in self.SCRIPT_WRITES:
            with self.subTest(name=name):
                self.assertTrue(self.captured(name))
        self.assertFalse(self.captured("zoho-errors.json"))
        self.assertEqual(self.raw("zoho-errors.json"), json.loads((Path(DRAFTS) / "zoho-errors.json").read_text(
            encoding="utf-8")))

    def test_the_token_capture_goes_under_refresh_beside_the_error_bodies(self):
        token = self.recorded("zoho-token.json")
        self.assertEqual(token["refresh"]["access_token"], "[secret]")
        self.assertEqual(token["refresh"]["api_domain"], "https://www.zohoapis.in")
        self.assertEqual(token["invalid_client_secret"], {"error": "invalid_client_secret"})
        # The error bodies keep the note that says where they came from.
        self.assertIn("em-biz-backend", self.raw("zoho-token.json")["_source_of_the_rest"])

    def test_the_upload_answer_is_the_attachment_and_the_list_is_kept_apart(self):
        ours = shape("zoho-attachment.json")["id"]
        self.assertEqual(self.recorded("zoho-attachment.json")["id"], ours)
        self.assertEqual([item["id"] for item in self.recorded("zoho-attachments.json")["data"]],
                         [self.EARLIER_ATTACHMENT, ours])

    def test_our_subject_and_the_ticket_link_are_kept_and_nothing_personal_is(self):
        ticket = self.recorded("zoho-ticket.json")
        self.assertEqual(ticket["subject"], shape("zoho-ticket.json")["subject"])
        self.assertEqual(ticket["webUrl"], shape("zoho-ticket.json")["webUrl"])
        self.assertEqual(ticket["cf"], {"cf_dealer_principle_name": "<str>"})
        self.assertEqual(ticket["phone"], "<str>")
        written = "".join(path.read_text(encoding="utf-8") for path in Path(self.shapes_dir).iterdir())
        for private in PERSONAL + ("Ravi Motors", "1000.refresh.value", "test-client-secret"):
            self.assertNotIn(private, written)


class TicketsReportTests(unittest.TestCase):
    def setUp(self):
        self.report = load("tickets_report")

    def test_a_waiting_record_is_listed_with_the_last_four_digits_only(self):
        store = InMemoryTicketStore()
        reference = store.next_reference()
        now = now_iso()
        store.insert(new_record(
            reference=reference, chat_reference="stage:" + reference,
            source_key="c1:%s:create_support_ticket:k1" % now, mode="test", kind="support",
            conversation_id="c1", started_at=now, cluster_id=None, channel="website_chat",
            phone="+919999999999", identity="verified", category="battery_charging", ai_severity="normal",
            summary="The battery will not charge.", claims={}, bike=None, coverage=None, customer_name=None,
            created_at=now,
        ))
        text = "\n".join(self.report.lines(store.listing("test")))
        self.assertIn(reference, text)
        self.assertIn("waiting", text)
        self.assertIn("...9999", text)
        self.assertNotIn("9999999999", text)
        self.assertIn("1 record(s)", text)

    def test_it_refuses_without_the_deployments_store(self):
        screen = Screen()
        with mock.patch.dict(os.environ, {"EMOTORAD_STORE": "memory"}):
            rc = self.report.main([], out=screen.out)
        self.assertEqual(rc, 1)
        self.assertIn("not mongodb", screen.text)

    def test_the_mode_is_the_deployments(self):
        with mock.patch.dict(os.environ, {"EMOTORAD_ZOHO_LIVE": "yes"}):
            self.assertEqual(self.report.parser().parse_args([]).mode, "live")
        with mock.patch.dict(os.environ, {"EMOTORAD_ZOHO_LIVE": "no"}):
            self.assertEqual(self.report.parser().parse_args([]).mode, "test")


class ScriptHygieneTests(unittest.TestCase):
    def test_every_script_says_who_runs_it_and_where(self):
        for name in SCRIPTS:
            tree = ast.parse((ZOHO_SCRIPTS / ("%s.py" % name)).read_text(encoding="utf-8"))
            doc = " ".join((ast.get_docstring(tree) or "").split())
            self.assertIn("Run by a person, never by a Claude session", doc, name)
            self.assertIn("outside the Claude app", doc, name)

    def test_no_script_holds_a_token(self):
        for name in SCRIPTS:
            text = (ZOHO_SCRIPTS / ("%s.py" % name)).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"1000\.[0-9a-f]{16,}", name)

    def test_no_script_knows_a_custom_field(self):
        for name in SCRIPTS:
            text = (ZOHO_SCRIPTS / ("%s.py" % name)).read_text(encoding="utf-8")
            for banned in ("cf_chat_reference", "cf_source", "--cf-", "CF_CHAT_REFERENCE", "CF_SOURCE"):
                self.assertNotIn(banned, text, name)


class DocsTests(unittest.TestCase):
    """The runbook and the contract say what the code does (spec sections 9 and 12)."""

    def setUp(self):
        self.runbook = (ROOT / "docs" / "runbooks" / "config-store.md").read_text(encoding="utf-8")
        self.contract = (ROOT / "docs" / "contracts" / "amiigo-support-chat.md").read_text(encoding="utf-8")

    def test_the_runbook_has_a_row_for_every_zoho_name_the_code_reads(self):
        for name in ENV_NAMES:
            with self.subTest(name=name):
                self.assertIn("| `%s` |" % name, self.runbook)

    def test_the_runbook_has_no_row_for_a_custom_field(self):
        self.assertNotIn("CF_CHAT_REFERENCE", self.runbook)
        self.assertNotIn("CF_SOURCE", self.runbook)
        self.assertNotIn("Chat reference", self.runbook)

    def test_the_runbook_names_the_test_department_by_id_and_the_layout_row(self):
        row = [line for line in self.runbook.splitlines() if line.startswith("| `EMOTORAD_ZOHO_TEST_DEPARTMENT_ID`")][0]
        self.assertIn("Inkodop technologies Pvt.Ltd", row)
        self.assertIn("by id", row)
        layout = [line for line in self.runbook.splitlines() if line.startswith("| `EMOTORAD_ZOHO_LAYOUT_ID`")][0]
        self.assertIn("probe.py", layout)
        self.assertIn("Optional", layout)

    def test_the_runbook_says_which_names_go_in_the_secret_and_how_to_stop(self):
        self.assertIn("## 7. Zoho Desk tickets", self.runbook)
        self.assertIn("six", self.runbook)
        self.assertIn("scripts/zoho/tickets_report.py", self.runbook)
        self.assertIn("scripts/zoho/revoke.py", self.runbook)
        self.assertIn("EMOTORAD_AI_ENV", self.runbook)
        self.assertIn("`[AI chat]`", self.runbook)
        for status in ("not configured", "test department", "token refused: <error>", "sending failing: <code>"):
            self.assertIn("`%s`" % status, self.runbook)

    def test_the_runbook_says_the_token_is_the_omss_and_the_rollback_never_revokes(self):
        # Sachin's decision of 5 October 2026: the chatbot shares the OMS's
        # client and refresh token. Revoking it stops the OMS's ticketing.
        section = " ".join(self.zoho_section().split())
        for needle in ("**Shared with the OMS.**", "OMS's own Zoho client id, client secret and refresh token",
                       "10 active access tokens per refresh token", "the oldest invalidated when an eleventh",
                       "10 access-token requests in 10 minutes", "20 refresh tokens per client per user",
                       "about two refreshes an hour", "the same day", "`token refused: <error>`",
                       "`zoho_token_refused`", "`Desk.basic.READ`", "`Desk.settings.READ`", "the probe shows",
                       "AFS dispatch", "Connected Apps page"):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)
        stop = section[section.index("**To stop sending.**"):]
        stop = stop[:stop.index("| `/health` `zoho` |")]
        self.assertIn("remove `EMOTORAD_ZOHO_REFRESH_TOKEN`", stop)
        self.assertIn("Never revoke it.", stop)
        self.assertIn("refuses without `--own-client`", stop)
        self.assertNotIn("To revoke the token itself", self.runbook)
        [row] = [line for line in self.runbook.splitlines() if line.startswith("| `EMOTORAD_ZOHO_REFRESH_TOKEN`")]
        self.assertIn("The OMS's own refresh token", row)
        self.assertNotIn("never the OMS's", row)

    def test_the_runbooks_steps_start_at_the_probe_and_the_grant_is_for_a_client_of_our_own(self):
        section = self.zoho_section()
        [first] = [line for line in section.splitlines() if line.startswith("1. ")][:1]
        self.assertIn("**The probe.**", first)
        future = section.index("**For a future client of our own.**")
        steps = section[:future]
        for script in ("python scripts/zoho/consent_url.py", "python scripts/zoho/exchange_code.py"):
            with self.subTest(script=script):
                self.assertNotIn(script, steps)
                self.assertIn(script, section[future:])
        self.assertLess(steps.index("**The probe.**"), steps.index("**The test ticket.**"))
        self.assertLess(steps.index("**The test ticket.**"), steps.index("**The settings.**"))
        self.assertIn("python scripts/zoho/revoke.py --own-client", section[future:])

    def test_the_spec_records_the_shared_token_with_the_5_october_decisions(self):
        spec = (ROOT / "docs" / "superpowers" / "specs" / "2026-10-05-zoho-desk-tickets-design.md").read_text(
            encoding="utf-8")
        decisions = spec[spec.index("The person's decisions (5 October 2026):"):spec.index("## What exists today")]
        decision = decisions[decisions.index("- **The OMS's token is shared (5 October 2026, Sachin's decision).**"):]
        decision = " ".join(decision.split())
        for needle in ("`consent_url.py` and `exchange_code.py` leave the setup", "start at the probe",
                       "10 active access tokens per refresh token", "never revoking", "AFS dispatch",
                       "the same day", "`Desk.settings.READ`"):
            with self.subTest(needle=needle):
                self.assertIn(needle, decision)

    def test_the_contract_says_ticket_id_is_our_reference_and_what_zoho_rules_filter_on(self):
        self.assertIn("`EM-` and seven digits, from `EM-1000001`", self.contract)
        self.assertIn("Never the Zoho Desk ticket number", self.contract)
        self.assertIn("`[AI chat]`", self.contract)
        self.assertIn("filter", self.contract)
        self.assertNotIn("No date is set for the Zoho work yet", self.contract)
        self.assertNotIn("Zoho ticketing are not built yet", self.contract)
        self.assertNotIn("The Zoho Desk ticket number |", self.contract)

    def test_the_contract_keeps_the_zoho_heading_its_references_point_at(self):
        self.assertIn("## What changes with the Zoho integration", self.contract)
        self.assertIn('see "What changes with the Zoho integration"', self.contract)

    def zoho_section(self):
        return self.runbook[self.runbook.index("## 7. Zoho Desk tickets"):]

    def test_a_filter_on_the_subject_is_on_it_containing_ai_chat(self):
        # The final review, tickets-worker Important 2: an unverified ticket's
        # subject puts "[Unverified] " first, so "starts with" misses them.
        for name, text in (("runbook", self.runbook), ("contract", self.contract)):
            with self.subTest(document=name):
                self.assertIn("contains `[AI chat]`", text)
                self.assertIn("`[Unverified] [AI chat]", text)
                self.assertNotIn("starts with `[AI chat]`", text)

    def test_erasing_someones_ticket_records_is_written_down_as_a_manual_step(self):
        # The final review, scripts-docs Important 5: erasure_admin does not reach `tickets` yet.
        media = (ROOT / "docs" / "runbooks" / "media.md").read_text(encoding="utf-8")
        media_six = media[media.index("## 6. "):media.index("## 7. ")]
        for name, text in (("config-store section 7", self.zoho_section()), ("media section 6", media_six)):
            text = " ".join(text.split())
            with self.subTest(document=name):
                for needle in ("section 11", "erasure_admin", "mongosh", "db.tickets.find({conversation_id:",
                               "db.tickets.find({phone:", "db.tickets.deleteOne({_id:",
                               "A Claude session never runs"):
                    self.assertIn(needle, text)

    def test_the_runbook_gives_both_shred_forms_the_alarm_profile_and_the_playground_rule(self):
        section = self.zoho_section()
        self.assertIn("rm -P ~/app-config.json", section)
        self.assertIn("shred -u ~/app-config.json", section)
        deploy = section[section.index("aws cloudformation deploy"):]
        deploy = deploy[:deploy.index("```")]
        self.assertIn("--profile emotorad-staging", deploy)
        self.assertIn("--region ap-south-1", deploy)
        self.assertIn("Streamlit", section)

    def test_the_rulebook_has_a_zoho_paragraph_and_the_manual_ticket_erasure(self):
        rulebook = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        [zoho] = [line for line in rulebook.splitlines() if line.startswith("- **Zoho Desk tickets**")]
        for needle in ("`EMOTORAD_ZOHO_REFRESH_TOKEN`", "`EMOTORAD_ZOHO_LIVE=yes`", "lifespan", "`scripts/zoho/*`",
                       "`tickets` and `counters`", "section 11", "`docs/runbooks/config-store.md` §7",
                       "The token is the OMS's", "shared by Sachin's decision", "must never be revoked",
                       "AFS dispatch"):
            with self.subTest(needle=needle):
                self.assertIn(needle, zoho)
        [erasure] = [line for line in rulebook.splitlines() if line.startswith("- **Delete my data")]
        self.assertIn("`tickets`", erasure)

    def test_register_row_7_3_says_what_the_backstop_does_with_zoho_off(self):
        register = (ROOT / "docs" / "Emotorad_Edge_Case_Register.md").read_text(encoding="utf-8")
        [row] = [line for line in register.splitlines() if line.startswith("| 7.3 |")]
        self.assertIn("With Zoho off", row)
        self.assertIn("`why: backstop_no_contact`", row)

    def test_the_build_log_has_the_zoho_build(self):
        log = (ROOT / "docs" / "Emotorad_Build_Log.md").read_text(encoding="utf-8")
        entry = log[log.index("**Zoho Desk tickets, parts 1 to 4, 2026-10-05.**"):]
        entry = entry[:entry.index("\n## ")]
        for needle in ("Invalid Redirect Uri", "zoho-token.json", "stranger", "first message", "lock-out",
                       "off by default"):
            with self.subTest(needle=needle):
                self.assertIn(needle, entry)


# The events the spec alarms on (section 8), and those it logs without an alarm.
ALARMED = ("zoho_misconfigured", "zoho_token_refused", "zoho_worker_error", "zoho_ticket_stuck",
           "safety_ticket_late", "safety_ticket_not_recorded", "unverified_ticket_capped",
           "zoho_worker_store_unavailable")
# An Atlas blip is not worth an email; an outage is. These alarm only when the
# event is logged in each of three consecutive five-minute periods.
PERSISTENT = ("zoho_worker_store_unavailable",)
# Every other ticket event the code logs, with the reason it has no alarm: an
# end state, or an event another alarm already covers when it matters.
STORE_ALARMS = "a store blip; a long outage alarms as zoho_worker_store_unavailable"
NOT_ALARMED = {
    "zoho_ticket_sent": "an end state: the ticket is in Desk",
    "zoho_retry": "the record is tried again; one that never gets through alarms as zoho_ticket_stuck or "
                  "safety_ticket_late",
    "zoho_rejected": "the record is tried hourly and /health says sending failing; one that never gets through "
                     "alarms as zoho_ticket_stuck or safety_ticket_late",
    "zoho_ticket_gone": "an end state: a person deleted or merged the ticket in Desk",
    "zoho_ticket_adopted": "an end state: the ticket an unanswered create made was found",
    "zoho_record_dropped": "an end state: the record was erased, or another worker holds it",
    "safety_ticket_failed": "followed by safety_ticket_not_recorded, which alarms",
    "ticket_recorded": "an end state: the record is made, and the worker sends it",
    "ticket_note_added": "an end state: the line is on the record",
    "ticket_note_failed": "the ticket stands without the line, and the reply promises nothing on it",
    "ticket_read_failed": "the read counts as not gone, so the record then succeeds or fails with its own event",
    "ticket_record_failed": "a safety report on that path alarms as safety_ticket_not_recorded; the other paths "
                            "promise nothing",
    "ticket_cap_unchecked": "the ticket is recorded without the cap; if the store is down, the record fails "
                            "with its own event",
    "handover_ticket_not_recorded": "the reply promises nothing: it says the hand-over was not passed on",
    "lockout_ticket_not_recorded": "with Zoho on the reply promises nothing: it says the hand-over was not "
                                   "passed on",
    "close_runs_failed": "the earlier run's record stays open, and its ticket goes to support only; " + STORE_ALARMS,
}
_EMITTED = re.compile(
    r"""\bemit\(\s*["']((?:zoho|safety_ticket|unverified_ticket|ticket|handover_ticket|lockout_ticket|close_runs)"""
    r"""_[a-z_]+)["']""")


class _CloudFormationLoader(yaml.SafeLoader):
    """CloudFormation's short tags (!Ref, !Sub) are kept as {"Ref": ...} and
    not resolved, so the template can be read as data."""


_CloudFormationLoader.add_multi_constructor("!", lambda loader, suffix, node: {
    suffix: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else loader.construct_sequence(node)})


class AlarmStackTests(unittest.TestCase):
    """Read as text: the template uses CloudFormation tags (!Ref, !Sub), which a
    plain YAML parser refuses."""

    def setUp(self):
        self.text = (ROOT / "infra" / "zoho-alarms.yaml").read_text(encoding="utf-8")

    def test_the_scan_finds_an_emit_on_one_line_and_across_two(self):
        self.assertEqual(_EMITTED.findall('self.log.emit("zoho_ticket_sent", cid, reference=r)'), ["zoho_ticket_sent"])
        self.assertEqual(_EMITTED.findall("log.emit(\n    'safety_ticket_late', cid)"), ["safety_ticket_late"])
        self.assertEqual(_EMITTED.findall('log.emit("turn_handled", cid)'), [])
        self.assertEqual(_EMITTED.findall('log.emit("tickets_listed", cid)'), [])

    def test_the_scan_finds_every_ticket_event_family(self):
        # The final review, scripts-docs Minor 13: these were outside the scan.
        for event in ("ticket_record_failed", "handover_ticket_not_recorded", "lockout_ticket_not_recorded",
                      "ticket_cap_unchecked", "close_runs_failed", "unverified_ticket_capped"):
            with self.subTest(event=event):
                self.assertEqual(_EMITTED.findall('self.log.emit(\n    "%s", cid, why="x")' % event), [event])

    def test_every_event_without_an_alarm_says_why(self):
        for event, reason in NOT_ALARMED.items():
            with self.subTest(event=event):
                self.assertGreater(len(reason.split()), 3)

    def test_every_alarmed_event_has_a_filter_and_an_alarm(self):
        for event in ALARMED:
            with self.subTest(event=event):
                self.assertIn("FilterPattern: '{ $.event = \"%s\" }'" % event, self.text)
                self.assertGreaterEqual(self.text.count("MetricName: %s\n" % event), 2)
                self.assertIn('AlarmName: !Sub "${LogGroupName}-%s"' % event, self.text)

    def test_the_stack_takes_the_log_group_and_emails_one_person(self):
        self.assertRegex(self.text, r"(?m)^  LogGroupName:\n    Type: String")
        self.assertRegex(self.text, r"(?m)^  AlarmEmail:\n    Type: String")
        self.assertIn("Protocol: email", self.text)
        self.assertIn("Endpoint: !Ref AlarmEmail", self.text)
        self.assertEqual(self.text.count("Type: AWS::Logs::MetricFilter"), len(ALARMED))
        self.assertEqual(self.text.count("Type: AWS::CloudWatch::Alarm"), len(ALARMED))
        self.assertEqual(self.text.count("- !Ref AlarmTopic"), len(ALARMED))

    def test_the_template_parses_and_each_alarm_watches_the_metric_of_its_own_filter(self):
        resources = yaml.load(self.text, Loader=_CloudFormationLoader)["Resources"]
        filters = {name: res["Properties"] for name, res in resources.items()
                   if res["Type"] == "AWS::Logs::MetricFilter"}
        alarms = {name: res["Properties"] for name, res in resources.items()
                  if res["Type"] == "AWS::CloudWatch::Alarm"}
        self.assertEqual(len(filters), len(ALARMED))
        for properties in filters.values():
            [transform] = properties["MetricTransformations"]
            self.assertEqual(properties["FilterPattern"], '{ $.event = "%s" }' % transform["MetricName"])
            self.assertEqual(properties["LogGroupName"], {"Ref": "LogGroupName"})
        self.assertEqual({p["MetricTransformations"][0]["MetricName"] for p in filters.values()},
                         {p["MetricName"] for p in alarms.values()})
        self.assertEqual({p["MetricName"] for p in alarms.values()}, set(ALARMED))
        for properties in alarms.values():
            self.assertEqual(properties["AlarmActions"], [{"Ref": "AlarmTopic"}])
            self.assertEqual(properties["TreatMissingData"], "notBreaching")

    def test_a_store_outage_alarms_only_when_it_lasts_three_five_minute_periods(self):
        # The worker logs zoho_worker_store_unavailable on each pass Atlas
        # fails. One blip is not worth an email; fifteen minutes of them is.
        resources = yaml.load(self.text, Loader=_CloudFormationLoader)["Resources"]
        alarms = {res["Properties"]["MetricName"]: res["Properties"] for res in resources.values()
                  if res["Type"] == "AWS::CloudWatch::Alarm"}
        for event, properties in alarms.items():
            with self.subTest(event=event):
                periods = 3 if event in PERSISTENT else 1
                self.assertEqual(properties["EvaluationPeriods"], periods)
                self.assertEqual(properties.get("DatapointsToAlarm", 1), periods)
                self.assertEqual((properties["Statistic"], properties["Threshold"]), ("Sum", 1))
                self.assertEqual(properties["ComparisonOperator"], "GreaterThanOrEqualToThreshold")
                if event in PERSISTENT:
                    self.assertEqual(properties["Period"], 300)

    def test_the_store_outage_alarm_comes_from_the_worker(self):
        worker = (ROOT / "src" / "emotorad_ai" / "zoho" / "worker.py").read_text(encoding="utf-8")
        self.assertIn("zoho_worker_store_unavailable", _EMITTED.findall(worker))
        self.assertNotIn("store's own alarms", worker)

    def test_no_event_is_both_alarmed_and_not(self):
        self.assertEqual(set(ALARMED) & set(NOT_ALARMED), set())

    def test_every_zoho_event_the_code_emits_has_a_decision(self):
        emitted = set()
        for path in (ROOT / "src" / "emotorad_ai").rglob("*.py"):
            emitted.update(_EMITTED.findall(path.read_text(encoding="utf-8")))
        undecided = sorted(emitted - set(ALARMED) - set(NOT_ALARMED))
        self.assertEqual(undecided, [], "add each to infra/zoho-alarms.yaml, or to NOT_ALARMED if the spec does not alarm it")
        for event in emitted & set(ALARMED):
            self.assertIn('"%s"' % event, self.text)


if __name__ == "__main__":
    unittest.main()
