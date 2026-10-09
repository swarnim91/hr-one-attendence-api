import unittest
import os
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import (
    app, IST, compute_late_minutes, compute_work_hours, 
    compute_overtime, get_attendance_date, epoch_ms_to_ist
)

class TestStage2a(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.db = cls.mongo[os.getenv("MONGO_DB", "attendance_db")]
        
    def setUp(self):
        # Clean DB before each test
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})

    # --- Pure Logic Tests ---
    def test_time_utilities(self):
        shift_start = "09:30"
        date_str = "2026-07-06"
        
        # 09:40:00 -> 0 late
        punch_in_1 = datetime.fromisoformat(f"{date_str}T09:40:00").replace(tzinfo=IST)
        self.assertEqual(compute_late_minutes(punch_in_1, date_str, shift_start), 0)
        
        # 09:40:01 -> 10 late
        punch_in_2 = datetime.fromisoformat(f"{date_str}T09:40:01").replace(tzinfo=IST)
        self.assertEqual(compute_late_minutes(punch_in_2, date_str, shift_start), 10)

        # Overnight shift dates
        self.assertEqual(get_attendance_date(datetime(2026, 7, 6, 21, 55, tzinfo=IST), "22:00", "06:00"), "2026-07-06")
        self.assertEqual(get_attendance_date(datetime(2026, 7, 7, 1, 10, tzinfo=IST), "22:00", "06:00"), "2026-07-06")

        # Whole second truncation (microsecond explicitly stripped via logic check)
        dt = epoch_ms_to_ist(1783312500123)
        self.assertEqual(dt.microsecond, 0)

        # Half up rounding
        dt1 = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
        self.assertEqual(compute_work_hours(dt1, dt1 + timedelta(seconds=7650)), 2.13)
        self.assertEqual(compute_work_hours(dt1, dt1 + timedelta(seconds=7646)), 2.12)

        # Overtime threshold
        self.assertEqual(compute_overtime(datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST), date_str, "09:30", "18:30"), 0)
        self.assertEqual(compute_overtime(datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST), date_str, "09:30", "18:30"), 30)

    # --- Integration Tests (MongoDB required) ---
    def test_employee_creation_duplicate(self):
        with TestClient(app) as client:
            payload = {
                "emp_code": "EMP1234",
                "name": "Test User",
                "email": "test@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01"
            }
            # First insert works
            r1 = client.post("/employees", json=payload)
            self.assertEqual(r1.status_code, 201)
            
            # Second insert fails with 409 due to unique index
            r2 = client.post("/employees", json=payload)
            self.assertEqual(r2.status_code, 409)

    def test_punch_in_duplicate(self):
        with TestClient(app) as client:
            client.post("/employees", json={
                "emp_code": "EMP9999", "name": "A", "email": "a@a.com", "department": "HR", 
                "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
            })
            
            payload = {"emp_code": "EMP9999", "status": "PRESENT"}
            r1 = client.post("/attendance/punch-in", json=payload)
            self.assertEqual(r1.status_code, 201)
            
            # Duplicate for same day
            r2 = client.post("/attendance/punch-in", json=payload)
            self.assertEqual(r2.status_code, 409)
            
    def test_pagination_filtered_totals(self):
        with TestClient(app) as client:
            for i in range(5):
                client.post("/employees", json={
                    "emp_code": f"EMP{i:04d}", "name": f"Name{i}", "email": f"a{i}@a.com",
                    "department": "IT" if i % 2 == 0 else "HR",
                    "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
                })
            
            r = client.get("/employees?department=IT")
            data = r.json()
            self.assertEqual(data["total"], 3)
            self.assertEqual(len(data["items"]), 3)
            self.assertEqual(data["items"][0]["emp_code"], "EMP0000")

    def test_invalid_timestamps(self):
        with TestClient(app) as client:
            client.post("/employees", json={
                "emp_code": "EMP1111", "name": "B", "email": "b@b.com", "department": "HR", 
                "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
            })
            
            # String -> 422
            r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP1111", "punched_at": "invalid"})
            self.assertEqual(r1.status_code, 422)
            
            # Seconds timestamp (out of bounds) -> 422
            r2 = client.post("/attendance/punch-in", json={"emp_code": "EMP1111", "punched_at": 1783312500})
            self.assertEqual(r2.status_code, 422)
            
            # Float timestamp -> 422
            r3 = client.post("/attendance/punch-in", json={"emp_code": "EMP1111", "punched_at": 1783312500123.5})
            self.assertEqual(r3.status_code, 422)

    def test_health_endpoint(self):
        with TestClient(app) as client:
            # Assumes MongoDB is running
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)

if __name__ == '__main__':
    unittest.main()
