"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Real environment variables take priority over default fallback
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING)],
        name="idx_employees_department",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="idx_employees_joined_on",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="idx_employees_dept_joined",
    )

    # attendance_logs collection
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_leaderboard_date_emp",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_late_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_attendance_emp_punch_in",
    )


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #

def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC BSON datetime to integer epoch milliseconds truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def from_epoch_ms(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Calculate IST attendance date.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    dt_ist = punch_in_dt.astimezone(IST)
    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end.split(":"))

    is_overnight = (sh_end_h, sh_end_m) <= (sh_start_h, sh_start_m)
    if is_overnight and (dt_ist.hour, dt_ist.minute) < (sh_end_h, sh_end_m):
        return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Late grace period 10 minutes.
    Strictly more than 10:00 elapsed from shift start -> whole minutes (floor) since shift_start.
    Otherwise 0.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_dt.astimezone(IST).replace(microsecond=0)

    sh_h, sh_m = map(int, shift_start.split(":"))
    base_d = date.fromisoformat(date_str)
    shift_start_dt = datetime(base_d.year, base_d.month, base_d.day, sh_h, sh_m, 0, tzinfo=IST)

    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)

    sec = Decimal(int((punch_out_dt - punch_in_dt).total_seconds()))
    hours = (sec / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(
    punch_out_dt: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str = "09:30",
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30, else 0.
    Handles overnight shifts where shift_end falls on the next calendar day.
    """
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)
    punch_out_ist = punch_out_dt.astimezone(IST).replace(microsecond=0)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    base_d = date.fromisoformat(date_str)
    shift_end_dt = datetime(base_d.year, base_d.month, base_d.day, end_h, end_m, 0, tzinfo=IST)

    if (end_h, end_m) <= (start_h, start_m):
        shift_end_dt += timedelta(days=1)

    overtime_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if overtime_seconds < 1800:
        return 0
    return int(overtime_seconds // 60)


def format_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format employee document for HTTP API response adhering to schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format attendance log document for HTTP API response adhering to schema."""
    history_entries = []
    for h in doc.get("history", []) or []:
        formatted_changes = {}
        for field, change in h.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                formatted_changes[field] = {
                    "from": to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val,
                    "to": to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val,
                }
            else:
                formatted_changes[field] = {"from": from_val, "to": to_val}

        at_val = h.get("at")
        history_entries.append({
            "at": to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": formatted_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_and_joined(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date YYYY-MM-DD")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping within timeout, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Unique emp_code enforced by unique database index.
    Concurrent duplicates yield exactly one 201 and the rest 409.
    """
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    now_sec = int(datetime.now(timezone.utc).timestamp())
    now_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees paginated, filtered by department, sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = [format_employee(emp) for emp in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Record a punch-in.
    Creates the day's record (R1, R2). Status PRESENT, WFH, or ON_DUTY.
    Race-safe: concurrent punch-ins for the same employee and date yield exactly one 201 and the rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        ts = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Calculate attendance date respecting overnight shift boundary (R1)
    att_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    late_mins = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out.
    Closes the employee's most recent record whose punch_in is at or before punched_at.
    Computes work_hours (R4), overtime_minutes (R3), and half_day (R5).
    Concurrency: two simultaneous punch-outs yield exactly one 200.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        ts = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Find employee's most recent record that has a punch_in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="Punch-in record not found")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after punch_in
    if ts <= punch_in_time:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    if (ts - punch_in_time) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    # If already punched out, return 409
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    # Compute derived values
    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_mins = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )
    half_day = work_hours < 4.50

    # Atomic update ensuring race safety: exactly one request gets 200, the other 409
    update_res = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime_mins,
            "half_day": half_day,
        }},
    )

    if update_res.modified_count == 0:
        raise HTTPException(status_code=409, detail="Already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records.
    Sorted by date descending, then emp_code ascending.
    Fast on 100k records, backed by indexes.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    q: Dict[str, Any] = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_record(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    """
    Regularize one day's record: recompute derived fields, append to history.
    Concurrent edits must not lose each other's history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    fields_set = body.model_fields_set

    # Setting ABSENT or LEAVE clears both punch times: supplying punch_in or punch_out is 422
    if body.status in ("ABSENT", "LEAVE"):
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE records must not supply punch_in or punch_out",
            )

    # Determine prospective status
    new_status = body.status if "status" in fields_set else record.get("status")

    # Determine prospective punch_in
    if "punch_in" in fields_set:
        new_punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else None
    else:
        new_punch_in = record.get("punch_in")
        if new_punch_in is not None and new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

    # Determine prospective punch_out
    if "punch_out" in fields_set:
        new_punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else None
    else:
        new_punch_out = record.get("punch_out")
        if new_punch_out is not None and new_punch_out.tzinfo is None:
            new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

    # Status rules
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in) > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect actual changes
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = bool(record.get("half_day", False))

    changes: Dict[str, Dict[str, Any]] = {}

    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_punch_in != new_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if old_punch_out != new_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="A request that changes nothing is rejected")

    now_sec = int(datetime.now(timezone.utc).timestamp())
    now_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    set_fields = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    # Optimistic locking: check current history array matches to prevent silent overwrite under concurrency
    current_history = record.get("history", []) or []
    update_res = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": current_history},
        {
            "$set": set_fields,
            "$push": {"history": history_entry},
        },
    )

    if update_res.modified_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent regularizations conflict")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee.
    working_days per R7 (Mon-Fri, from joined_on).
    present_days per R5-R7 (half day = 0.5, Mon-Fri only).
    attendance_pct = present_days / working_days * 100, 2 decimals half-up.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_num = map(int, month.split("-"))
    month_start = date(year, month_num, 1)
    month_end = date(year, month_num, monthrange(year, month_num)[1])

    joined_on = date.fromisoformat(emp["joined_on"])
    working_start = max(month_start, joined_on)

    working_days = 0
    if working_start <= month_end:
        cur = working_start
        while cur <= month_end:
            if cur.weekday() < 5:  # Mon=0 ... Fri=4
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "is_workday": {
                    "$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_workday",
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    stats = res[0] if res else {}

    raw_present_days = stats.get("present_days", 0.0)
    present_days = float(
        Decimal(str(raw_present_days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )

    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days)) * Decimal("100") / Decimal(working_days)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month (aggregation across collections).
    headcount per R9 (people with zero logs still count, people joining later do not).
    avg_work_hours is mean over present records with work_hours.
    """
    year, month_num = map(int, month.split("-"))
    month_start = date(year, month_num, 1).isoformat()
    month_end = date(year, month_num, monthrange(year, month_num)[1]).isoformat()

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": month_start, "$lte": month_end}
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$addFields": {
                "is_workday": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$logs.date",
                                    "onError": None,
                                    "onNull": None,
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {
                    "emp_code": "$emp_code",
                    "department": "$department",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_workday",
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$logs.half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    items_raw = list(db.employees.aggregate(pipeline))
    formatted_items = []
    for doc in items_raw:
        present_days = float(
            Decimal(str(doc["present_days"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        if doc["work_hours_count"] > 0:
            avg_wh = float(
                (Decimal(str(doc["work_hours_sum"])) / Decimal(doc["work_hours_count"])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        else:
            avg_wh = None

        formatted_items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"],
        })

    return {"month": month, "items": formatted_items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month.
    Standard competition ranking with ties (1, 2, 2, 4).
    limit applied after ranking (rank <= limit).
    """
    year, month_number = map(int, month.split("-"))
    last_day = monthrange(year, month_number)[1]

    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {
                    "$gte": start_date,
                    "$lte": end_date,
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department.
    Exactly one row per calendar day from from to to inclusive via $densify.
    Includes moving average over preceding window (up to 7 days).
    """
    try:
        start_d = date.fromisoformat(from_date)
        end_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if end_d < start_d:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")

    if (end_d - start_d).days > 91:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    # 404 for unknown department
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Unknown department")

    dept_emp_codes = [
        e["emp_code"]
        for e in db.employees.find({"department": department}, {"emp_code": 1})
    ]

    start_dt = datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc)
    end_dt_exclusive = datetime(end_d.year, end_d.month, end_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {
                        "dateString": from_date,
                        "format": "%Y-%m-%d",
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt_exclusive],
                },
            }
        },
        {
            "$addFields": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "day_of_week": {"$dayOfWeek": "$date"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", dept_emp_codes]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "logs_summary",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$logs_summary.present_count", 0]}, 0.0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$logs_summary.late_count", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": ["$present_count", "$headcount"]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    items_raw = list(db.employees.aggregate(pipeline))
    formatted_items = []
    for doc in items_raw:
        present_count = float(
            Decimal(str(doc["present_count"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        rate = doc["attendance_rate"]
        if rate is not None:
            rate_rounded = float(Decimal(str(rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            rate_rounded = None

        m_avg = doc["moving_avg_7d"]
        if m_avg is not None:
            m_avg_rounded = float(Decimal(str(m_avg)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            m_avg_rounded = None

        formatted_items.append({
            "date": doc["date"],
            "is_working_day": doc["is_working_day"],
            "headcount": doc["headcount"],
            "present_count": present_count,
            "late_count": doc["late_count"],
            "attendance_rate": rate_rounded,
            "moving_avg_7d": m_avg_rounded,
        })

    return {
        "department": department,
        "items": formatted_items,
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain for an endpoint's main query with executionStats verbosity.
    Accepts the same query parameters as the explained endpoint.
    Missing required parameters -> 422.
    """
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported endpoint. Choose one of: {sorted(allowed)}",
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

        q: Dict[str, Any] = {}
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
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": skip,
            "limit": page_size,
        }
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        year, month_num = map(int, month.split("-"))
        month_start = date(year, month_num, 1)
        month_end = date(year, month_num, monthrange(year, month_num)[1])

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end.isoformat(),
                    },
                }
            },
            {
                "$addFields": {
                    "date_obj": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "is_workday": {
                        "$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$is_workday",
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$ifNull": ["$half_day", False]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year, month_num = map(int, month.split("-"))
        month_start = date(year, month_num, 1).isoformat()
        month_end = date(year, month_num, monthrange(year, month_num)[1]).isoformat()

        match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department is not None:
            match_filter["department"] = department

        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "pipeline": [
                        {
                            "$match": {
                                "date": {"$gte": month_start, "$lte": month_end}
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year, month_number = map(int, month.split("-"))
        last_day = monthrange(year, month_number)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        ])

        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    else:  # department_trend
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' are required for department_trend")

        try:
            start_d = date.fromisoformat(from_date)
            end_d = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if end_d < start_d or (end_d - start_d).days > 91:
            raise HTTPException(status_code=422, detail="Invalid date range")

        start_dt = datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc)
        end_dt_exclusive = datetime(end_d.year, end_d.month, end_d.day, tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateFromString": {
                            "dateString": from_date,
                            "format": "%Y-%m-%d",
                        }
                    },
                }
            },
            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_dt_exclusive],
                    },
                }
            },
        ]
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "employees"

    result = db.command("explain", command, verbosity="executionStats")
    response_body = {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json_util.loads(json_util.dumps(result)),
    }
    return Response(
        content=json_util.dumps(response_body),
        media_type="application/json",
    )
