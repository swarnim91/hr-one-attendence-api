"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt
from typing import Literal
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def get_ist_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST).replace(microsecond=0)

def epoch_ms_to_ist(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms // 1000, tz=timezone.utc).astimezone(IST).replace(microsecond=0)

def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))
    is_overnight = (h_e < h_s) or (h_e == h_s and m_e <= m_s)
    
    if is_overnight:
        if punch_in.hour < h_e or (punch_in.hour == h_e and punch_in.minute < m_e):
            return (punch_in.date() - timedelta(days=1)).isoformat()
    return punch_in.date().isoformat()

def get_shift_start_datetime(date_str: str, shift_start: str) -> datetime:
    base_date = datetime.fromisoformat(date_str)
    h_s, m_s = map(int, shift_start.split(":"))
    return base_date.replace(hour=h_s, minute=m_s, tzinfo=IST)

def get_shift_end_datetime(date_str: str, shift_start: str, shift_end: str) -> datetime:
    base_date = datetime.fromisoformat(date_str)
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))
    is_overnight = (h_e < h_s) or (h_e == h_s and m_e <= m_s)
    end_date = base_date + timedelta(days=1) if is_overnight else base_date
    return end_date.replace(hour=h_e, minute=m_e, tzinfo=IST)

def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    start = get_shift_start_datetime(date_str, shift_start)
    delta_seconds = (punch_in - start).total_seconds()
    if delta_seconds > 600:
        return int(delta_seconds // 60)
    return 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = (punch_out - punch_in).total_seconds() / 3600
    return float(Decimal(str(hours)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    end = get_shift_end_datetime(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def _dt_to_epoch_ms(dt: datetime) -> int:
    """Convert a datetime to epoch milliseconds.
    PyMongo returns naive UTC datetimes; treat naive datetimes as UTC.
    Tz-aware datetimes (from our own code) are converted directly.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def employee_to_dict(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    if "created_at" in d and isinstance(d["created_at"], datetime):
        d["created_at"] = _dt_to_epoch_ms(d["created_at"])
    return d

def attendance_to_dict(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)

    if d.get("punch_in") and isinstance(d["punch_in"], datetime):
        d["punch_in"] = _dt_to_epoch_ms(d["punch_in"])
    if d.get("punch_out") and isinstance(d["punch_out"], datetime):
        d["punch_out"] = _dt_to_epoch_ms(d["punch_out"])

    if "history" in d:
        for entry in d["history"]:
            if "at" in entry and isinstance(entry["at"], datetime):
                entry["at"] = _dt_to_epoch_ms(entry["at"])
            if "changes" in entry:
                for key in ["punch_in", "punch_out"]:
                    if key in entry["changes"]:
                        frm = entry["changes"][key]["from"]
                        to  = entry["changes"][key]["to"]
                        entry["changes"][key]["from"] = _dt_to_epoch_ms(frm) if isinstance(frm, datetime) else frm
                        entry["changes"][key]["to"]   = _dt_to_epoch_ms(to)  if isinstance(to,  datetime) else to
    return d

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r'^EMP\d{4,6}$')
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field("18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)

ALLOW_ABSENT_LEAVE = {"ABSENT", "LEAVE"}
ALLOW_PRESENCE     = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES        = ALLOW_ABSENT_LEAVE | ALLOW_PRESENCE

DERIVED_FIELDS = {"work_hours", "late_minutes", "overtime_minutes", "half_day"}

class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump()
    doc["created_at"] = get_ist_now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_dict(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": [employee_to_dict(i) for i in items], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
        
    ts = epoch_ms_to_ist(body.punched_at) if body.punched_at else get_ist_now()
    d = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    return attendance_to_dict(doc)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from > date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size))
    
    return {"items": [attendance_to_dict(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
        
    ts = epoch_ms_to_ist(body.punched_at) if body.punched_at else get_ist_now()
    
    # Find most recent record where punch_in <= ts
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)]
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
        
    punch_in_ts = record["punch_in"].replace(tzinfo=timezone.utc).astimezone(IST)
    
    if ts <= punch_in_ts:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if (ts - punch_in_ts).total_seconds() > 86400:
        raise HTTPException(422, "punched_at cannot exceed 24 hours after punch_in")
        
    work_hours = compute_work_hours(punch_in_ts, ts)
    overtime_minutes = compute_overtime(ts, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = work_hours < 4.50
    
    # Atomic update
    updated_record = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day
        }},
        return_document=True
    )
    
    if not updated_record:
        raise HTTPException(409, "already punched out")
        
    return attendance_to_dict(updated_record)

@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # 1. Look up employee and record
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, f"no attendance record for {emp_code} on {date}")

    # 2. Resolve effective values (merge patch fields onto current record)
    new_status   = body.status if body.status is not None else record["status"]
    new_punch_in_ts  = epoch_ms_to_ist(body.punch_in)  if body.punch_in  is not None else (
        record["punch_in"].replace(tzinfo=timezone.utc).astimezone(IST) if record.get("punch_in") else None
    )
    new_punch_out_ts = epoch_ms_to_ist(body.punch_out) if body.punch_out is not None else (
        record["punch_out"].replace(tzinfo=timezone.utc).astimezone(IST) if record.get("punch_out") else None
    )

    # 3. Status-based punch-time rules
    if new_status in ALLOW_ABSENT_LEAVE:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
        new_punch_in_ts  = None
        new_punch_out_ts = None
    else:  # presence status
        if new_punch_in_ts is None:
            raise HTTPException(422, f"{new_status} requires a punch_in")
        # punch_in must belong to the record's attendance date under R1
        computed_date = get_attendance_date(new_punch_in_ts, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422,
                f"punch_in resolves to attendance date {computed_date}, expected {date}")

    # 4. punch_out ordering and 24h limit
    if new_punch_in_ts and new_punch_out_ts:
        if new_punch_out_ts <= new_punch_in_ts:
            raise HTTPException(422, "punch_out must be strictly after punch_in")
        if (new_punch_out_ts - new_punch_in_ts).total_seconds() > 86400:
            raise HTTPException(422, "punch_out cannot exceed 24 hours after punch_in")

    # 5. Recalculate derived fields
    if new_status in ALLOW_ABSENT_LEAVE:
        new_late_minutes     = 0
        new_work_hours       = None
        new_overtime_minutes = 0
        new_half_day         = False
    elif new_punch_in_ts and new_punch_out_ts:
        new_late_minutes     = compute_late_minutes(new_punch_in_ts, date, emp["shift_start"])
        new_work_hours       = compute_work_hours(new_punch_in_ts, new_punch_out_ts)
        new_overtime_minutes = compute_overtime(new_punch_out_ts, date, emp["shift_start"], emp["shift_end"])
        new_half_day         = new_work_hours < 4.50
    else:  # presence, no punch_out yet
        new_late_minutes     = compute_late_minutes(new_punch_in_ts, date, emp["shift_start"])
        new_work_hours       = None
        new_overtime_minutes = 0
        new_half_day         = False

    # 6. Detect actual changes — compare against current stored values
    old_pi  = record["punch_in"].replace(tzinfo=timezone.utc).astimezone(IST) if record.get("punch_in")  else None
    old_po  = record["punch_out"].replace(tzinfo=timezone.utc).astimezone(IST) if record.get("punch_out") else None

    def _ts_eq(a, b):
        """Compare two nullable aware datetimes ignoring sub-second (already truncated)."""
        if a is None and b is None: return True
        if a is None or b is None:  return False
        return int(a.timestamp()) == int(b.timestamp())

    field_map = {
        "status":           (record["status"],                             new_status),
        "punch_in":         (old_pi,                                       new_punch_in_ts),
        "punch_out":        (old_po,                                       new_punch_out_ts),
        "work_hours":       (record.get("work_hours"),                     new_work_hours),
        "late_minutes":     (record.get("late_minutes", 0),                new_late_minutes),
        "overtime_minutes": (record.get("overtime_minutes", 0),            new_overtime_minutes),
        "half_day":         (record.get("half_day", False),                new_half_day),
    }

    changes = {}
    for field, (old_val, new_val) in field_map.items():
        if field in ("punch_in", "punch_out"):
            if not _ts_eq(old_val, new_val):
                changes[field] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                changes[field] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, "no changes detected")

    # 7. Build history entry (BSON datetimes stored; serialised to epoch-ms at response boundary)
    history_entry = {
        "at":     get_ist_now(),
        "by":     body.regularized_by,
        "reason": body.reason,
        "changes": {
            field: {
                "from": v["from"] if field not in ("punch_in", "punch_out") else
                        (v["from"] if v["from"] is None else v["from"]),
                "to":   v["to"]   if field not in ("punch_in", "punch_out") else
                        (v["to"]   if v["to"]   is None else v["to"]),
            }
            for field, v in changes.items()
        },
    }

    # 8. Build $set payload
    set_payload = {
        "status":           new_status,
        "punch_in":         new_punch_in_ts,
        "punch_out":        new_punch_out_ts,
        "work_hours":       new_work_hours,
        "late_minutes":     new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day":         new_half_day,
    }

    # 9. Atomic update with optimistic concurrency: filter on _id AND current history length
    #    If a concurrent request already appended an entry, history_len differs and we get None back.
    history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": history_len}},
        {"$set": set_payload, "$push": {"history": history_entry}},
        return_document=True,
    )

    if updated is None:
        raise HTTPException(409, "concurrent modification detected, please retry")

    return attendance_to_dict(updated)

# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
import calendar

def _get_month_bounds(month: str):
    y, m = map(int, month.split('-'))
    _, last_day = calendar.monthrange(y, m)
    start_str = f"{y:04d}-{m:02d}-01"
    end_str = f"{y:04d}-{m:02d}-{last_day:02d}"
    return start_str, end_str

def _compute_working_days(month: str, joined_on: str) -> int:
    y, m = map(int, month.split('-'))
    _, last_day = calendar.monthrange(y, m)
    
    start_d = 1
    if joined_on.startswith(month):
        start_d = max(1, int(joined_on[-2:]))
    elif joined_on > month + "-31":
        return 0
        
    working_days = 0
    for d in range(start_d, last_day + 1):
        if calendar.weekday(y, m, d) < 5:
            working_days += 1
    return working_days

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
        
    start_date, end_date = _get_month_bounds(month)
    working_days = _compute_working_days(month, emp["joined_on"])
    
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start_date, "$lte": end_date}
        }},
        {"$addFields": {
            "is_weekday": {
                "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]
            }
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            "$is_weekday",
                            {"$in": ["$status", list(ALLOW_PRESENCE)]}
                        ]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]
    
    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        stats = {
            "present_days": 0.0, "leave_days": 0, "late_count": 0,
            "total_late_minutes": 0, "total_overtime_minutes": 0
        }
    else:
        stats = res[0]
        
    att_pct = None
    if working_days > 0:
        att_pct = float(Decimal(str(stats["present_days"] / working_days * 100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(stats["present_days"]),
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": att_pct
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), department: Optional[str] = None):
    start_date, end_date = _get_month_bounds(month)
    
    emp_match = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department
        
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"e_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$e_code"]},
                    "date": {"$gte": start_date, "$lte": end_date}
                }},
                {"$addFields": {
                    "is_weekday": {
                        "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True
        }},
        {"$group": {
            "_id": {"dept": "$department", "emp": "$emp_code"},
            "emp_present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            "$logs.is_weekday",
                            {"$in": ["$logs.status", list(ALLOW_PRESENCE)]}
                        ]},
                        {"$cond": ["$logs.half_day", 0.5, 1.0]},
                        0
                    ]
                }
            },
            "emp_work_hours_sum": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            {"$in": ["$logs.status", list(ALLOW_PRESENCE)]},
                            {"$ne": ["$logs.work_hours", None]}
                        ]},
                        "$logs.work_hours",
                        0
                    ]
                }
            },
            "emp_work_hours_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            {"$in": ["$logs.status", list(ALLOW_PRESENCE)]},
                            {"$ne": ["$logs.work_hours", None]}
                        ]},
                        1, 0
                    ]
                }
            },
            "emp_late_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                        ]},
                        1, 0
                    ]
                }
            },
            "emp_total_late": {
                "$sum": {
                    "$cond": [
                        {"$eq": [{"$type": "$logs"}, "object"]},
                        {"$ifNull": ["$logs.late_minutes", 0]},
                        0
                    ]
                }
            },
            "emp_leave": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            {"$eq": ["$logs.status", "LEAVE"]}
                        ]},
                        1, 0
                    ]
                }
            },
            "emp_onduty": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            {"$eq": ["$logs.status", "ON_DUTY"]}
                        ]},
                        1, 0
                    ]
                }
            }
        }},
        {"$group": {
            "_id": "$_id.dept",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$emp_present_days"},
            "total_work_hours_sum": {"$sum": "$emp_work_hours_sum"},
            "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
            "late_count": {"$sum": "$emp_late_count"},
            "total_late_minutes": {"$sum": "$emp_total_late"},
            "leave_count": {"$sum": "$emp_leave"},
            "on_duty_count": {"$sum": "$emp_onduty"}
        }},
        {"$sort": {"_id": 1}}
    ]
    
    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        avg_wh = None
        if d["total_work_hours_count"] > 0:
            avg_wh = float(Decimal(str(d["total_work_hours_sum"] / d["total_work_hours_count"])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        
        items.append({
            "department": d["_id"],
            "headcount": d["headcount"],
            "present_days": float(d["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"]
        })
        
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    start_date, end_date = _get_month_bounds(month)
    
    pipeline = [
        {"$match": {
            "date": {"$gte": start_date, "$lte": end_date},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]
    
    if department:
        pipeline.append({"$match": {"emp.department": department}})
        
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {
                    "$rank": {}
                }
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    ])
    
    docs = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for d in docs:
        items.append({
            "rank": d["rank"],
            "emp_code": d["_id"],
            "name": d["emp"]["name"],
            "department": d["emp"]["department"],
            "total_late_minutes": d["total_late_minutes"],
            "late_count": d["late_count"]
        })
        
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., alias="to", pattern=r'^\d{4}-\d{2}-\d{2}$')
):
    if to_date < from_date:
        raise HTTPException(422, "to < from")
        
    f_dt = datetime.fromisoformat(from_date)
    t_dt = datetime.fromisoformat(to_date)
    if (t_dt - f_dt).days > 92:
        raise HTTPException(422, "range exceeds 92 days")
        
    emp_exists = db.employees.find_one({"department": department})
    if not emp_exists:
        raise HTTPException(404, "unknown department")

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date": f_dt}},
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [f_dt, t_dt + timedelta(days=1)]
            }
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"curr_date": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$curr_date"]}
                }},
                {"$count": "headcount"}
            ],
            "as": "hc_info"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$first": "$hc_info.headcount"}, 0]}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"curr_date": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$date", "$$curr_date"]}
                }},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp"
                }},
                {"$match": {"emp.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", list(ALLOW_PRESENCE)]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                        }
                    }
                }}
            ],
            "as": "att_info"
        }},
        {"$addFields": {
            "present_count": {"$ifNull": [{"$first": "$att_info.present_count"}, 0]},
            "late_count": {"$ifNull": [{"$first": "$att_info.late_count"}, 0]}
        }},
        {"$addFields": {
            "attendance_rate_raw": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$eq": ["$attendance_rate_raw", None]},
                    None,
                    {"$divide": [
                        {"$floor": {"$add": [{"$multiply": ["$attendance_rate_raw", 10000]}, 0.5]}},
                        10000
                    ]}
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"]
                    }
                }
            }
        }},
        {"$addFields": {
            "moving_avg_7d": {
                "$cond": [
                    {"$eq": ["$moving_avg_7d_raw", None]},
                    None,
                    {"$divide": [
                        {"$floor": {"$add": [{"$multiply": ["$moving_avg_7d_raw", 10000]}, 0.5]}},
                        10000
                    ]}
                ]
            }
        }},
        {"$project": {
            "_id": 0, "hc_info": 0, "att_info": 0, "attendance_rate_raw": 0, "moving_avg_7d_raw": 0
        }}
    ]
    
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}

# --------------------------------------------------------------------------- #
# Admin Endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint_name}")
def explain_endpoint(
    endpoint_name: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: Optional[str] = Query(None, alias="to", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint_name not in valid:
        raise HTTPException(422, "invalid endpoint")

    coll_name = ""
    explain_cmd = {}

    if endpoint_name == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        skip = (page - 1) * page_size
        coll_name = "attendance_logs"
        explain_cmd = {
            "find": coll_name,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }

    elif endpoint_name == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "missing params")
        start_date, end_date = _get_month_bounds(month)
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }},
            {"$addFields": {
                "is_weekday": {
                    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]
                }
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                "$is_weekday",
                                {"$in": ["$status", list(ALLOW_PRESENCE)]}
                            ]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }}
        ]
        coll_name = "attendance_logs"
        explain_cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {}
        }

    elif endpoint_name == "department_summary":
        if not month: raise HTTPException(422, "missing params")
        start_date, end_date = _get_month_bounds(month)
        emp_match = {"joined_on": {"$lte": end_date}}
        if department: emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"e_code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$emp_code", "$$e_code"]},
                        "date": {"$gte": start_date, "$lte": end_date}
                    }},
                    {"$addFields": {
                        "is_weekday": {
                            "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]
                        }
                    }}
                ],
                "as": "logs"
            }},
            {"$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }},
            {"$group": {
                "_id": {"dept": "$department", "emp": "$emp_code"},
                "emp_present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": [{"$type": "$logs"}, "object"]},
                                "$logs.is_weekday",
                                {"$in": ["$logs.status", list(ALLOW_PRESENCE)]}
                            ]},
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": [{"$type": "$logs"}, "object"]},
                                {"$in": ["$logs.status", list(ALLOW_PRESENCE)]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]},
                            "$logs.work_hours",
                            0
                        ]
                    }
                },
                "emp_work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": [{"$type": "$logs"}, "object"]},
                                {"$in": ["$logs.status", list(ALLOW_PRESENCE)]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]},
                            1, 0
                        ]
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": [{"$type": "$logs"}, "object"]},
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                            ]},
                            1, 0
                        ]
                    }
                },
                "emp_total_late": {
                    "$sum": {
                        "$cond": [
                            {"$eq": [{"$type": "$logs"}, "object"]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "emp_leave": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": [{"$type": "$logs"}, "object"]},
                                {"$eq": ["$logs.status", "LEAVE"]}
                            ]},
                            1, 0
                        ]
                    }
                },
                "emp_onduty": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$eq": [{"$type": "$logs"}, "object"]},
                                {"$eq": ["$logs.status", "ON_DUTY"]}
                            ]},
                            1, 0
                        ]
                    }
                }
            }},
            {"$group": {
                "_id": "$_id.dept",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "total_work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late"},
                "leave_count": {"$sum": "$emp_leave"},
                "on_duty_count": {"$sum": "$emp_onduty"}
            }},
            {"$sort": {"_id": 1}}
        ]
        coll_name = "employees"
        explain_cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {}
        }

    elif endpoint_name == "late_leaderboard":
        if not month: raise HTTPException(422, "missing params")
        start_date, end_date = _get_month_bounds(month)
        pipeline = [
            {"$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"}
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}}
        ])
        coll_name = "attendance_logs"
        explain_cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {}
        }

    elif endpoint_name == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "missing params")
        f_dt = datetime.fromisoformat(from_date)
        t_dt = datetime.fromisoformat(to_date)
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "date": f_dt}},
            {"$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [f_dt, t_dt + timedelta(days=1)]
                }
            }},
            {"$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]}
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {"$match": {
                        "department": department,
                        "$expr": {"$lte": ["$joined_on", "$$curr_date"]}
                    }},
                    {"$count": "headcount"}
                ],
                "as": "hc_info"
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc_info.headcount"}, 0]}
            }},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$date", "$$curr_date"]}
                    }},
                    {"$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "emp"
                    }},
                    {"$match": {"emp.department": department}},
                    {"$group": {
                        "_id": None,
                        "present_count": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$status", list(ALLOW_PRESENCE)]},
                                    {"$cond": ["$half_day", 0.5, 1.0]},
                                    0
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {
                                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                            }
                        }
                    }}
                ],
                "as": "att_info"
            }},
            {"$addFields": {
                "present_count": {"$ifNull": [{"$first": "$att_info.present_count"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$att_info.late_count"}, 0]}
            }},
            {"$addFields": {
                "attendance_rate_raw": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$eq": ["$attendance_rate_raw", None]},
                        None,
                        {"$divide": [
                            {"$floor": {"$add": [{"$multiply": ["$attendance_rate_raw", 10000]}, 0.5]}},
                            10000
                        ]}
                    ]
                }
            }},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }},
            {"$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d_raw", None]},
                        None,
                        {"$divide": [
                            {"$floor": {"$add": [{"$multiply": ["$moving_avg_7d_raw", 10000]}, 0.5]}},
                            10000
                        ]}
                    ]
                }
            }},
            {"$project": {
                "_id": 0, "hc_info": 0, "att_info": 0, "attendance_rate_raw": 0, "moving_avg_7d_raw": 0
            }}
        ]
        coll_name = "employees"
        explain_cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {}
        }

    res = db.command("explain", explain_cmd, verbosity="executionStats")
    
    return {
        "endpoint": endpoint_name,
        "collection": coll_name,
        "explain": res
    }
