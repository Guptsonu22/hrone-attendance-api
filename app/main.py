"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Load local .env if present; existing environment variables take precedence
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Use tz_aware=True so PyMongo returns timezone-aware UTC datetimes
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all required indexes at startup idempotently
    # Unique index on employee code for uniqueness and concurrency safety
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")

    # Attendance logs unique natural key (emp_code, date) prevents race conditions
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # Compound indexes for fast filtering, punch-out matching, and sorting at scale
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime (naive or aware) to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def process_instant(punched_at: Optional[int] = None) -> tuple[datetime, int]:
    """
    R1: Instants are truncated to whole seconds before any calculation,
    and the truncated value is stored and returned.
    Returns (datetime_utc, epoch_ms).
    """
    if punched_at is not None:
        epoch_secs = punched_at // 1000
    else:
        now_utc = datetime.now(timezone.utc)
        epoch_secs = int(now_utc.timestamp())

    dt_utc = datetime.fromtimestamp(epoch_secs, tz=timezone.utc)
    ms = epoch_secs * 1000
    return dt_utc, ms


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift
    that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    h_end, m_end = map(int, shift_end.split(":"))
    end_time = time(h_end, m_end)

    if is_overnight and punch_in_ist.time() < end_time:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00
    (600 seconds) after shift_start; then late_minutes = whole minutes (floor)
    since shift_start, not since the grace period ended. Otherwise 0.
    """
    h_start, m_start = map(int, shift_start.split(":"))
    att_d = date_cls.fromisoformat(attendance_date)
    start_dt = datetime(att_d.year, att_d.month, att_d.day, h_start, m_start, 0, tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = int((punch_in_ist - start_dt).total_seconds())

    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up
    (not banker's rounding, not float rounding).
    """
    diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    val = Decimal(diff_sec) / Decimal(3600)
    rounded = val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if that is
    at least 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift_end falls on the next day.
    """
    h_end, m_end = map(int, shift_end.split(":"))
    att_d = date_cls.fromisoformat(attendance_date)
    end_dt = datetime(att_d.year, att_d.month, att_d.day, h_end, m_end, 0, tzinfo=IST)
    if shift_end <= shift_start:
        end_dt += timedelta(days=1)

    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = int((punch_out_ist - end_dt).total_seconds())

    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    return bool(work_hours is not None and work_hours < 4.50)


def format_attendance_record(doc: dict) -> dict:
    """Formats an attendance log document from MongoDB into OpenAPI schema format."""
    history_entries = []
    for h in doc.get("history", []) or []:
        entry = {
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for field, change in (h.get("changes") or {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                from_val = dt_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = dt_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
            entry["changes"][field] = {"from": from_val, "to": to_val}
        history_entries.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date_cls.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            valid = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
            if v not in valid:
                raise ValueError("Invalid status")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc)
    epoch_seconds = int(now_utc.timestamp())
    created_at_dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    created_at_ms = epoch_seconds * 1000

    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = created_at_ms
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for doc in cursor:
        if isinstance(doc.get("created_at"), datetime):
            doc["created_at"] = dt_to_epoch_ms(doc["created_at"])
        items.append(doc)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    dt_utc, epoch_ms = process_instant(body.punched_at)
    attendance_date = get_attendance_date(dt_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(dt_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": dt_utc,
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
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    dt_utc, epoch_ms = process_instant(body.punched_at)

    # 1. Check if there is an open record for this employee
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}, "punch_out": None},
        sort=[("punch_in", -1)],
    )

    if open_record:
        punch_in_utc = open_record["punch_in"]
        if punch_in_utc.tzinfo is None:
            punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

        diff_seconds = (dt_utc - punch_in_utc).total_seconds()
        if diff_seconds <= 0:
            raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
        if diff_seconds > 24 * 3600:
            raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

        record = open_record
    else:
        # No open record. Find the most recent record whose punch_in is <= dt_utc
        record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": dt_utc},
            },
            sort=[("punch_in", -1)],
        )

        if not record:
            # Check if there is a record with punch_in > dt_utc
            future_record = db.attendance_logs.find_one(
                {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$gt": dt_utc}},
                sort=[("punch_in", 1)],
            )
            if future_record:
                raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
            raise HTTPException(status_code=404, detail="No punch-in found")

        # Record exists but was already punched out
        raise HTTPException(status_code=409, detail="That record is already punched out")

    # Compute derived values per R3 - R5
    work_hours = compute_work_hours(punch_in_utc, dt_utc)
    overtime_mins = compute_overtime(dt_utc, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = compute_half_day(work_hours)

    # Atomic update to ensure two simultaneous punch-outs yield exactly one 200
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": dt_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    return format_attendance_record(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        date_q = {}
        if date_from:
            try:
                date_cls.fromisoformat(date_from)
            except Exception:
                raise HTTPException(status_code=422, detail="Invalid date_from format")
            date_q["$gte"] = date_from
        if date_to:
            try:
                date_cls.fromisoformat(date_to)
            except Exception:
                raise HTTPException(status_code=422, detail="Invalid date_to format")
            date_q["$lte"] = date_to
        q["date"] = date_q

    if status:
        valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
        if status not in valid_statuses:
            raise HTTPException(status_code=422, detail="Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    # Validate date format (YYYY-MM-DD)
    try:
        date_cls.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not existing:
        raise HTTPException(status_code=404, detail="Attendance record not found for date")

    # Determine final status
    final_status = body.status if body.status is not None else existing.get("status")

    if final_status in ("ABSENT", "LEAVE"):
        # Setting status ABSENT or LEAVE clears both punch times;
        # supplying punch_in or punch_out with it is 422
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when setting ABSENT or LEAVE",
            )
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status (PRESENT, WFH, ON_DUTY)
        if body.punch_in is not None:
            final_punch_in, _ = process_instant(body.punch_in)
        else:
            final_punch_in = existing.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_att_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_att_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            final_punch_out, _ = process_instant(body.punch_out)
        else:
            final_punch_out = existing.get("punch_out")

        if final_punch_out is not None:
            p_in = final_punch_in.replace(tzinfo=timezone.utc) if final_punch_in.tzinfo is None else final_punch_in
            p_out = final_punch_out.replace(tzinfo=timezone.utc) if final_punch_out.tzinfo is None else final_punch_out
            diff_sec = (p_out - p_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            final_work_hours = compute_work_hours(p_in, p_out)
            final_overtime_minutes = compute_overtime(p_out, emp["shift_end"], emp["shift_start"], date)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)

    # Detect actual changes
    changes = {}

    if existing.get("status") != final_status:
        changes["status"] = {"from": existing.get("status"), "to": final_status}

    ex_pi = existing.get("punch_in")
    if dt_to_epoch_ms(ex_pi) != dt_to_epoch_ms(final_punch_in):
        changes["punch_in"] = {"from": ex_pi, "to": final_punch_in}

    ex_po = existing.get("punch_out")
    if dt_to_epoch_ms(ex_po) != dt_to_epoch_ms(final_punch_out):
        changes["punch_out"] = {"from": ex_po, "to": final_punch_out}

    if existing.get("work_hours") != final_work_hours:
        changes["work_hours"] = {"from": existing.get("work_hours"), "to": final_work_hours}

    if existing.get("late_minutes", 0) != final_late_minutes:
        changes["late_minutes"] = {"from": existing.get("late_minutes", 0), "to": final_late_minutes}

    if existing.get("overtime_minutes", 0) != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": existing.get("overtime_minutes", 0), "to": final_overtime_minutes}

    if existing.get("half_day", False) != final_half_day:
        changes["half_day"] = {"from": existing.get("half_day", False), "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    # Create history entry
    at_dt, _ = process_instant()
    history_entry = {
        "at": at_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length
    current_history_len = len(existing.get("history") or [])

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": existing["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    return format_attendance_record(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(year, m)
    start_month_str = f"{month}-01"
    end_month_str = f"{month}-{days_in_month:02d}"

    start_d = date_cls(year, m, 1)
    end_d = date_cls(year, m, days_in_month)
    joined_d = date_cls.fromisoformat(emp["joined_on"])

    # Calculate working_days (Mon-Fri from max(start_d, joined_d) to end_d)
    if joined_d > end_d:
        working_days = 0
    else:
        calc_start = max(start_d, joined_d)
        cur = calc_start
        working_days = 0
        while cur <= end_d:
            if cur.weekday() < 5:  # Mon=0, ..., Fri=4
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_month_str, "$lte": end_month_str},
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
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],  # Mon=2..Fri=6 in Mongo $dayOfWeek
                                        ]
                                    },
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

    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        raw_pres = float(row.get("present_days", 0.0))
        present_days = float(Decimal(str(raw_pres)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        val = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

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


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #


@app.get(
    "/analytics/departments/summary",
    response_model=dict,
    tags=["analytics"],
    summary="Department summary for a month",
)
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    start_month_str = f"{month}-01"
    end_month_str = f"{month}-{num_days:02d}"

    match_filter: dict = {"joined_on": {"$lte": end_month_str}}
    if department:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_month_str]},
                                    {"$lte": ["$date", end_month_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$l.work_hours", None]},
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "total_wh_sum": {"$sum": "$emp_work_hours_sum"},
                "total_wh_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": "$headcount",
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$total_wh_count", 0]},
                        {"$round": [{"$divide": ["$total_wh_sum", "$total_wh_count"]}, 2]},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    for row in items:
        if row.get("avg_work_hours") is not None:
            row["avg_work_hours"] = float(Decimal(str(row["avg_work_hours"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        row["present_days"] = float(Decimal(str(row.get("present_days", 0.0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {"month": month, "items": items}


@app.get(
    "/analytics/leaderboard/late",
    response_model=dict,
    tags=["analytics"],
    summary="Late-comers leaderboard for a month",
)
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    start_month_str = f"{month}-01"
    end_month_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_month_str, "$lte": end_month_str},
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
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=dict,
    tags=["analytics"],
    summary="Daily attendance trend for a department",
)
def get_department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        from_d = date_cls.fromisoformat(date_from)
        to_d = date_cls.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date must not be before 'from' date")

    num_days = (to_d - from_d).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(status_code=404, detail="Department not found")

    dt_from = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    dt_to_bound = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_bound],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$curr_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_docs.count", 0]}, 0]},
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": [{"$ifNull": [{"$arrayElemAt": ["$hc_docs.count", 0]}, 0]}, 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        {"$arrayElemAt": ["$hc_docs.count", 0]},
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    for row in items:
        row["present_count"] = float(Decimal(str(row.get("present_count", 0.0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = float(Decimal(str(row["attendance_rate"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = float(Decimal(str(row["moving_avg_7d"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Endpoints
# --------------------------------------------------------------------------- #


class ExplainEndpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


@app.get(
    "/admin/explain/{endpoint}",
    response_model=dict,
    tags=["admin"],
    summary="MongoDB explain for an endpoint's main query",
)
def get_explain(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[StatusEnum] = Query(None),
    from_param: Optional[str] = Query(None, alias="from"),
    to_param: Optional[str] = Query(None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == ExplainEndpoint.attendance_list:
        collection = "attendance_logs"
        query: dict = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status.value
        if date_from or date_to:
            date_filter: dict = {}
            if date_from:
                date_filter["$gte"] = date_from
            if date_to:
                date_filter["$lte"] = date_to
            query["date"] = date_filter
        cmd = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

    elif endpoint == ExplainEndpoint.employee_monthly:
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="Missing required parameter: emp_code and month")
        collection = "attendance_logs"
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        start_month_str = f"{month}-01"
        end_month_str = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_month_str, "$lte": end_month_str},
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
                                        {
                                            "$in": [
                                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
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
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == ExplainEndpoint.department_summary:
        if not month:
            raise HTTPException(status_code=422, detail="Missing required parameter: month")
        collection = "employees"
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        start_month_str = f"{month}-01"
        end_month_str = f"{month}-{num_days:02d}"

        match_filter: dict = {"joined_on": {"$lte": end_month_str}}
        if department:
            match_filter["department"] = department

        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_month_str]},
                                        {"$lte": ["$date", end_month_str]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "emp_present_days": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {
                                                    "$in": [
                                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_work_hours_sum": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$logs",
                                        "as": "l",
                                        "cond": {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$l.work_hours", None]},
                                            ]
                                        },
                                    }
                                },
                                "as": "l",
                                "in": "$$l.work_hours",
                            }
                        }
                    },
                    "emp_work_hours_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        }
                    },
                    "emp_late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                            }
                        }
                    },
                    "emp_total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$ifNull": ["$$l.late_minutes", 0]},
                            }
                        }
                    },
                    "emp_leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "LEAVE"]},
                            }
                        }
                    },
                    "emp_on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$emp_present_days"},
                    "total_wh_sum": {"$sum": "$emp_work_hours_sum"},
                    "total_wh_count": {"$sum": "$emp_work_hours_count"},
                    "late_count": {"$sum": "$emp_late_count"},
                    "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                    "leave_count": {"$sum": "$emp_leave_count"},
                    "on_duty_count": {"$sum": "$emp_on_duty_count"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": "$headcount",
                    "present_days": "$present_days",
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$total_wh_count", 0]},
                            {"$round": [{"$divide": ["$total_wh_sum", "$total_wh_count"]}, 2]},
                            None,
                        ]
                    },
                    "late_count": "$late_count",
                    "total_late_minutes": "$total_late_minutes",
                    "leave_count": "$leave_count",
                    "on_duty_count": "$on_duty_count",
                }
            },
            {"$sort": {"department": 1}},
        ]
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == ExplainEndpoint.late_leaderboard:
        if not month:
            raise HTTPException(status_code=422, detail="Missing required parameter: month")
        collection = "attendance_logs"
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        start_month_str = f"{month}-01"
        end_month_str = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_month_str, "$lte": end_month_str},
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
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": "$total_late_minutes",
                    "late_count": "$late_count",
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            {
                "$match": {
                    "rank": {"$lte": limit}
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ])
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == ExplainEndpoint.department_trend:
        if not department or not from_param or not to_param:
            raise HTTPException(status_code=422, detail="Missing required parameters: department, from, and to")

        try:
            from_d = date_cls.fromisoformat(from_param)
            to_d = date_cls.fromisoformat(to_param)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if to_d < from_d:
            raise HTTPException(status_code=422, detail="'to' date must not be before 'from' date")

        num_days = (to_d - from_d).days + 1
        if num_days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        collection = "attendance_logs"
        dt_from = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
        dt_to_bound = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_param, "$lte": to_param}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date_dt": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [dt_from, dt_to_bound],
                    },
                }
            },
            {
                "$project": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "date_dt": 1,
                    "is_working_day": {
                        "$in": [
                            {"$dayOfWeek": "$date_dt"},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"curr_date": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$curr_date"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "hc_docs",
                }
            },
            {
                "$project": {
                    "date": 1,
                    "date_dt": 1,
                    "is_working_day": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_docs.count", 0]}, 0]},
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": [{"$ifNull": [{"$arrayElemAt": ["$hc_docs.count", 0]}, 0]}, 0]},
                                ]
                            },
                            {
                                "$round": [
                                    {
                                        "$divide": [
                                            "$present_count",
                                            {"$arrayElemAt": ["$hc_docs.count", 0]},
                                        ]
                                    },
                                    4,
                                ]
                            },
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date_dt": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, "current"]
                            },
                        }
                    },
                }
            },
            {"$sort": {"date_dt": 1}},
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$cond": [
                            {"$ne": ["$moving_avg_7d", None]},
                            {"$round": ["$moving_avg_7d", 4]},
                            None,
                        ]
                    },
                }
            },
        ]
        cmd = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
    else:
        raise HTTPException(status_code=422, detail="Invalid endpoint identifier")

    raw_explain = db.command("explain", cmd, verbosity="executionStats")
    return {
        "endpoint": endpoint.value,
        "collection": collection,
        "explain": jsonable_encoder(raw_explain),
    }
