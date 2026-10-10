"""The OMS afs_order_add client (spec 2026-10-10 replacement orders, section 5).
No network: the WebSocket and the login are fakes."""

import json
import unittest

from emotorad_ai import oms_afs

SETTINGS = oms_afs.AFSSettings(ws_url="wss://oms.example/ws/", login_url="https://oms.example/user/login",
                               email="bot-admin@example.com", password="pw-secret-1", token="tok-old")


class FakeOMS:
    """One fake OMS: valid tokens, its orders, and every frame it was sent."""

    def __init__(self, valid=("tok-old",), orders=(), fail_add_after_create=False, down=False):
        self.valid, self.orders, self.sent = set(valid), list(orders), []
        self.fail_add_after_create, self.down, self.logins = fail_add_after_create, down, 0

    def connect(self, url, **kwargs):
        if self.down:
            raise OSError("refused")
        return FakeSocket(self, url.rsplit("/", 2)[-2])

    def post(self, url, json=None, timeout=None):
        self.logins += 1
        token = "tok-new"
        self.valid.add(token)
        return FakeResponse(200, {"msg": "ok", "data": {"access_token": token}})

    def answer(self, token, frame):
        if token not in self.valid:
            return {"transmit": "single", "url": "unauthorized"}
        url, request = frame["url"], frame.get("request") or {}
        frame = dict(frame)
        if url == "afs_order_list":
            rows = [o for o in self.orders if request.get("search", "") in (o.get("ticket_id") or "")]
            frame["response"] = ({"status": 200, "msg": "Order Found", "data": {"data": rows}} if rows
                                 else {"status": 400, "msg": "Order Not Found", "data": {}})
        elif url == "sale_type_list":
            frame["response"] = {"status": 200, "msg": "ok", "data": {"data": [
                {"id": "st-1", "sale_type": "After Sales"}, {"id": "st-2", "sale_type": "Warranty"}]}}
        elif url == "afs_order_add":
            self.sent.append(request)
            order = {"id": "o-%d" % (len(self.orders) + 1), "order_code": "AFS/26-27/EC/%d" % (len(self.orders) + 1),
                     "ticket_id": request.get("ticket_number")}
            self.orders.append(order)
            frame["response"] = ({"status": 400, "msg": "ERP timeout", "data": {}} if self.fail_add_after_create
                                 else {"status": 200, "msg": "Order Added", "data": order})
        return frame


class FakeSocket:
    def __init__(self, oms, token):
        self.oms, self.token, self.outbox = oms, token, []

    def send(self, text):
        self.outbox.append(json.dumps(self.oms.answer(self.token, json.loads(text))))

    def recv(self, timeout=None):
        if not self.outbox:
            raise TimeoutError("no frame")
        return self.outbox.pop(0)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


def client(oms, logged=None):
    return oms_afs.AFSClient(SETTINGS, connect=oms.connect, post=oms.post,
                             log=(lambda event, fields: logged.append((event, fields))) if logged is not None else None)


class SettingsTests(unittest.TestCase):
    def test_off_unless_exactly_on(self):
        for value in ("", "yes", "ON"):
            self.assertEqual(oms_afs.settings_from_env({oms_afs.SWITCH_ENV: value})[1], "off")

    def test_on_needs_every_required_setting(self):
        env = {oms_afs.SWITCH_ENV: "on", "EMOTORAD_OMS_WS_URL": "wss://x/ws/"}
        settings, health = oms_afs.settings_from_env(env)
        self.assertIsNone(settings)
        self.assertTrue(health.startswith("misconfigured: "))
        self.assertIn("EMOTORAD_OMS_ADMIN_PASSWORD", health)
        env.update({"EMOTORAD_OMS_LOGIN_URL": "https://x/user/login", "EMOTORAD_OMS_ADMIN_EMAIL": "a@b.c",
                    "EMOTORAD_OMS_ADMIN_PASSWORD": "p"})
        settings, health = oms_afs.settings_from_env(env)
        self.assertEqual(health, "on")
        self.assertIsNone(settings.token)

    def test_the_settings_never_print_their_values(self):
        self.assertNotIn("pw-secret-1", repr(SETTINGS))
        self.assertNotIn("tok-old", repr(SETTINGS))


