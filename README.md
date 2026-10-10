# HROne Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, regularization, and department analytics. Built according to the specification in `openapi.yaml`, `DATA_MODEL.md`, and business rules (R1–R10).

All application logic is implemented in `app/main.py`.

---

## Prerequisites

- **Python:** 3.10 or newer (tested on Python 3.14)
- **MongoDB:** 6.0 or newer (uses `$densify` and `$setWindowFields` introduced in MongoDB 5.0+)

---

## Setup & Installation

### 1. Clone & Navigate to Repository
```bash
git clone <YOUR_REPOSITORY_URL>
cd hrone-attendance-api
```

### 2. Create and Activate Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.\.venv\Scripts\activate.bat
```

**Linux / macOS (Bash):**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

---

## Environment Configuration

The application reads configuration from environment variables or a local `.env` file:

| Variable | Description | Default Value |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | MongoDB database name | `attendance_db` |

*Note: Pre-existing environment variables take precedence over `.env`.*

---

## Seed Database

Populate initial sample employees and attendance records:
```bash
python sample_seed.py
```

---

## Run Application Server

Start the FastAPI application with Uvicorn on port 8000:
```bash
uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation (Swagger UI) is available at:
- `http://localhost:8000/docs`
- `http://localhost:8000/redoc`

---

## Implemented Endpoints

### Core Operations
- `GET /health` – Verifies MongoDB connectivity (`client.admin.command('ping')`).
- `POST /employees` – Creates employee with unique `emp_code` constraint.
- `GET /employees` – Lists employees sorted by `emp_code` with department filter and pagination.
- `POST /attendance/punch-in` – Creates punch-in record (concurrency-safe via unique compound key).
- `POST /attendance/punch-out` – Closes attendance record, computing work hours, overtime, and half-day status.
- `GET /attendance` – Paginated attendance records with date and status filters.
- `PATCH /attendance/{emp_code}/{date}` – Manual attendance correction with audit history and optimistic locking.

### Analytics Endpoints (MongoDB Aggregations)
- `GET /analytics/employees/{emp_code}/monthly` – Monthly attendance summary per employee.
- `GET /analytics/departments/summary` – Headcount, present days, average work hours, and late minutes per department.
- `GET /analytics/leaderboard/late` – Standard competition ranking ($1, 2, 2, 4$) for late minutes.
- `GET /analytics/departments/{department}/trend` – Daily attendance trend with `$densify` gap filling and 7-day moving average.

### Admin Endpoint
- `GET /admin/explain/{endpoint}` – Runs MongoDB `explain` with `executionStats` for every endpoint query or aggregation pipeline.

---

## Testing

Run the test suite using Python:
```bash
python scratch/test_analytics_endpoints.py
```
Or verify API health via curl / PowerShell:
```powershell
Invoke-RestMethod -Uri http://localhost:8000/health
```

---

## Submission Notes

- Implementation file: `app/main.py`
- Architectural decisions: `DECISIONS.md`
- Starter audit & defect fixes: `REVIEW.md`
- No secrets, `.env`, or Dockerfiles are included in the repository.
