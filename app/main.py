"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
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
    # Compound indexes for fast filtering and sorting at scale
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
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
    att_d = date.fromisoformat(attendance_date)
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
    att_d = date.fromisoformat(attendance_date)
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
            date.fromisoformat(v)
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
                date.fromisoformat(date_from)
            except Exception:
                raise HTTPException(status_code=422, detail="Invalid date_from format")
            date_q["$gte"] = date_from
        if date_to:
            try:
                date.fromisoformat(date_to)
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


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
