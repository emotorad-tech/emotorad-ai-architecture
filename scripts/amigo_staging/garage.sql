/*
  Amigo staging, database `garage` (the old set, not revalt_garage):
  read-only grants for the chatbot, and service records for test rider C.
  Run by a person, connected as postgres to `garage`, after userbike.sql.
  Safe to run twice. Undo with cleanup_garage.sql.

  The service record has the shape the Amigo garage service writes
  (garage-server/api/service/postService.go): one status per service type,
  "complete", "pending" or "upcoming". Rider C has done the 250 km service,
  is due the 1000 km one, and has the 2000 km one ahead. purchasedate is
  epoch milliseconds: 15 March 2026, 11:00 IST.
*/
BEGIN;

GRANT CONNECT ON DATABASE garage TO ro_chatbot;
GRANT USAGE ON SCHEMA public TO ro_chatbot;
GRANT SELECT (emuserid, vin, bikemodel, purchasedate, odometer, services) ON servicedetail TO ro_chatbot;
GRANT SELECT (emuserid, vin, bikemodel, servicetype) ON servicehistory TO ro_chatbot;
GRANT SELECT ON servicetype TO ro_chatbot;

INSERT INTO servicedetail (emuserid, vin, bikemodel, phonenumber, issynced, purchasedate, odometer, services) VALUES
  ('00000000-0000-4000-8000-00000000a003', 'TESTVIN00000000003', 'TREXAIR', '+919700000033', true,
   1773552600000, 1180, '{"serviceOne": "complete", "serviceTwo": "pending", "serviceThree": "upcoming"}')
ON CONFLICT DO NOTHING;

INSERT INTO servicehistory (emuserid, vin, bikemodel, phonenumber, emailid, servicetype)
SELECT '00000000-0000-4000-8000-00000000a003', 'TESTVIN00000000003', 'TREXAIR', '+919700000033', NULL, id
FROM servicetype WHERE servicename = 'serviceOne'
ON CONFLICT DO NOTHING;

COMMIT;
