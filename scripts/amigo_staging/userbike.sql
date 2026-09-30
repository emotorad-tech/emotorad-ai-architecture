/*
  Amigo staging, database `userbike` (the old, unused set on amigo-stage-db,
  not revalt_userbike): read-only grants for the chatbot, and three invented
  test riders. Run by a person, connected as postgres to `userbike`; never
  from a Claude session. Safe to run twice. Undo with cleanup_userbike.sql.

  Every rider, bike and phone here is invented: names start "TEST", phones are
  in the +91970000xxxx block the chatbot's fixtures use, and VINs start with
  TESTVIN (or FRPVINTEST for the IMEI-registered bike).

    A  +919700000031  two bikes: an EMX+ and a Doodle Pro, both owned
    B  +919700000032  one bike registered by IMEI: its frame number field
                      holds the IMEI and its VIN is generated, as the Amigo
                      app does since July 2026
    C  +919700000033  one T-Rex Air, with service records (garage.sql) and
                      recent trips (ride.sql)

  Needs the role first (as postgres, in any database):
    CREATE ROLE ro_chatbot LOGIN CONNECTION LIMIT 10;  then  \password ro_chatbot
    ALTER ROLE ro_chatbot SET default_transaction_read_only = on;
    ALTER ROLE ro_chatbot SET statement_timeout = '5s';
*/
BEGIN;

GRANT CONNECT ON DATABASE userbike TO ro_chatbot;
GRANT USAGE ON SCHEMA public TO ro_chatbot;
GRANT SELECT (emuserid, username, phone) ON emuser TO ro_chatbot;
GRANT SELECT (vin, model, color, framenumber, imei, nickname, createdat) ON bike TO ro_chatbot;
GRANT SELECT (emuserid, bikevin, primarymapping, createdat) ON userbikemap TO ro_chatbot;

INSERT INTO emuser (emuserid, username, phone, level) VALUES
  ('00000000-0000-4000-8000-00000000a001', 'TEST Rider A', '+919700000031', 1),
  ('00000000-0000-4000-8000-00000000a002', 'TEST Rider B', '+919700000032', 1),
  ('00000000-0000-4000-8000-00000000a003', 'TEST Rider C', '+919700000033', 1)
ON CONFLICT DO NOTHING;

INSERT INTO bike (vin, model, color, framenumber, imei, nickname, mqttpassword) VALUES
  ('TESTVIN00000000001', 'EMXPLUS', 'aqua', 'TESTEMXP0000001', NULL, 'TEST city bike', 'test-only-not-a-secret'),
  ('TESTVIN00000000002', 'DOODLEPRO', 'nativepop', 'TESTDDLP0000002', NULL, NULL, 'test-only-not-a-secret'),
  ('FRPVINTEST0000000000000b', 'TREXSMART', 'grey', '860000000000032', '860000000000032', NULL, 'test-only-not-a-secret'),
  ('TESTVIN00000000003', 'TREXAIR', 'green', 'TESTTREX0000003', NULL, NULL, 'test-only-not-a-secret')
ON CONFLICT DO NOTHING;

INSERT INTO userbikemap (emuserid, bikevin, primarymapping) VALUES
  ('00000000-0000-4000-8000-00000000a001', 'TESTVIN00000000001', true),
  ('00000000-0000-4000-8000-00000000a001', 'TESTVIN00000000002', true),
  ('00000000-0000-4000-8000-00000000a002', 'FRPVINTEST0000000000000b', true),
  ('00000000-0000-4000-8000-00000000a003', 'TESTVIN00000000003', true)
ON CONFLICT DO NOTHING;

COMMIT;
