/*
  Amigo staging, database `ride` (the old set, not revalt_ride): read-only
  grants for the chatbot, and three recent trips for test rider C. Run by a
  person, connected as postgres to `ride`, after userbike.sql. Safe to run
  twice. Undo with cleanup_ride.sql.

  The trips table here has the trip-server layout (startsat / endsat), as
  checked on amigo-stage-db on 30 September 2026. Times are epoch
  milliseconds (IST): 27 Sep 08:10 to 08:51, 28 Sep 18:05 to 18:31,
  29 Sep 07:40 to 08:22. Distance in km, duration in seconds. The places are
  labelled TEST; the coordinates are central Pune.
*/
BEGIN;

GRANT CONNECT ON DATABASE ride TO ro_chatbot;
GRANT USAGE ON SCHEMA public TO ro_chatbot;
GRANT SELECT (emuserid, tripid, vin, startsat, endsat, distance, duration, faultdatastorage) ON trips TO ro_chatbot;

INSERT INTO trips (emuserid, vin, startsat, endsat, startlat, startlng, endlat, endlng, startloc, endloc, info,
                   distance, averagevehiclespeed, maxspeed, calories, duration, humaneffort, motoreffort,
                   averageco2, altitude, tripid, faultdatastorage, isoffline) VALUES
  ('00000000-0000-4000-8000-00000000a003', 'TESTVIN00000000003', 1790476800000, 1790479260000,
   18.5204, 73.8567, 18.5590, 73.8076, 'TEST home', 'TEST office', 'TEST morning ride',
   12.4, 18.1, 25, 210, 2460, 0.4, 0.6, 0, 560, 'TESTTRIP-0001', 0, false),
  ('00000000-0000-4000-8000-00000000a003', 'TESTVIN00000000003', 1790598900000, 1790600460000,
   18.5590, 73.8076, 18.5204, 73.8567, 'TEST office', 'TEST home', 'TEST evening ride',
   11.9, 27.5, 25, 150, 1560, 0.3, 0.7, 0, 560, 'TESTTRIP-0002', 0, false),
  ('00000000-0000-4000-8000-00000000a003', 'TESTVIN00000000003', 1790647800000, 1790650320000,
   18.5204, 73.8567, 18.5590, 73.8076, 'TEST home', 'TEST office', 'TEST morning ride',
   12.6, 18.0, 24, 220, 2520, 0.4, 0.6, 0, 560, 'TESTTRIP-0003', 0, false)
ON CONFLICT DO NOTHING;

COMMIT;
