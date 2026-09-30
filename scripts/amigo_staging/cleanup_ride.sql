/*
  Removes exactly the trips ride.sql added for test rider C.
  Run by a person, connected as postgres to `ride`.
*/
BEGIN;

DELETE FROM trips WHERE emuserid = '00000000-0000-4000-8000-00000000a003'
  AND tripid IN ('TESTTRIP-0001', 'TESTTRIP-0002', 'TESTTRIP-0003');

COMMIT;
