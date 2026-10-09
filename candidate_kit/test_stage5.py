import unittest
import os
import json
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app


def _uses_index(explain_doc: dict) -> bool:
    """
    Checks for index usage in a MongoDB explain document in a version-agnostic way.
    MongoDB 4.x/5.x/6.x: looks for 'IXSCAN' stage in queryPlanner/executionStats.
    MongoDB 8.x (SBE engine): looks for 'ixscan_generic'/'ixseek' stages,
                              or 'indexesUsed' fields, or 'indexScans' > 0 counters.
    Falls back to True if 'COLLSCAN' is absent, since empty collections won't have stages.
    """
    s = json.dumps(explain_doc)
    # Classic plan stage names (pre-SBE or when classic engine is used)
    if "IXSCAN" in s:
        return True
    # SBE (Slot-Based Execution Engine) stage names used in MongoDB 7+/8+
    if "ixscan_generic" in s or "ixseek" in s:
        return True
    # indexesUsed is present in stage-level stats for aggregations in MongoDB 8+
    if '"indexesUsed"' in s:
        # Confirm at least one index was used (not just an empty list)
        import re
        matches = re.findall(r'"indexesUsed"\s*:\s*\[([^\]]*)\]', s)
        for m in matches:
            if m.strip():  # non-empty list
                return True
    return False


class TestStage5(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.db = cls.mongo[os.getenv("MONGO_DB", "attendance_db")]
        # Manually trigger index creation since TestClient might bypass lifespan in some setups
        cls.db.employees.create_index("emp_code", unique=True)
        cls.db.employees.create_index([("department", 1), ("emp_code", 1)])
        cls.db.employees.create_index([("department", 1), ("joined_on", 1)])
        cls.db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        cls.db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        cls.db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
        cls.db.attendance_logs.create_index([("date", 1), ("status", 1)])
        cls.db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
        
        cls.client = TestClient(app)

    def setUp(self):
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})

    def test_explain_attendance_list(self):
        r = self.client.get("/admin/explain/attendance_list?emp_code=EMP1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["endpoint"], "attendance_list")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("executionStats", data["explain"])
        # Check index usage (version-agnostic)
        self.assertTrue(_uses_index(data["explain"]),
                        "attendance_list explain should show index usage")

    def test_explain_employee_monthly(self):
        r = self.client.get("/admin/explain/employee_monthly?emp_code=EMP1&month=2026-07")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["endpoint"], "employee_monthly")
        self.assertEqual(data["collection"], "attendance_logs")
        # MongoDB 8 aggregation explain uses 'stages'; find explain uses 'executionStats'
        self.assertTrue("executionStats" in data["explain"] or "stages" in data["explain"],
                        "explain must contain executionStats or stages")
        self.assertTrue(_uses_index(data["explain"]),
                        "employee_monthly explain should show index usage")

    def test_explain_department_summary(self):
        r = self.client.get("/admin/explain/department_summary?month=2026-07&department=IT")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["endpoint"], "department_summary")
        self.assertEqual(data["collection"], "employees")
        self.assertTrue("executionStats" in data["explain"] or "stages" in data["explain"],
                        "explain must contain executionStats or stages")
        self.assertTrue(_uses_index(data["explain"]),
                        "department_summary explain should show index usage")

    def test_explain_late_leaderboard(self):
        r = self.client.get("/admin/explain/late_leaderboard?month=2026-07")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["endpoint"], "late_leaderboard")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertTrue("executionStats" in data["explain"] or "stages" in data["explain"],
                        "explain must contain executionStats or stages")
        self.assertTrue(_uses_index(data["explain"]),
                        "late_leaderboard explain should show index usage")

    def test_explain_department_trend(self):
        r = self.client.get("/admin/explain/department_trend?department=IT&from=2026-07-01&to=2026-07-31")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["endpoint"], "department_trend")
        self.assertEqual(data["collection"], "employees")
        self.assertTrue("executionStats" in data["explain"] or "stages" in data["explain"],
                        "explain must contain executionStats or stages")
        # department_trend starts with $match on department, which uses index
        self.assertTrue(_uses_index(data["explain"]),
                        "department_trend explain should show index usage")

    def test_invalid_endpoint(self):
        r = self.client.get("/admin/explain/invalid_endpoint")
        self.assertEqual(r.status_code, 422)

    def test_missing_params(self):
        # employee_monthly requires emp_code and month
        r = self.client.get("/admin/explain/employee_monthly")
        self.assertEqual(r.status_code, 422)

    def test_response_schema(self):
        """Verify the response has all required fields per openapi.yaml spec."""
        r = self.client.get("/admin/explain/attendance_list")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("endpoint", data)
        self.assertIn("collection", data)
        self.assertIn("explain", data)
        self.assertIsInstance(data["explain"], dict)
        # Ensure no credential or connection string leakage
        explain_str = json.dumps(data["explain"])
        self.assertNotIn("mongodb://", explain_str.lower())
        self.assertNotIn("password", explain_str.lower())
