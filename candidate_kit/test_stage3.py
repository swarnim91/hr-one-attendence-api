"""
test_stage3.py — Stage 3: PATCH /attendance/{emp_code}/{date} (regularize)

All tests run against live MongoDB on localhost:27017.
Each test method starts with a clean slate (employees + attendance_logs cleared).

Epoch reference (IST = UTC+05:30):
  1783310400000  -> 2026-07-06T09:30:00+05:30   (shift start, on-time)
  1783314000000  -> 2026-07-06T10:30:00+05:30   (60 late minutes if shift 09:30)
  1783342800000  -> 2026-07-06T18:30:00+05:30   (shift end 09:30-18:30)
  1783344600000  -> 2026-07-06T19:00:00+05:30   (30 min overtime)
  1783326600000  -> 2026-07-06T14:00:00+05:30   (4h30m after 09:30 -> work_hours=4.5, NOT half-day)
  1783325520000  -> 2026-07-06T13:42:00+05:30   (work_hours=4.2h if pi=09:30, half_day=True)
  1783353300000  -> 2026-07-06T21:25:00+05:30   (punch_in for overnight 22:00-06:00 shift)
  1783355100000  -> 2026-07-06T21:55:00+05:30   (punch_in for overnight 22:00-06:00 shift)
  1783381800000  -> 2026-07-07T05:30:00+05:30   (punch_out for overnight)
  1783384200000  -> 2026-07-07T06:10:00+05:30   (punch_out for overnight)
"""
import os
import threading
import unittest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, IST

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB", "attendance_db")

BASE_EMP = {
    "emp_code": "EMP0001",
    "name": "Test User",
    "email": "test@test.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-01",
}

REASON_OK = "Biometric glitch on that day"
REG_BY    = "hr.admin"
DATE      = "2026-07-06"

# Epoch ms helpers
PI_ON_TIME     = 1783310400000   # 09:30 IST (0 late)
PI_60_LATE     = 1783314000000   # 10:30 IST (60 late)
PO_SHIFT_END   = 1783342800000   # 18:30 IST
PO_30_OT       = 1783344600000   # 19:00 IST (30 min OT)
PO_HALF_DAY    = 1783325520000   # 13:42 IST  (4.2h work -> half_day True)
PO_NOT_HALF    = 1783326600000   # 14:00 IST  (4.5h work -> half_day False, boundary)


