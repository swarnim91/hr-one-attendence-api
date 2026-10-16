# HROne Employee Attendance & Analytics API

A production-ready REST API for employee attendance management and HR analytics, built with **FastAPI**, **MongoDB**, and **Pydantic v2**. Implements all 5 stages of the HROne backend challenge with 38/38 tests passing.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Data Model](#data-model)
- [API Endpoints](#api-endpoints)
- [Business Rules](#business-rules)
- [Local Setup](#local-setup)
- [Running Tests](#running-tests)
- [Design Decisions](#design-decisions)
- [Defects Found & Fixed](#defects-found--fixed)

---

## Overview

This API manages employees and their daily attendance. It supports:
- **Punch-in / Punch-out** with automatic computation of late minutes, overtime, work hours, and half-day flags.
- **Regularization** (manual HR corrections) with a full audit trail.
- **Analytics** — monthly summaries per employee, department roll-ups, late leaderboards, and 7-day attendance trend with moving average.
- **Admin explain** endpoint that exposes MongoDB query execution plans for debugging.

All datetime instants are stored as BSON dates in UTC and returned as epoch **milliseconds** at the API boundary. Calendar dates and times-of-day are plain `"YYYY-MM-DD"` / `"HH:MM"` strings.

---

## Features

| Stage | Feature |
|-------|---------|
| 1 | Employee CRUD (create, get, list, update, delete) |
| 2A | Attendance: punch-in with late-minutes calculation |
| 2B | Attendance: punch-out with work-hours, overtime, half-day |
| 3 | Attendance list & regularization with history audit trail |
| 4 | Analytics: monthly summary, department summary, late leaderboard, trend |
| 5 | `GET /admin/explain/{endpoint}` — MongoDB query explain |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Framework | [FastAPI](https://fastapi.tiangolo.com/) >= 0.110 |
| ASGI Server | [Uvicorn](https://www.uvicorn.org/) >= 0.29 |
| Database | MongoDB 6.0+ (tested on 7.0) |
| ODM / Driver | [PyMongo](https://pymongo.readthedocs.io/) >= 4.7 |
| Validation | [Pydantic](https://docs.pydantic.dev/) v2 >= 2.6 |
| Config | python-dotenv >= 1.0 |
| Language | Python 3.11+ |

---

## Project Structure

```
hrone-attendance-api-/
├── LICENSE
└── candidate_kit/
    ├── app/
    │   ├── __init__.py
    │   └── main.py            # All application code (single-file, as required)
    ├── sample_data/           # Nine sample MongoDB Extended JSON documents
    ├── .env.example           # Environment variable template
    ├── .gitignore
    ├── DATA_MODEL.md          # MongoDB schema specification
    ├── DECISIONS.md           # Key design decisions
    ├── IMPLEMENTATION_LOG.md  # Stage-by-stage implementation notes
    ├── NEXT_STEPS.md          # Future improvements
    ├── openapi.yaml           # Full OpenAPI 3.1 contract + business rules R1-R10
    ├── PROBLEM_STATEMENT.docx # Original challenge brief
    ├── PROJECT_STATUS.md      # Current status (38/38 tests passing)
    ├── requirements.txt
    ├── REVIEW.md              # Defects found & fixed
    ├── sample_seed.py         # Seeds sample_data/ into MongoDB
    ├── test_stage2a.py        # Punch-in tests
    ├── test_stage2b.py        # Punch-out tests
    ├── test_stage3.py         # Attendance list & regularization tests
    ├── test_stage4.py         # Analytics tests
    └── test_stage5.py         # Admin explain tests
```

---

## Data Model

Two MongoDB collections:

### `employees`

| Field | Type | Notes |
|-------|------|-------|
| `_id` | ObjectId | Internal; never exposed via API |
| `emp_code` | string | Business key (e.g. `EMP0001`). Unique, immutable |
| `name` | string | |
| `email` | string | |
| `department` | string | Case-sensitive |
| `shift_start` | `"HH:MM"` | IST, 24-hour |
| `shift_end` | `"HH:MM"` | IST. If <= shift_start, shift is overnight |
| `joined_on` | `"YYYY-MM-DD"` | First day of employment |
| `created_at` | BSON date | Set by the API at creation |

### `attendance_logs`

| Field | Type | Notes |
|-------|------|-------|
| `emp_code` | string | FK -> `employees.emp_code` |
| `date` | `"YYYY-MM-DD"` | Attendance day (IST). For overnight: day the shift started |
| `status` | string | `PRESENT` \| `WFH` \| `ON_DUTY` \| `ABSENT` \| `LEAVE` |
| `punch_in` | BSON date \| null | |
| `punch_out` | BSON date \| null | Null until punch-out |
| `work_hours` | double \| null | Computed at punch-out |
| `late_minutes` | int | Computed at punch-in (0 if within grace) |
| `overtime_minutes` | int | Computed at punch-out |
| `half_day` | bool | True if `work_hours < 4.5` |
| `history` | array | Audit trail of manual corrections |

See [`DATA_MODEL.md`](DATA_MODEL.md) for full schema details and nine sample documents.

---

## API Endpoints

### Employees

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/employees` | Create employee |
| `GET` | `/employees/{emp_code}` | Get employee |
| `GET` | `/employees` | List employees (filter by department) |
| `PUT` | `/employees/{emp_code}` | Update employee |
| `DELETE` | `/employees/{emp_code}` | Delete employee |

### Attendance

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/attendance/punch-in` | Punch in (creates log, computes `late_minutes`) |
| `POST` | `/attendance/punch-out` | Punch out (computes `work_hours`, `overtime_minutes`, `half_day`) |
| `GET` | `/attendance` | List attendance logs (filters: `emp_code`, `date_from`, `date_to`, `status`) |
| `PUT` | `/attendance/{emp_code}/{date}` | Regularize (manual correction + history append) |

### Analytics

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| `GET` | `/analytics/departments/summary` | Department-level roll-up (optional department filter) |
| `GET` | `/analytics/leaderboard/late` | Top-N late employees with competition ranking |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance rate + 7-day moving average |

### Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/explain/{endpoint}` | MongoDB `explain` for a given analytics endpoint |

Full contract with request/response schemas and business rules in [`openapi.yaml`](openapi.yaml).

---

## Business Rules

| Rule | Summary |
|------|---------|
| **R1** | Overnight shift: if `shift_end <= shift_start`, the shift spans midnight. `date` = day shift started |
| **R2** | Late grace period is 15 minutes. `late_minutes = max(0, punch_in_ist - shift_start - 15)` |
| **R3** | Overtime threshold is 30 minutes. `overtime_minutes = max(0, punch_out_ist - shift_end - 30)` |
| **R4** | `work_hours = (punch_out - punch_in)` in fractional hours, rounded to 2 decimal places |
| **R5** | `half_day = True` if `work_hours < 4.5` |
| **R6** | `PRESENT`, `WFH`, `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not |
| **R7** | Analytics only count days on or after the employee's `joined_on` date |
| **R8** | Working days = Mon-Fri (weekends excluded from all analytics denominators) |
| **R9** | Monthly analytics exclude days before `joined_on` from the working-days count |
| **R10** | Regularization recomputes derived fields if punch times change |

---

## Local Setup

### Prerequisites

- Python 3.11+
- MongoDB 6.0+ (or MongoDB Atlas M0 free tier)

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/swarnim91/hr-one-attendence-api.git
cd hr-one-attendence-api/candidate_kit

# 2. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and set:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=hrone

# 5. (Optional) Start MongoDB via Docker
docker run -d -p 27017:27017 mongo:7

# 6. Seed sample data
python sample_seed.py

# 7. Start the API server
uvicorn app.main:app --port 8000 --reload
```

The API will be available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`

---

## Running Tests

```bash
# Run all test stages
pytest test_stage2a.py test_stage2b.py test_stage3.py test_stage4.py test_stage5.py -v

# Run a specific stage
pytest test_stage4.py -v
```

**Current test status: 38/38 passing**

---

## Design Decisions

### Indexes

All indexes are created at application startup via the `lifespan` handler:

| Collection | Index | Purpose |
|-----------|-------|---------|
| `employees` | `emp_code` (unique) | Uniqueness enforcement & fast lookup |
| `employees` | `(department, emp_code)` | Department list filtering |
| `employees` | `(department, joined_on)` + `joined_on` | Analytics headcount without COLLSCAN |
| `attendance_logs` | `(emp_code, date)` (unique) | Idempotent punch-in, concurrency safety |
| `attendance_logs` | `(date DESC, emp_code)` | Sorted list queries |
| `attendance_logs` | `(date, status)` | Aggregate filtering |
| `attendance_logs` | `(emp_code, date, status)` | Composite aggregate filtering |
| `attendance_logs` | `(date, late_minutes)` | Late leaderboard |

### Concurrency Safety

Punch-in uses MongoDB's unique `(emp_code, date)` index as an atomic guard. A duplicate punch-in raises a `DuplicateKeyError`, which the API maps to `409 Conflict`.

### Competition Ranking (Ties)

The late leaderboard uses `$setWindowFields` with `$rank` accumulator — standard competition ranking (1, 2, 2, 4). The limit is applied as `rank <= N`, so a multi-way tie at the boundary returns more than N rows (correct behavior per spec).

### Headcount in Department Summary

The pipeline starts from `employees` (not `attendance_logs`) and uses `$lookup` with `preserveNullAndEmptyArrays: true` so employees with zero attendance records still count toward `headcount`.

### Scalability Note

At 100x data scale, monthly analytics should be precomputed nightly via `$out` / `$merge` aggregations (materialized views). On-the-fly grouping and lookups across millions of rows per request is unsustainable.

---

## Defects Found & Fixed

| # | Location | Issue | Fix |
|---|----------|-------|-----|
| 1 | `lifespan` | Missing `joined_on` index caused `COLLSCAN` in `department_summary` | Added `db.employees.create_index("joined_on")` |
| 2 | `list_attendance`, `explain_endpoint` | `date_from`/`date_to` accepted any string, returning 200 instead of 422 | Added `pattern=r'^\d{4}-\d{2}-\d{2}$'` to `Query()` |
| 3 | `list_attendance`, `explain_endpoint` | `status` accepted any value, returning 200 instead of 422 | Typed as `Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]` |
| 4 | `regularize_attendance` | `date` path param loosely validated, causing a deep 404 instead of 422 | Used `Path(..., pattern=r'^\d{4}-\d{2}-\d{2}$')` |

See [`REVIEW.md`](REVIEW.md) for the complete defect analysis.

---

## License

[MIT](../LICENSE)
