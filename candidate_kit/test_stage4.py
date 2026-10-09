import unittest
import os
from datetime import datetime
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, _dt_to_epoch_ms

class TestStage4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.db = cls.mongo[os.getenv("MONGO_DB", "attendance_db")]
        cls.client = TestClient(app)
        
    def setUp(self):
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})

    def test_employee_monthly_empty(self):
        r = self.client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        self.assertEqual(r.status_code, 404)
        
        self.db.employees.insert_one({
            "emp_code": "EMP0001", "name": "A", "email": "a@a.com",
            "department": "IT", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-07-05", "created_at": datetime.now()
        })
        r = self.client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertEqual(data["month"], "2026-07")
        self.assertEqual(data["present_days"], 0.0)
        self.assertEqual(data["leave_days"], 0)
        self.assertEqual(data["late_count"], 0)
        self.assertEqual(data["total_late_minutes"], 0)
        self.assertEqual(data["total_overtime_minutes"], 0)
        self.assertEqual(data["attendance_pct"], 0.0)
        self.assertEqual(data["working_days"], 20) # July 5 to 31 = 27 days, 20 are weekdays
        
    def test_employee_monthly_with_data(self):
        self.db.employees.insert_one({
            "emp_code": "EMP0001", "name": "A", "email": "a@a.com",
            "department": "IT", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-07-01", "created_at": datetime.now()
        })
        self.db.attendance_logs.insert_many([
            # Mon 2026-07-06 (Weekday) - Present, full day
            {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 15, "overtime_minutes": 60, "half_day": False},
            # Tue 2026-07-07 (Weekday) - WFH, half day
            {"emp_code": "EMP0001", "date": "2026-07-07", "status": "WFH", "late_minutes": 0, "overtime_minutes": 0, "half_day": True},
            # Wed 2026-07-08 (Weekday) - Leave
            {"emp_code": "EMP0001", "date": "2026-07-08", "status": "LEAVE", "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
            # Sat 2026-07-11 (Weekend) - Present (doesn't count to present_days, but late/overtime counts)
            {"emp_code": "EMP0001", "date": "2026-07-11", "status": "PRESENT", "late_minutes": 10, "overtime_minutes": 120, "half_day": False}
        ])
        
        r = self.client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["working_days"], 23)
        self.assertEqual(data["present_days"], 1.5) # 1 + 0.5
        self.assertEqual(data["leave_days"], 1)
        self.assertEqual(data["late_count"], 2) # 15 on Mon, 10 on Sat
        self.assertEqual(data["total_late_minutes"], 25)
        self.assertEqual(data["total_overtime_minutes"], 180)
        self.assertEqual(data["attendance_pct"], 6.52) # 1.5 / 23 * 100 = 6.5217 -> 6.52

    def test_department_summary(self):
        # 3 employees in IT, 1 in HR
        self.db.employees.insert_many([
            {"emp_code": "E1", "department": "IT", "joined_on": "2026-07-01", "shift_start": "09:30", "shift_end": "18:30"},
            {"emp_code": "E2", "department": "IT", "joined_on": "2026-07-15", "shift_start": "09:30", "shift_end": "18:30"},
            {"emp_code": "E3", "department": "IT", "joined_on": "2026-08-01", "shift_start": "09:30", "shift_end": "18:30"}, # Joined after July
            {"emp_code": "E4", "department": "HR", "joined_on": "2026-07-01", "shift_start": "09:30", "shift_end": "18:30"}
        ])
        
        self.db.attendance_logs.insert_many([
            {"emp_code": "E1", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 5, "work_hours": 8.0, "half_day": False},
            {"emp_code": "E2", "date": "2026-07-16", "status": "PRESENT", "late_minutes": 20, "work_hours": 4.0, "half_day": True},
            {"emp_code": "E4", "date": "2026-07-06", "status": "LEAVE", "late_minutes": 0, "work_hours": None, "half_day": False}
        ])
        
        r = self.client.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual(r.status_code, 200)
        data = r.json()["items"]
        self.assertEqual(len(data), 2)
        
        hr = data[0]
        self.assertEqual(hr["department"], "HR")
        self.assertEqual(hr["headcount"], 1)
        self.assertEqual(hr["present_days"], 0)
        self.assertEqual(hr["leave_count"], 1)
        self.assertIsNone(hr["avg_work_hours"])
        
        it = data[1]
        self.assertEqual(it["department"], "IT")
        self.assertEqual(it["headcount"], 2) # E3 joined in August
        self.assertEqual(it["present_days"], 1.5)
        self.assertEqual(it["late_count"], 2)
        self.assertEqual(it["total_late_minutes"], 25)
        self.assertEqual(it["avg_work_hours"], 6.0)

    def test_leaderboard(self):
        self.db.employees.insert_many([
            {"emp_code": "E1", "name": "A", "department": "IT", "joined_on": "2026-07-01", "shift_start": "09:30", "shift_end": "18:30"},
            {"emp_code": "E2", "name": "B", "department": "IT", "joined_on": "2026-07-01", "shift_start": "09:30", "shift_end": "18:30"},
            {"emp_code": "E3", "name": "C", "department": "HR", "joined_on": "2026-07-01", "shift_start": "09:30", "shift_end": "18:30"},
            {"emp_code": "E4", "name": "D", "department": "HR", "joined_on": "2026-07-01", "shift_start": "09:30", "shift_end": "18:30"}
        ])
        self.db.attendance_logs.insert_many([
            {"emp_code": "E1", "date": "2026-07-01", "late_minutes": 30},
            {"emp_code": "E1", "date": "2026-07-02", "late_minutes": 20}, # E1 total 50
            {"emp_code": "E2", "date": "2026-07-01", "late_minutes": 50}, # E2 total 50 (tie with E1)
            {"emp_code": "E3", "date": "2026-07-01", "late_minutes": 10}, # E3 total 10
            # E4 has no late minutes
        ])
        
        r = self.client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        self.assertEqual(len(items), 2)
        
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[1]["rank"], 1)
        # E1 and E2 tie for rank 1
        
        # Test department filter
        r2 = self.client.get("/analytics/leaderboard/late?month=2026-07&department=HR")
        items2 = r2.json()["items"]
        self.assertEqual(len(items2), 1)
        self.assertEqual(items2[0]["emp_code"], "E3")
        self.assertEqual(items2[0]["rank"], 1)

    def test_department_trend(self):
        self.db.employees.insert_many([
            {"emp_code": "E1", "department": "IT", "joined_on": "2026-07-01", "shift_start": "09:30", "shift_end": "18:30"},
            {"emp_code": "E2", "department": "IT", "joined_on": "2026-07-03", "shift_start": "09:30", "shift_end": "18:30"}
        ])
        
        # Wed Jul 1 to Mon Jul 6
        # Jul 1: 1 HC, 1 present -> 1.0 (raw), avg 1.0
        # Jul 2: 1 HC, 0 present -> 0.0, avg 0.5
        # Jul 3: 2 HC, 1 present (half) -> 0.25, avg 0.4167
        # Jul 4: Sat (not working day) -> raw None, avg 0.4167 (window unchanged size of non-nulls)
        # Jul 5: Sun (not working day) -> raw None, avg 0.4167
        # Jul 6: 2 HC, 2 present -> 1.0, avg 0.5625
        
        self.db.attendance_logs.insert_many([
            {"emp_code": "E1", "date": "2026-07-01", "status": "PRESENT", "half_day": False, "late_minutes": 0},
            {"emp_code": "E2", "date": "2026-07-03", "status": "PRESENT", "half_day": True, "late_minutes": 10},
            {"emp_code": "E1", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0},
            {"emp_code": "E2", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0}
        ])
        
        r = self.client.get("/analytics/departments/IT/trend?from=2026-07-01&to=2026-07-06")
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        self.assertEqual(len(items), 6)
        
        # Jul 1
        self.assertEqual(items[0]["date"], "2026-07-01")
        self.assertEqual(items[0]["headcount"], 1)
        self.assertEqual(items[0]["attendance_rate"], 1.0)
        self.assertEqual(items[0]["moving_avg_7d"], 1.0)
        
        # Jul 2
        self.assertEqual(items[1]["date"], "2026-07-02")
        self.assertEqual(items[1]["headcount"], 1)
        self.assertEqual(items[1]["attendance_rate"], 0.0)
        self.assertEqual(items[1]["moving_avg_7d"], 0.5)
        
        # Jul 3
        self.assertEqual(items[2]["date"], "2026-07-03")
        self.assertEqual(items[2]["headcount"], 2)
        self.assertEqual(items[2]["attendance_rate"], 0.25)
        self.assertEqual(items[2]["moving_avg_7d"], 0.4167)
        
        # Jul 4
        self.assertEqual(items[3]["date"], "2026-07-04")
        self.assertEqual(items[3]["is_working_day"], False)
        self.assertIsNone(items[3]["attendance_rate"])
        self.assertEqual(items[3]["moving_avg_7d"], 0.4167)
        
        # Jul 6
        self.assertEqual(items[5]["date"], "2026-07-06")
        self.assertEqual(items[5]["attendance_rate"], 1.0)
        self.assertEqual(items[5]["moving_avg_7d"], 0.5625)
