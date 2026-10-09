# DECISIONS.md

1. **Indexes.** 
   - `employees: emp_code` (unique) to enforce uniqueness.
   - `employees: [("department", 1), ("emp_code", 1)]` for list filtering.
   - `employees: [("department", 1), ("joined_on", 1)]` and `("joined_on")` for accurate analytics headcount matching without `COLLSCAN`.
   - `attendance_logs: [("emp_code", 1), ("date", 1)]` (unique) for idempotency and concurrency locks.
   - `attendance_logs: [("date", -1), ("emp_code", 1)]` for sorted list queries.
   - `attendance_logs: [("date", 1), ("status", 1)]` and `[("emp_code", 1), ("date", 1), ("status", 1)]` for aggregate filtering.
   - `attendance_logs: [("date", 1), ("late_minutes", 1)]` for the late leaderboard index.

2. **Punch-in race.**
   A unique compound index exists on `(emp_code, date)`. If two duplicate requests arrive at the exact same instant, MongoDB's atomic document insertion ensures only one thread succeeds. The competing thread throws a `DuplicateKeyError`, which the API handles to return a `409` conflict, keeping data consistent.

3. **Ties.**
   When employees tie on late minutes, the MongoDB pipeline uses `$setWindowFields` with the `$rank` accumulator. This guarantees standard competition ranking (e.g. 1, 2, 2, 4). Limit is applied strictly as `rank <= limit`, correctly yielding more than 10 rows if a multi-way tie intersects the cutoff line.

4. **Headcount.**
   The `department_summary` endpoint starts its pipeline by matching on `employees` (not logs), then performs a `$lookup` onto `attendance_logs`. It then preserves empty arrays via `$unwind: {preserveNullAndEmptyArrays: true}` so employees with zero logs still count as `$sum: 1` towards `headcount`.

5. **One thing you would change.**
   If scaling to 100x data, I would schedule a cron-job to run `$out` or `$merge` aggregations incrementally at night (or use a materialized view), precomputing monthly metrics. Doing `group` and `lookup` on the fly for 10 million rows per request is unsustainable.
