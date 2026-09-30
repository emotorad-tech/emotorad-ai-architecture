/*
  Removes exactly the test riders and bikes userbike.sql added, and nothing
  else. Run by a person, connected as postgres to `userbike`. The grants stay;
  revoke them separately if the chatbot's access is to end too.
*/
BEGIN;

DELETE FROM userbikemap WHERE emuserid IN (
  '00000000-0000-4000-8000-00000000a001', '00000000-0000-4000-8000-00000000a002', '00000000-0000-4000-8000-00000000a003');
DELETE FROM bike WHERE vin IN (
  'TESTVIN00000000001', 'TESTVIN00000000002', 'FRPVINTEST0000000000000b', 'TESTVIN00000000003');
DELETE FROM emuser WHERE emuserid IN (
  '00000000-0000-4000-8000-00000000a001', '00000000-0000-4000-8000-00000000a002', '00000000-0000-4000-8000-00000000a003');

COMMIT;
