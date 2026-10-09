import unittest
import os
import threading
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient
import time

from app.main import app, IST

class TestStage2b(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.db = cls.mongo[os.getenv("MONGO_DB", "attendance_db")]
        
    def setUp(self):
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})

    def _setup_employee(self, client, shift_start="09:30", shift_end="18:30"):
        client.post("/employees", json={
            "emp_code": "EMP0001", "name": "Test", "email": "a@a.com", "department": "IT", 
            "shift_start": shift_start, "shift_end": shift_end, "joined_on": "2026-01-01"
        })

    def test_successful_punch_out_and_work_hours(self):
        with TestClient(app) as client:
            self._setup_employee(client)
            
            # Punch in at 09:30 IST (Epoch 1783310400000 = 2026-07-06T09:30:00+05:30)
            client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310400000})
            
            # Punch out at 18:30 IST (Epoch 1783342800000)
            r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783342800000})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["work_hours"], 9.0)
            self.assertEqual(data["overtime_minutes"], 0)
            self.assertEqual(data["half_day"], False)

    def test_overtime_29_vs_30(self):
        with TestClient(app) as client:
            self._setup_employee(client)
            client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310400000}) # 09:30
            
            # 18:59 (29 min late) -> Epoch 1783344540000
            # Since record is punched out, we must simulate a new day or just manually delete and recreate.
            r1 = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783344540000})
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.json()["overtime_minutes"], 0)
            
            # Reset
            self.db.attendance_logs.delete_many({})
            client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310400000}) # 09:30
            
            # 19:00 (30 min late) -> Epoch 1783344600000
            r2 = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783344600000})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.json()["overtime_minutes"], 30)

    def test_overnight_shift(self):
        with TestClient(app) as client:
            self._setup_employee(client, shift_start="22:00", shift_end="06:00")
            
            # Punch in at 21:55 (Epoch 1783355100000 = 2026-07-06T21:55)
            r_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783355100000})
            self.assertEqual(r_in.json()["date"], "2026-07-06")
            
            # Punch out next day at 06:10 (Epoch 1783384800000 = 2026-07-07T06:10)
            r_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783384800000})
            self.assertEqual(r_out.status_code, 200)
            data = r_out.json()
            # 21:55 to 06:10 is 8 hours 15 minutes = 8.25
            self.assertEqual(data["work_hours"], 8.25)
            # Overtime: 06:10 vs 06:00 (10 mins) -> 0 overtime
            self.assertEqual(data["overtime_minutes"], 0)

    def test_errors(self):
        with TestClient(app) as client:
            self._setup_employee(client)
            
            # Unknown employee
            r = client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": 1783310400000})
            self.assertEqual(r.status_code, 404)
            
            # Missing punch-in
            r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783310400000})
            self.assertEqual(r.status_code, 404)
            
            # Invalid timestamps (Before punch-in, exactly punch-in)
            client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310400000})
            r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783310400000})
            self.assertEqual(r.status_code, 422) # Exact same time
            r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783310300000})
            self.assertEqual(r.status_code, 404) # Before (no record <= ts)
            
            # 24-hour boundary
            r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783310400000 + 86401000})
            self.assertEqual(r.status_code, 422) # > 24 hours
            
            # Successful punch out
            client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783342800000})
            
            # Already closed
            r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783346400000})
            self.assertEqual(r.status_code, 409)

    def test_concurrent_punch_out(self):
        with TestClient(app) as client:
            self._setup_employee(client)
            client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310400000})
            
            def make_request(results, i):
                results[i] = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783342800000}).status_code
                
            results = [0, 0]
            t1 = threading.Thread(target=make_request, args=(results, 0))
            t2 = threading.Thread(target=make_request, args=(results, 1))
            
            # Execute nearly at same time
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            
            # One should succeed (200), one should fail due to atomic update (409)
            self.assertIn(200, results)
            self.assertIn(409, results)

if __name__ == '__main__':
    unittest.main()
