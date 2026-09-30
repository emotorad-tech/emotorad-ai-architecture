/*
  Removes exactly the service records garage.sql added for test rider C.
  Run by a person, connected as postgres to `garage`.
*/
BEGIN;

DELETE FROM servicehistory WHERE emuserid = '00000000-0000-4000-8000-00000000a003';
DELETE FROM servicedetail WHERE emuserid = '00000000-0000-4000-8000-00000000a003';

COMMIT;