class CallTests(unittest.TestCase):
    def test_find_matches_our_reference_exactly(self):
        oms = FakeOMS(orders=[{"id": "o-9", "order_code": "AFS/X/9", "ticket_id": "RO-10000011"},
                              {"id": "o-1", "order_code": "AFS/X/1", "ticket_id": "RO-1000001"}])
        self.assertEqual(client(oms).find("RO-1000001"), {"order_code": "AFS/X/1", "order_id": "o-1"})

    def test_find_with_no_order_is_none(self):
        self.assertIsNone(client(FakeOMS()).find("RO-1000001"))

    def test_an_internal_error_that_says_not_found_is_not_no_order(self):
        class Broken(FakeOMS):
            def answer(self, token, frame):
                answered = dict(frame)
                answered["response"] = {"status": 400, "msg": "Region matching query not found: x", "data": {}}
                return answered

        with self.assertRaises(oms_afs.OMSCallError):
            client(Broken()).find("RO-1000001")

    def test_find_prefers_the_parent_of_a_split_order(self):
        oms = FakeOMS(orders=[
            {"id": "o-2", "order_code": "AFS/X/1/1", "ticket_id": "RO-1000001", "parent_code": "AFS/X/1"},
            {"id": "o-1", "order_code": "AFS/X/1", "ticket_id": "RO-1000001", "parent_code": None}])
        self.assertEqual(client(oms).find("RO-1000001"), {"order_code": "AFS/X/1", "order_id": "o-1"})

    def test_find_falls_back_to_the_first_match_when_none_is_a_parent(self):
        oms = FakeOMS(orders=[
            {"id": "o-2", "order_code": "AFS/X/1/1", "ticket_id": "RO-1000001", "parent_code": "AFS/X/1"}])
        self.assertEqual(client(oms).find("RO-1000001"), {"order_code": "AFS/X/1/1", "order_id": "o-2"})

    def test_place_returns_the_order(self):
        oms = FakeOMS()
        placed = client(oms).place({"ticket_number": "RO-1000001"}, "RO-1000001:1")
        self.assertEqual(placed, {"order_code": "AFS/26-27/EC/1", "order_id": "o-1"})

    def test_an_error_answer_is_refused_with_its_status(self):
        with self.assertRaises(oms_afs.OMSRefused) as caught:
            client(FakeOMS(fail_add_after_create=True)).place({"ticket_number": "RO-1000001"}, "RO-1000001:1")
        self.assertEqual(caught.exception.status, 400)

    def test_the_sale_type_id_is_looked_up_once(self):
        oms = FakeOMS()
        afs = client(oms)
        self.assertEqual(afs.sale_type_id("Warranty"), "st-2")
        oms.valid.clear()
        self.assertEqual(afs.sale_type_id("Warranty"), "st-2")

    def test_a_refused_token_logs_in_once_and_retries(self):
        oms = FakeOMS(valid=())
        afs = client(oms)
        self.assertIsNone(afs.find("RO-1000001"))
        self.assertEqual(oms.logins, 1)
        self.assertIsNone(afs.find("RO-1000001"))
        self.assertEqual(oms.logins, 1)

    def test_a_dead_oms_is_a_call_error(self):
        with self.assertRaises(oms_afs.OMSCallError):
            client(FakeOMS(down=True)).find("RO-1000001")

    def test_no_setting_value_reaches_an_error_or_a_log(self):
        logged = []

        class BadLogin(FakeOMS):
            def post(self, url, json=None, timeout=None):
                return FakeResponse(400, {"error": "Wrong password"})

        with self.assertRaises(oms_afs.OMSAuthError) as caught:
            client(BadLogin(valid=()), logged).find("RO-1000001")
        text = str(caught.exception) + json.dumps(logged)
        for secret in ("pw-secret-1", "tok-old", "bot-admin@example.com", "oms.example"):
            self.assertNotIn(secret, text)
        self.assertEqual(logged, [("oms_login_failed", {"status": 400})])


class RequestTests(unittest.TestCase):
    def test_the_request_is_a_free_warranty_customer_order(self):
        order = {"_id": "RO-1000001", "frame_number": "EMXP0001", "product_id": "uuid-1",
                 "customer": {"name": "Test Rider", "email": "t@example.com", "mobile": "9876543210",
                              "address": {"line1": "A1102, Park View", "line2": "Sector 49, Gurugram, Haryana",
                                          "pincode": "122018"}}}
        request = oms_afs.afs_request(order, pin_code_id="pin-1", sale_type_id="st-2")
        self.assertEqual(request["order_type"], "CO")
        self.assertEqual(request["items"], [{"product_id": "uuid-1", "product_qty": 1, "is_demo": False,
                                             "rate": 0, "amount": 0, "idx": 1}])
        self.assertEqual((request["sale_type"], request["sale_type_id"]), ("Warranty", "st-2"))
        self.assertEqual(request["ticket_number"], "RO-1000001")
        self.assertEqual(request["frame_number"], "EMXP0001")
        for side in ("bill", "ship"):
            self.assertEqual(request["%s_customer_name" % side], "Test Rider")
            self.assertEqual(request["%s_mobile" % side], "9876543210")
            self.assertEqual(request["%s_email" % side], "t@example.com")
            self.assertEqual(request["%s_pin_code_id" % side], "pin-1")
            self.assertEqual(request["%s_address" % side], "A1102, Park View")
            self.assertEqual(request["%s_address2" % side], "Sector 49, Gurugram, Haryana")
        self.assertEqual(request["remark"], "Warranty replacement placed by the EMotorad support chatbot.")

    def test_the_remark_names_our_ticket_when_there_is_one(self):
        base = {"_id": "RO-1000001", "customer": {}}
        self.assertEqual(oms_afs.afs_request(base, "p", "s")["remark"], oms_afs.REMARK)
        with_ticket = dict(base, ticket_reference="EM-1234567")
        self.assertEqual(oms_afs.afs_request(with_ticket, "p", "s")["remark"], oms_afs.REMARK + " Ticket EM-1234567.")


if __name__ == "__main__":
    unittest.main()
