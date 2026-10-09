# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `department_summary` / `lifespan` | Missing index on `joined_on`, resulting in a `COLLSCAN` when fetching all departments. | `explain` returned `COLLSCAN` in plan when `department` wasn't supplied. | Added `db.employees.create_index("joined_on")` in `lifespan`. |
| 2 | `list_attendance`, `explain_endpoint` | `date_from`, `date_to` lacked regex format constraints. | Supplying `date_from=hello` returned 200 instead of 422. | Added `Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$')` to parameters. |
| 3 | `list_attendance`, `explain_endpoint` | `status` did not enforce valid enumerations (`PRESENT`, `ABSENT`, etc.). | Supplying `status=INVALID` returned 200 instead of 422. | Added `Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]` to enforce type. |
| 4 | `regularize_attendance` | `date` path parameter was loosely validated. | Supplying `12-34` in path would fail deeper with 404 instead of 422 at validation step. | Imported `Path` and used `Path(..., pattern=r'^\d{4}-\d{2}-\d{2}$')`. |

Also note anything you looked at and decided was **not** a defect, and why:

* **Ties in `late_leaderboard` using `$rank` instead of `$denseRank`**: Not a defect. The spec mandated standard competition ranking (1, 2, 2, 4), which matches `$rank` exactly.
* **Epoch millis whole-second truncation in `epoch_ms_to_ist`**: Not a defect. The `epoch_ms // 1000` is intended to precisely slice off milliseconds matching R1.
* **Joined_on month comparison `joined_on > month + "-31"`**: Not a defect. String comparison is lexically safe for ISO8601 dates, gracefully serving as a fast filter before doing accurate datetime looping.
