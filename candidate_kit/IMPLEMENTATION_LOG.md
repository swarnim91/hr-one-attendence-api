# Implementation Log

## Stage 4: Analytics Endpoints

* **`GET /analytics/employees/{emp_code}/monthly`**: Implemented using a MongoDB aggregation pipeline. Calculates `present_days`, `leave_days`, `late_count`, `total_late_minutes`, and `total_overtime_minutes` by matching records in the target month. Working days logic checks the calendar in Python to strictly count (Mon-Fri) and dynamically filters days before `joined_on`.
* **`GET /analytics/departments/summary`**: Aggregation joins `employees` and `attendance_logs` to calculate accurate headcounts (independent of attendance existence) and roll up metrics by department. Handled the `avg_work_hours` logic by accurately tracking sum and count inside the pipeline.
* **`GET /analytics/leaderboard/late`**: Created a Mongo pipeline combining `$match`, `$group`, `$lookup` (employees), and `$setWindowFields` to use the `$rank` accumulator for standard competition ranking (e.g., 1, 1, 3). Limit applied post-ranking to include all tied employees.
* **`GET /analytics/departments/{department}/trend`**: Uses the `$densify` stage to pad the calendar gap reliably. Lookup stages inject the dynamic headcount and attendance stats for each day. Follows up with `$setWindowFields` to efficiently process the 7-day moving average of the `attendance_rate` natively within the DB. Fixed a MongoDB bounds issue by securely passing python `datetime` instances.

### Design Decisions
* Handled complex constraints without touching or persisting any invalid data.
* Implemented `_compute_working_days` as a lightweight python function for precise weekday tracking relative to the `joined_on` boundaries for monthly metrics.
* Used MongoDB `$densify` for gap filling in the trend endpoint exactly as specified.
* Leveraged `$setWindowFields` heavily for ranking and moving average.
