# HROne Employee Attendance & Analytics API

A backend API built with FastAPI and MongoDB for employee attendance tracking, attendance regularization, and department analytics. The implementation follows the API contract in `openapi.yaml`, the data model in `DATA_MODEL.md`, and the business rules (R1–R10).

All application logic is implemented in `app/main.py`.

---

## Prerequisites

- **Python:** 3.11 or newer
- **MongoDB:** 6.0 or newer
- **Git**

---

## Setup & Installation

### 1. Clone the repository
```bash
git clone https://github.com/Guptsonu22/hrone-attendance-api.git
cd hrone-attendance-api
```

### 2. Create and activate a virtual environment

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

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies
```bash
python -m pip install -r requirements.txt
```

---

## Environment Configuration

Configure the following environment variables if needed:

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | MongoDB database name | `attendance_db` |

Set environment variables in your local environment. Never commit credentials, secrets, or `.env` files.

---

## Seed Sample Data

Populate the database with the supplied sample employees and attendance records:

```bash
python sample_seed.py
```

---

## Run the API

Start the FastAPI server:

```bash
uvicorn app.main:app --port 8000 --reload
```

### API Documentation:
- **Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

### Health Check:
```powershell
Invoke-RestMethod -Uri http://localhost:8000/health
```

---

## Implemented Endpoints

### Employee Management
- `GET /health` — Checks API and MongoDB connectivity.
- `POST /employees` — Creates an employee with unique employee-code enforcement.
- `GET /employees` — Lists employees with department filtering and pagination.

### Attendance Management
- `POST /attendance/punch-in` — Records employee punch-in and calculates lateness.
- `POST /attendance/punch-out` — Records punch-out and calculates work hours, overtime, and half-day status.
- `GET /attendance` — Lists attendance records with filtering and pagination.
- `PATCH /attendance/{emp_code}/{date}` — Corrects attendance records and maintains an audit history.

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` — Provides monthly employee attendance analytics.
- `GET /analytics/departments/summary` — Provides department-level attendance and headcount metrics.
- `GET /analytics/leaderboard/late` — Ranks employees by late minutes using competition ranking.
- `GET /analytics/departments/{department}/trend` — Provides daily department trends, date gap filling, and a 7-day moving average.

### Query Explain
- `GET /admin/explain/{endpoint}` — Provides MongoDB query execution-plan information for supported query keys.

---

## Testing

Run the available test scripts from the project root:

```powershell
.\.venv\Scripts\python test_rules_r1_r10.py
.\.venv\Scripts\python test_qa_full.py
```

The business-rule tests cover specified boundary conditions. The HTTP QA suite tests endpoints against a running API and configured MongoDB instance.

> **Note:** Ensure the API server and MongoDB are running before executing integration tests.

---

## Project Documentation

- `app/main.py` — API implementation.
- `requirements.txt` — Python dependencies.
- `openapi.yaml` — API contract.
- `DATA_MODEL.md` — Data model reference.
- `REVIEW.md` — Starter-code review and identified defects.
- `DECISIONS.md` — Architectural decisions and trade-offs.

---

## Security Notes

- Do not commit credentials, API keys, `.env` files, or database secrets.
- Configure MongoDB access through environment variables.
- Keep the repository free of unnecessary confidential assignment materials.

---

## Submission

- **Repository:** [https://github.com/Guptsonu22/hrone-attendance-api](https://github.com/Guptsonu22/hrone-attendance-api)
- The repository must remain publicly accessible until 31 October 2026, as specified in the assignment email.
- Submit the repository URL through the official HROne assignment submission form before the deadline.