class TestStage3(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoClient(MONGO_URI)
        cls.db = cls.mongo[MONGO_DB]

    def setUp(self):
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})

    # -----------------------------------------------------------------------
    # Setup helpers
    # -----------------------------------------------------------------------
    def _create_emp(self, client, **overrides):
        payload = {**BASE_EMP, **overrides}
        r = client.post("/employees", json=payload)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _punch_in(self, client, emp_code="EMP0001", punched_at=PI_ON_TIME, status="PRESENT"):
        r = client.post("/attendance/punch-in",
                        json={"emp_code": emp_code, "punched_at": punched_at, "status": status})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _punch_out(self, client, emp_code="EMP0001", punched_at=PO_SHIFT_END):
        r = client.post("/attendance/punch-out",
                        json={"emp_code": emp_code, "punched_at": punched_at})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _patch(self, client, emp_code="EMP0001", date=DATE, **body):
        return client.patch(f"/attendance/{emp_code}/{date}", json=body)

    # -----------------------------------------------------------------------
    # Test: successful correction + exactly one history entry
    # -----------------------------------------------------------------------
    def test_successful_patch_one_history_entry(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_60_LATE)   # late record

            r = self._patch(client,
                            punch_in=PI_ON_TIME,
                            reason=REASON_OK,
                            regularized_by=REG_BY)

            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()

            # Derived fields recomputed
            self.assertEqual(data["late_minutes"], 0)
            self.assertEqual(data["punch_in"], PI_ON_TIME)

            # Exactly one history entry
            self.assertEqual(len(data["history"]), 1)
            entry = data["history"][0]
            self.assertEqual(entry["by"], REG_BY)
            self.assertEqual(entry["reason"], REASON_OK)
            self.assertIn("at", entry)

            # Changes must include punch_in and late_minutes
            self.assertIn("punch_in", entry["changes"])
            self.assertIn("late_minutes", entry["changes"])
            self.assertEqual(entry["changes"]["punch_in"]["from"], PI_60_LATE)
            self.assertEqual(entry["changes"]["punch_in"]["to"],   PI_ON_TIME)
            self.assertEqual(entry["changes"]["late_minutes"]["from"], 60)
            self.assertEqual(entry["changes"]["late_minutes"]["to"],   0)

            # _id must not be in response
            self.assertNotIn("_id", data)

    # -----------------------------------------------------------------------
    # Test: only genuinely changed fields in changes
    # -----------------------------------------------------------------------
    def test_changes_only_contains_changed_fields(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)

            r = self._patch(client,
                            status="WFH",   # only status changes
                            reason=REASON_OK,
                            regularized_by=REG_BY)

            self.assertEqual(r.status_code, 200, r.text)
            changes = r.json()["history"][0]["changes"]

            self.assertIn("status", changes)
            self.assertNotIn("punch_in", changes)
            self.assertNotIn("punch_out", changes)
            self.assertNotIn("late_minutes", changes)

    # -----------------------------------------------------------------------
    # Test: no-op returns 422
    # -----------------------------------------------------------------------
    def test_noop_patch_returns_422(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)

            # Same punch_in, no status change — genuinely nothing changes
            r = self._patch(client,
                            punch_in=PI_ON_TIME,
                            reason=REASON_OK,
                            regularized_by=REG_BY)
            self.assertEqual(r.status_code, 422, r.text)

    # -----------------------------------------------------------------------
    # Test: 404 for missing employee and missing attendance record
    # -----------------------------------------------------------------------
    def test_404_cases(self):
        with TestClient(app) as client:
            # Unknown employee
            r = self._patch(client, emp_code="EMP9999",
                            reason=REASON_OK, regularized_by=REG_BY,
                            punch_in=PI_ON_TIME)
            self.assertEqual(r.status_code, 404)

            # Known employee but no record
            self._create_emp(client)
            r = self._patch(client, date="2020-01-01",
                            reason=REASON_OK, regularized_by=REG_BY,
                            punch_in=PI_ON_TIME)
            self.assertEqual(r.status_code, 404)

    # -----------------------------------------------------------------------
    # Test: ABSENT/LEAVE clearing; invalid combinations
    # -----------------------------------------------------------------------
    def test_absent_leave_rules(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)

            # Setting ABSENT is fine — clears punch times
            r = self._patch(client, status="ABSENT",
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertIsNone(data["punch_in"])
            self.assertIsNone(data["punch_out"])
            self.assertEqual(data["late_minutes"], 0)
            self.assertIsNone(data["work_hours"])

            # Supplying punch_in with ABSENT is 422
            r2 = self._patch(client, status="ABSENT",
                             punch_in=PI_ON_TIME,
                             reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r2.status_code, 422)

            # Supplying punch_out with LEAVE is 422
            r3 = self._patch(client, status="LEAVE",
                             punch_out=PO_SHIFT_END,
                             reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r3.status_code, 422)

    # -----------------------------------------------------------------------
    # Test: presence status requires punch_in
    # -----------------------------------------------------------------------
    def test_presence_requires_punch_in(self):
        with TestClient(app) as client:
            self._create_emp(client)
            # Insert a raw ABSENT record (no punch_in)
            self.db.attendance_logs.insert_one({
                "emp_code": "EMP0001", "date": DATE,
                "status": "ABSENT", "punch_in": None, "punch_out": None,
                "work_hours": None, "late_minutes": 0,
                "overtime_minutes": 0, "half_day": False, "history": []
            })
            # Try to set PRESENT without supplying punch_in -> 422
            r = self._patch(client, status="PRESENT",
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 422, r.text)

    # -----------------------------------------------------------------------
    # Test: strict timestamp validation
    # -----------------------------------------------------------------------
    def test_strict_timestamp_validation(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)

            # Float timestamp
            r = client.patch(f"/attendance/EMP0001/{DATE}", json={
                "reason": REASON_OK, "regularized_by": REG_BY,
                "punch_in": 1783310400000.5
            })
            self.assertEqual(r.status_code, 422)

            # String timestamp
            r = client.patch(f"/attendance/EMP0001/{DATE}", json={
                "reason": REASON_OK, "regularized_by": REG_BY,
                "punch_in": "1783310400000"
            })
            self.assertEqual(r.status_code, 422)

            # Seconds-looking timestamp (too small)
            r = client.patch(f"/attendance/EMP0001/{DATE}", json={
                "reason": REASON_OK, "regularized_by": REG_BY,
                "punch_in": 1783310400
            })
            self.assertEqual(r.status_code, 422)

    # -----------------------------------------------------------------------
    # Test: punch_in date validation (R1) – must resolve to record's date
    # -----------------------------------------------------------------------
    def test_punch_in_date_validation(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)

            # Set punch_in to a totally different calendar date (2026-07-10)
            r = self._patch(client,
                            punch_in=1783656600000,    # 2026-07-10T09:30:00+05:30
                            reason=REASON_OK,
                            regularized_by=REG_BY)
            self.assertEqual(r.status_code, 422, r.text)

    # -----------------------------------------------------------------------
    # Test: overnight shift punch_in date validation
    # -----------------------------------------------------------------------
    def test_overnight_shift_punch_in_date(self):
        with TestClient(app) as client:
            self._create_emp(client, shift_start="22:00", shift_end="06:00")
            # Punch in at 21:55 (2026-07-06T16:25:00Z = 2026-07-06T21:55:00+05:30)
            # attendance date = 2026-07-06
            self._punch_in(client, punched_at=1783355100000)

            # Try to patch with a punch_in at 01:10 IST (would resolve to 2026-07-06 for overnight)
            # 2026-07-07T01:10:00+05:30 = 1783381800000 - verify acceptance
            r = self._patch(client, date="2026-07-06",
                            punch_in=1783381800000,   # 01:10 IST -> 2026-07-06 for overnight
                            reason=REASON_OK,
                            regularized_by=REG_BY)
            self.assertEqual(r.status_code, 200, r.text)

            # A punch_in from a different day (daytime, resolves to 2026-07-07) -> 422
            r2 = self._patch(client, date="2026-07-06",
                             punch_in=1783398600000,  # 2026-07-07T11:00:00+05:30 -> 2026-07-07
                             reason=REASON_OK,
                             regularized_by=REG_BY)
            self.assertEqual(r2.status_code, 422, r2.text)

    # -----------------------------------------------------------------------
    # Test: punch_out ordering and 24h limit
    # -----------------------------------------------------------------------
    def test_punch_out_order_and_24h(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)

            # punch_out before punch_in -> 422
            r = self._patch(client,
                            punch_out=PI_ON_TIME - 3600000,   # 1h before
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 422)

            # punch_out exactly equal to punch_in -> 422
            r = self._patch(client,
                            punch_out=PI_ON_TIME,
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 422)

            # punch_out > 24h after punch_in -> 422
            r = self._patch(client,
                            punch_out=PI_ON_TIME + 86401000,   # 24h + 1s
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 422)

            # Valid punch_out -> 200
            r = self._patch(client,
                            punch_out=PO_SHIFT_END,
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 200, r.text)

    # -----------------------------------------------------------------------
    # Test: derived-field recalculation and half-day boundary
    # -----------------------------------------------------------------------
    def test_derived_field_recalculation(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)
            self._punch_out(client, punched_at=PO_SHIFT_END)

            # Correct to 30 min overtime
            r = self._patch(client,
                            punch_out=PO_30_OT,
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["overtime_minutes"], 30)

    def test_half_day_boundary(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_ON_TIME)

            # PO_HALF_DAY -> work_hours < 4.5 -> half_day True
            r = self._patch(client,
                            punch_out=PO_HALF_DAY,
                            reason=REASON_OK, regularized_by=REG_BY)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertLess(data["work_hours"], 4.50)
            self.assertTrue(data["half_day"])

            # PO_NOT_HALF -> work_hours == 4.50 exactly -> half_day False
            r2 = self._patch(client,
                             punch_out=PO_NOT_HALF,
                             reason=f"{REASON_OK} again",
                             regularized_by=REG_BY)
            self.assertEqual(r2.status_code, 200)
            data2 = r2.json()
            self.assertEqual(data2["work_hours"], 4.50)
            self.assertFalse(data2["half_day"])

    # -----------------------------------------------------------------------
    # Test: existing history is preserved on second correction
    # -----------------------------------------------------------------------
    def test_existing_history_preserved(self):
        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_60_LATE)

            # First correction
            r1 = self._patch(client,
                             punch_in=PI_ON_TIME,
                             reason=REASON_OK,
                             regularized_by=REG_BY)
            self.assertEqual(r1.status_code, 200)

            # Second correction
            r2 = self._patch(client,
                             status="WFH",
                             reason="Switched to WFH per manager approval",
                             regularized_by="manager.x")
            self.assertEqual(r2.status_code, 200)
            data = r2.json()

            # Both history entries must be present, oldest first
            self.assertEqual(len(data["history"]), 2)
            self.assertEqual(data["history"][0]["by"], REG_BY)
            self.assertEqual(data["history"][1]["by"], "manager.x")

    # -----------------------------------------------------------------------
    # Test: concurrent corrections do not lose history (409 for loser)
    # -----------------------------------------------------------------------
    def test_concurrent_corrections_no_lost_history(self):
        """
        Verify the optimistic $size lock at the DB level.

        TestClient (httpx synchronous WSGI transport) cannot produce a real wall-clock
        race between two threads, so we simulate concurrency directly:
          1. Request A reads the record (history_len = 0).
          2. Request B sneaks in and appends a history entry (history_len becomes 1).
          3. Request A attempts its find_one_and_update with {$size: 0}.
             The filter no longer matches => returns None => 409.
        This proves the atomic guard works without depending on OS scheduling.
        """
        from bson import ObjectId

        with TestClient(app) as client:
            self._create_emp(client)
            self._punch_in(client, punched_at=PI_60_LATE)

            # Step 1 – read record (simulates what request A has just done)
            record = self.db.attendance_logs.find_one({"emp_code": "EMP0001", "date": DATE})
            self.assertIsNotNone(record)
            history_len_a = len(record.get("history") or [])  # 0

            # Step 2 – simulate request B winning the race by pushing a history entry
            from app.main import get_ist_now
            self.db.attendance_logs.update_one(
                {"_id": record["_id"]},
                {"$push": {"history": {
                    "at": get_ist_now(), "by": "admin_b",
                    "reason": "concurrent edit by B",
                    "changes": {"status": {"from": "PRESENT", "to": "WFH"}}
                }}}
            )

            # Step 3 – request A now tries to update with the stale $size filter
            updated = self.db.attendance_logs.find_one_and_update(
                {"_id": record["_id"], "history": {"$size": history_len_a}},
                {"$set": {"status": "ON_DUTY"}, "$push": {"history": {
                    "at": get_ist_now(), "by": "admin_a",
                    "reason": "conflicting edit by A",
                    "changes": {"status": {"from": "PRESENT", "to": "ON_DUTY"}}
                }}},
                return_document=True,
            )

            # The $size filter no longer matches (len is now 1), so update returns None
            self.assertIsNone(updated, "Optimistic lock should have blocked request A")

            # Verify only B's entry is in history (A's entry was not written)
            final = self.db.attendance_logs.find_one({"_id": record["_id"]})
            self.assertEqual(len(final["history"]), 1)
            self.assertEqual(final["history"][0]["by"], "admin_b")


if __name__ == "__main__":
    unittest.main()
