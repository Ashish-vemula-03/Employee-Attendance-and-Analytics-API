"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

from pymongo.errors import DuplicateKeyError
import os
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

from bson import SON, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient

date_cls = date

load_dotenv()

mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_db_name = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(mongo_uri, tz_aware=True)
db = client[mongo_db_name]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    # Employees collection indexes
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="unique_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="idx_employees_dept_joined",
    )
    db.employees.create_index(
        [("joined_on", 1)],
        name="idx_employees_joined",
    )

    # Attendance logs collection indexes
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="unique_employee_attendance_data",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="idx_attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="idx_attendance_emp_punch_in",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_time_ist(t_str: str) -> time:
    return datetime.strptime(t_str, "%H:%M").time()


def resolve_shift_and_date(
    punch_in_ist: datetime,
    shift_start_str: str,
    shift_end_str: str,
):
    """
    Resolves the attendance calendar date and the shift boundaries in IST
    according to business rule R1.
    """
    s_start = parse_time_ist(shift_start_str)
    s_end = parse_time_ist(shift_end_str)
    is_overnight = s_end <= s_start
    p_time = punch_in_ist.time()

    if is_overnight and p_time < s_end:
        attendance_date = punch_in_ist.date() - timedelta(days=1)
    else:
        attendance_date = punch_in_ist.date()

    shift_start_dt = datetime.combine(attendance_date, s_start, tzinfo=IST)
    if is_overnight:
        shift_end_dt = datetime.combine(attendance_date + timedelta(days=1), s_end, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(attendance_date, s_end, tzinfo=IST)

    return attendance_date.isoformat(), shift_start_dt, shift_end_dt


def compute_late_minutes(
    punch_in: datetime,
    shift_start_datetime: datetime,
) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more
    than 10:00 after shift_start; then late_minutes = whole minutes (floor)
    since shift_start.
    """
    elapsed = punch_in - shift_start_datetime
    if elapsed <= timedelta(minutes=10):
        return 0
    return int(elapsed.total_seconds() // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime(
    punch_out: datetime,
    shift_end_datetime: datetime,
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if
    that is at least 30; otherwise 0.
    """
    elapsed = punch_out - shift_end_datetime
    minutes = int(elapsed.total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def epoch_ms_to_datetime(ms: int) -> datetime:
    """
    Converts epoch milliseconds to a UTC datetime truncated to whole seconds (R1).
    """
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(value: float | Decimal, decimals: int) -> float:
    d = Decimal(str(value))
    exp = Decimal("10") ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def count_working_days(month_start: date, month_end: date, joined_on: date) -> int:
    """
    R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners.
    """
    start_date = max(month_start, joined_on)
    if start_date > month_end:
        return 0
    working_days = 0
    cur = start_date
    while cur <= month_end:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def serialize_record(doc: dict) -> dict:
    """
    Serializes a MongoDB attendance_logs document into the OpenAPI AttendanceRecord shape.
    """
    punch_in_val = doc.get("punch_in")
    punch_out_val = doc.get("punch_out")

    punch_in_ms = datetime_to_epoch_ms(punch_in_val) if isinstance(punch_in_val, datetime) else punch_in_val
    punch_out_ms = datetime_to_epoch_ms(punch_out_val) if isinstance(punch_out_val, datetime) else punch_out_val

    history_list = []
    for h in doc.get("history", []):
        at_val = h.get("at")
        at_ms = datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
        changes_dict = {}
        for field, change in h.get("changes", {}).items():
            from_v = change.get("from")
            to_v = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = datetime_to_epoch_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = datetime_to_epoch_ms(to_v)
            changes_dict[field] = {"from": from_v, "to": to_v}

        history_list.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_dict,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_list,
    }


def serialize_employee(doc: dict) -> dict:
    """
    Serializes a MongoDB employees document into the OpenAPI Employee shape.
    """
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = datetime_to_epoch_ms(created_at)
    else:
        created_at_ms = created_at

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError("Time must use valid HH:MM format")
        if parsed.strftime("%H:%M") != value:
            raise ValueError("Time must use valid HH:MM format")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )


class RegularizeRequestIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    punch_out: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    page = page if isinstance(page, int) else (getattr(page, "default", 1) or 1)
    page_size = page_size if isinstance(page_size, int) else (getattr(page_size, "default", 20) or 20)
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="emp_code not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)

    ts_ist = ts_utc.astimezone(IST)
    attendance_date, shift_start_dt, _ = resolve_shift_and_date(
        ts_ist,
        emp["shift_start"],
        emp["shift_end"],
    )

    late_minutes = compute_late_minutes(ts_ist, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return serialize_record(doc)


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out  - Done
#   PATCH /attendance/{emp_code}/{date}  - Done
#   GET   /analytics/employees/{emp_code}/monthly  - Done
#   GET   /analytics/departments/summary  - Done
#   GET   /analytics/leaderboard/late - Done
#   GET   /analytics/departments/{department}/trend - Done
#   GET   /admin/explain/{endpoint}  - Done
# --------------------------------------------------------------------------- #


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="emp_code not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)

    ts_ist = ts_utc.astimezone(IST)

    # Find the most recent record with punch_in <= punched_at
    attendance = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts_utc},
        },
        sort=[("punch_in", -1)],
    )

    if attendance is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found for punch-out",
        )

    if attendance.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already punched out",
        )

    punch_in_utc = attendance["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after it -> 422
    if ts_utc <= punch_in_utc or ts_utc > punch_in_utc + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    punch_in_ist = punch_in_utc.astimezone(IST)
    work_hours = compute_work_hours(punch_in_ist, ts_ist)
    half_day = compute_half_day(work_hours)

    s_start = parse_time_ist(emp["shift_start"])
    s_end = parse_time_ist(emp["shift_end"])
    is_overnight = s_end <= s_start
    att_date = date.fromisoformat(attendance["date"])

    if is_overnight:
        shift_end_dt = datetime.combine(att_date + timedelta(days=1), s_end, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(att_date, s_end, tzinfo=IST)

    overtime_minutes = compute_overtime(ts_ist, shift_end_dt)

    result = db.attendance_logs.update_one(
        {
            "_id": attendance["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already punched out",
        )

    updated = db.attendance_logs.find_one({"_id": attendance["_id"]})
    return serialize_record(updated)


def validate_date_range(date_from: Optional[str], date_to: Optional[str]):
    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if start and end and start > end:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    page = page if isinstance(page, int) else (getattr(page, "default", 1) or 1)
    page_size = page_size if isinstance(page_size, int) else (getattr(page_size, "default", 20) or 20)
    validate_date_range(date_from, date_to)
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
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_record(d) for d in cursor]
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
    body: RegularizeRequestIn,
):
    try:
        att_date = date_cls.fromisoformat(date)
        if att_date.isoformat() != date:
            raise ValueError()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format",
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="emp_code not found")

    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Determine proposed status
    final_status = body.status.value if body.status is not None else current["status"]

    # Normalize current punch times to UTC-aware
    curr_punch_in = current.get("punch_in")
    curr_punch_out = current.get("punch_out")
    if curr_punch_in is not None and curr_punch_in.tzinfo is None:
        curr_punch_in = curr_punch_in.replace(tzinfo=timezone.utc)
    if curr_punch_out is not None and curr_punch_out.tzinfo is None:
        curr_punch_out = curr_punch_out.replace(tzinfo=timezone.utc)

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422.
    if body.status in (AttendanceStatus.ABSENT, AttendanceStatus.LEAVE):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch times when setting status to ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
    elif final_status in ("ABSENT", "LEAVE"):
        # Status remains ABSENT/LEAVE; cannot have punches
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch times for ABSENT or LEAVE record",
            )
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status: requires punch_in (supplied or existing)
        target_punch_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else curr_punch_in
        target_punch_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else curr_punch_out

        if target_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires a punch_in",
            )

        # Validate that punch_in stays on the record's attendance date under R1
        punch_in_ist = target_punch_in.astimezone(IST)
        resolved_date, _, _ = resolve_shift_and_date(
            punch_in_ist,
            emp["shift_start"],
            emp["shift_end"],
        )
        if resolved_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must stay on the record's attendance date in IST",
            )

        # Validate punch_out if present
        if target_punch_out is not None:
            if target_punch_out <= target_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in",
                )
            if target_punch_out > target_punch_in + timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be within 24 hours of punch_in",
                )

    # Recompute derived fields
    if final_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        s_start = parse_time_ist(emp["shift_start"])
        s_end = parse_time_ist(emp["shift_end"])
        is_overnight = s_end <= s_start
        shift_start_dt = datetime.combine(att_date, s_start, tzinfo=IST)
        new_late_minutes = compute_late_minutes(target_punch_in.astimezone(IST), shift_start_dt)

        if target_punch_out is not None:
            new_work_hours = compute_work_hours(
                target_punch_in.astimezone(IST),
                target_punch_out.astimezone(IST),
            )
            new_half_day = compute_half_day(new_work_hours)
            if is_overnight:
                shift_end_dt = datetime.combine(att_date + timedelta(days=1), s_end, tzinfo=IST)
            else:
                shift_end_dt = datetime.combine(att_date, s_end, tzinfo=IST)
            new_overtime_minutes = compute_overtime(target_punch_out.astimezone(IST), shift_end_dt)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Determine what actually changed
    old_state = {
        "status": current.get("status"),
        "punch_in": curr_punch_in,
        "punch_out": curr_punch_out,
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }

    new_state = {
        "status": final_status,
        "punch_in": target_punch_in,
        "punch_out": target_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    changes = {}
    for k in old_state:
        if old_state[k] != new_state[k]:
            changes[k] = {"from": old_state[k], "to": new_state[k]}

    # A request that changes nothing is 422
    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The request changes nothing on the attendance record",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking with current status, punches, and history length
    filter_q = {
        "_id": current["_id"],
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "history": {"$size": len(current.get("history", []))},
    }

    res = db.attendance_logs.update_one(
        filter_q,
        {
            "$set": {
                "status": final_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if res.modified_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; please retry",
        )

    updated = db.attendance_logs.find_one({"_id": current["_id"]})
    return serialize_record(updated)


def build_employee_monthly_pipeline(emp_code: str, month: str):
    month_start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    if month_start.month == 12:
        next_month_start = date(month_start.year + 1, 1, 1)
    else:
        next_month_start = date(month_start.year, month_start.month + 1, 1)

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month_start.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_calendar_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "_is_weekday": {
                    "$and": [
                        {"$ne": [{"$dayOfWeek": "$_calendar_date"}, 1]},
                        {"$ne": [{"$dayOfWeek": "$_calendar_date"}, 7]},
                    ]
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    "$_is_weekday",
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
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
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    try:
        month_start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM format")

    if month_start.month == 12:
        next_month_start = date(month_start.year + 1, 1, 1)
    else:
        next_month_start = date(month_start.year, month_start.month + 1, 1)

    month_end = next_month_start - timedelta(days=1)

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="emp_code not found")

    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = count_working_days(month_start, month_end, joined_on)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        data = agg_res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(
            (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100),
            2,
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    month_start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    if month_start.month == 12:
        next_month_start = date(month_start.year + 1, 1, 1)
    else:
        next_month_start = date(month_start.year, month_start.month + 1, 1)
    month_end = next_month_start - timedelta(days=1)

    emp_match = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month_start.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_calendar_date": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_is_weekday": {
                                "$and": [
                                    {"$ne": [{"$dayOfWeek": "$_calendar_date"}, 1]},
                                    {"$ne": [{"$dayOfWeek": "$_calendar_date"}, 7]},
                                ]
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "headcount": {"$first": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    "$logs._is_weekday",
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
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
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
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
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
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
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        headcount = int(r["headcount"])
        if headcount == 0:
            continue
        work_hours_count = r["work_hours_count"]
        if work_hours_count == 0:
            avg_work_hours = None
        else:
            avg_work_hours = round_half_up(
                Decimal(str(r["work_hours_sum"])) / Decimal(work_hours_count),
                2,
            )

        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_work_hours,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    month_start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    if month_start.month == 12:
        next_month_start = date(month_start.year + 1, 1, 1)
    else:
        next_month_start = date(month_start.year, month_start.month + 1, 1)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month_start.isoformat(),
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
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
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    limit = limit if isinstance(limit, int) else (getattr(limit, "default", 10) or 10)
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items,
    }


def build_department_trend_pipeline(department: str, from_date: date, to_date: date):
    day_count = (to_date - from_date).days + 1

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "department": {"$first": "$department"},
            }
        },
        {
            "$set": {
                "_days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": from_date.isoformat(),
                                                "format": "%Y-%m-%d",
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_days"},
        {
            "$lookup": {
                "from": "employees",
                "let": {"dept": "$department", "d": "$_days"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$dept"]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "eligible",
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$eligible"},
                "emp_codes": "$eligible.emp_code",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes", "d": "$_days"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$eq": ["$date", "$$d"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "date": "$_days",
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "_cal": {
                    "$dateFromString": {
                        "dateString": "$_days",
                        "format": "%Y-%m-%d",
                    }
                },
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$ne": [{"$dayOfWeek": "$_cal"}, 1]},
                        {"$ne": [{"$dayOfWeek": "$_cal"}, 7]},
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$_moving_avg_raw", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$_moving_avg_raw", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if isinstance(from_date, str):
        from_date = date_cls.fromisoformat(from_date)
    if isinstance(to_date, str):
        to_date = date_cls.fromisoformat(to_date)
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    day_count = (to_date - from_date).days + 1
    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    dept_exists = db.employees.find_one({"department": department}, {"_id": 1})
    if dept_exists is None:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    page = page if isinstance(page, int) else (getattr(page, "default", 1) or 1)
    page_size = page_size if isinstance(page_size, int) else (getattr(page_size, "default", 20) or 20)
    limit = limit if isinstance(limit, int) else (getattr(limit, "default", 10) or 10)
    if isinstance(from_date, str):
        from_date = date_cls.fromisoformat(from_date)
    if isinstance(to_date, str):
        to_date = date_cls.fromisoformat(to_date)

    if endpoint not in supported:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of: {', '.join(sorted(supported))}",
        )

    if endpoint == "attendance_list":
        validate_date_range(date_from, date_to)
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
            q["status"] = status.value

        skip = (page - 1) * page_size
        find_cmd = SON([
            ("find", "attendance_logs"),
            ("filter", q),
            ("sort", SON([("date", -1), ("emp_code", 1)])),
            ("skip", skip),
            ("limit", page_size),
        ])
        explain_cmd = SON([("explain", find_cmd), ("verbosity", "executionStats")])
        res = db.command(explain_cmd)
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        agg_cmd = SON([
            ("aggregate", "attendance_logs"),
            ("pipeline", pipeline),
            ("cursor", {}),
        ])
        explain_cmd = SON([("explain", agg_cmd), ("verbosity", "executionStats")])
        res = db.command(explain_cmd)
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )
        pipeline = build_department_summary_pipeline(month, department)
        agg_cmd = SON([
            ("aggregate", "employees"),
            ("pipeline", pipeline),
            ("cursor", {}),
        ])
        explain_cmd = SON([("explain", agg_cmd), ("verbosity", "executionStats")])
        res = db.command(explain_cmd)
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        agg_cmd = SON([
            ("aggregate", "attendance_logs"),
            ("pipeline", pipeline),
            ("cursor", {}),
        ])
        explain_cmd = SON([("explain", agg_cmd), ("verbosity", "executionStats")])
        res = db.command(explain_cmd)
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        agg_cmd = SON([
            ("aggregate", "employees"),
            ("pipeline", pipeline),
            ("cursor", {}),
        ])
        explain_cmd = SON([("explain", agg_cmd), ("verbosity", "executionStats")])
        res = db.command(explain_cmd)
        collection_name = "employees"

    # Convert BSON structures (e.g. Timestamp, ObjectId) safely to JSON types
    clean_explain = json.loads(json_util.dumps(res))

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": clean_explain,
    }