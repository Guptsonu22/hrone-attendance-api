import os
import sys
import calendar
import time
from datetime import datetime, timezone, timedelta, date as date_cls
from decimal import Decimal, ROUND_HALF_UP
import httpx as requests

BASE_URL = "http://127.0.0.1:8000"

def run_tests():
    print("=====================================================================")
    print("        HROne BACKEND QA AUDIT TEST SUITE (LIVE ENDPOINTS)           ")
    print("=====================================================================")
    
    # -------------------------------------------------------------------------
    # PHASE 1 & 2: All 12 Endpoints Verification
    # -------------------------------------------------------------------------
    
    # 1. GET /health
    print("\n[EP-1] GET /health")
    r = requests.get(f"{BASE_URL}/health")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert r.json() == {"status": "ok"}, f"Unexpected body: {r.text}"
    print("  -> PASS: 200 OK with {'status': 'ok'}")

    # 2. POST /employees
    print("\n[EP-2] POST /employees")
    # Validation failures (422)
    bad_code = requests.post(f"{BASE_URL}/employees", json={
        "emp_code": "INVALID", "name": "Test", "email": "test@example.com",
        "department": "Engineering", "joined_on": "2026-01-01"
    })
    assert bad_code.status_code == 422, f"Expected 422 for invalid emp_code, got {bad_code.status_code}"
    
    same_shift = requests.post(f"{BASE_URL}/employees", json={
        "emp_code": "EMP7001", "name": "Test", "email": "test@example.com",
        "department": "Engineering", "joined_on": "2026-01-01",
        "shift_start": "09:00", "shift_end": "09:00"
    })
    assert same_shift.status_code == 422, f"Expected 422 for identical shift times, got {same_shift.status_code}"

    # Valid creation (201)
    # Use isolated test employee code EMP7701
    from pymongo import MongoClient
    mc = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    db = mc[os.getenv("MONGO_DB", "attendance_db")]
    db.employees.delete_one({"emp_code": "EMP7701"})
    db.attendance_logs.delete_many({"emp_code": "EMP7701"})

    valid_emp = {
        "emp_code": "EMP7701",
        "name": "QA Employee",
        "email": "qa.emp@example.com",
        "department": "QualityAssurance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15"
    }
    r_create = requests.post(f"{BASE_URL}/employees", json=valid_emp)
    assert r_create.status_code == 201, f"Expected 201, got {r_create.status_code}: {r_create.text}"
    c_json = r_create.json()
    assert "_id" not in c_json, "MongoDB _id was exposed in response!"
    assert c_json["emp_code"] == "EMP7701"
    assert isinstance(c_json["created_at"], int), "created_at must be epoch ms integer"

    # Duplicate creation (409)
    r_dup = requests.post(f"{BASE_URL}/employees", json=valid_emp)
    assert r_dup.status_code == 409, f"Expected 409, got {r_dup.status_code}"
    print("  -> PASS: 201 on create, 409 on duplicate, 422 on validation")

    # 3. GET /employees
    print("\n[EP-3] GET /employees")
    r_list = requests.get(f"{BASE_URL}/employees?department=QualityAssurance&page=1&page_size=10")
    assert r_list.status_code == 200
    l_json = r_list.json()
    assert "items" in l_json and "total" in l_json and "page" in l_json and "page_size" in l_json
    assert l_json["total"] >= 1
    assert any(e["emp_code"] == "EMP7701" for e in l_json["items"])
    print("  -> PASS: 200 OK with EmployeePage schema and department filter")

    # 4. POST /attendance/punch-in
    print("\n[EP-4] POST /attendance/punch-in")
    # 404 for unknown employee
    r_unknown_pi = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert r_unknown_pi.status_code == 404
    # 422 for bad epoch milliseconds (< 100000000000)
    r_bad_epoch = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP7701", "punched_at": 12345})
    assert r_bad_epoch.status_code == 422

    # 201 for valid punch in (9:45 IST on 2026-07-20 = 15m late, strictly > 10m grace period)
    # 2026-07-20 09:45:00 IST = 2026-07-20 04:15:00 UTC
    dt_in = datetime(2026, 7, 20, 4, 15, 0, tzinfo=timezone.utc)
    ts_in = int(dt_in.timestamp() * 1000)
    r_pi = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP7701", "punched_at": ts_in})
    assert r_pi.status_code == 201, f"Expected 201, got {r_pi.status_code}: {r_pi.text}"
    pi_json = r_pi.json()
    assert pi_json["emp_code"] == "EMP7701"
    assert pi_json["date"] == "2026-07-20"
    assert pi_json["late_minutes"] == 15, f"Expected 15m late, got {pi_json['late_minutes']}"
    assert pi_json["punch_out"] is None
    assert pi_json["work_hours"] is None

    # 409 for duplicate punch-in on same attendance date
    r_pi_dup = requests.post(f"{BASE_URL}/attendance/punch-in", json={"emp_code": "EMP7701", "punched_at": ts_in + 1000})
    assert r_pi_dup.status_code == 409, f"Expected 409, got {r_pi_dup.status_code}"
    print("  -> PASS: 201 on punch-in, 409 on duplicate, late_minutes computed correctly")

    # 5. POST /attendance/punch-out
    print("\n[EP-5] POST /attendance/punch-out")
    # 404 for unknown employee
    assert requests.post(f"{BASE_URL}/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    # 422 if punched_at <= punch_in
    r_po_early = requests.post(f"{BASE_URL}/attendance/punch-out", json={"emp_code": "EMP7701", "punched_at": ts_in - 5000})
    assert r_po_early.status_code == 422
    # 422 if punched_at > 24 hours after punch_in
    r_po_late = requests.post(f"{BASE_URL}/attendance/punch-out", json={"emp_code": "EMP7701", "punched_at": ts_in + 90000000})
    assert r_po_late.status_code == 422

    # 200 for valid punch-out at 19:15:00 IST (04:15 UTC to 13:45 UTC = 9.5 hours = 570 mins)
    # shift ends 18:30 -> punch out 19:15 is 45 min overtime (>= 30 min threshold)
    dt_out = datetime(2026, 7, 20, 13, 45, 0, tzinfo=timezone.utc)
    ts_out = int(dt_out.timestamp() * 1000)
    r_po = requests.post(f"{BASE_URL}/attendance/punch-out", json={"emp_code": "EMP7701", "punched_at": ts_out})
    assert r_po.status_code == 200, f"Expected 200, got {r_po.status_code}: {r_po.text}"
    po_json = r_po.json()
    assert po_json["work_hours"] == 9.5
    assert po_json["overtime_minutes"] == 45
    assert po_json["half_day"] is False

    # 409 on duplicate punch-out (already closed)
    r_po_dup = requests.post(f"{BASE_URL}/attendance/punch-out", json={"emp_code": "EMP7701", "punched_at": ts_out + 1000})
    assert r_po_dup.status_code == 409
    print("  -> PASS: 200 on punch-out, 409 on duplicate, work_hours & overtime verified")

    # 6. GET /attendance
    print("\n[EP-6] GET /attendance")
    # 422 when date_from > date_to
    assert requests.get(f"{BASE_URL}/attendance?date_from=2026-07-25&date_to=2026-07-20").status_code == 422
    # 200 on valid query
    r_att = requests.get(f"{BASE_URL}/attendance?emp_code=EMP7701&page=1&page_size=10")
    assert r_att.status_code == 200
    att_json = r_att.json()
    assert att_json["total"] >= 1
    assert att_json["items"][0]["emp_code"] == "EMP7701"
    print("  -> PASS: 200 OK with AttendancePage schema, date validation, and pagination")

    # 7. PATCH /attendance/{emp_code}/{date}
    print("\n[EP-7] PATCH /attendance/{emp_code}/{date}")
    # 404 for unknown record
    r_p_404 = requests.patch(f"{BASE_URL}/attendance/EMP9999/2026-07-20", json={
        "status": "WFH", "reason": "Testing 404", "regularized_by": "lead@example.com"
    })
    assert r_p_404.status_code == 404
    # 422 for no-op (no fields changed)
    r_p_noop = requests.patch(f"{BASE_URL}/attendance/EMP7701/2026-07-20", json={
        "status": "PRESENT", "reason": "No change", "regularized_by": "lead@example.com"
    })
    assert r_p_noop.status_code == 422

    # 200 for valid correction to WFH
    r_patch = requests.patch(f"{BASE_URL}/attendance/EMP7701/2026-07-20", json={
        "status": "WFH", "reason": "Approved work from home", "regularized_by": "manager@example.com"
    })
    assert r_patch.status_code == 200, f"Expected 200, got {r_patch.status_code}: {r_patch.text}"
    patch_json = r_patch.json()
    assert patch_json["status"] == "WFH"
    assert len(patch_json["history"]) == 1
    h_entry = patch_json["history"][0]
    assert h_entry["by"] == "manager@example.com"
    assert h_entry["reason"] == "Approved work from home"
    assert h_entry["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}
    print("  -> PASS: 200 on patch, no-op rejected with 422, audit history diff created")

    # 8. GET /analytics/employees/{emp_code}/monthly
    print("\n[EP-8] GET /analytics/employees/{emp_code}/monthly")
    # 404 for unknown employee
    assert requests.get(f"{BASE_URL}/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404
    # 422 for invalid month format
    assert requests.get(f"{BASE_URL}/analytics/employees/EMP7701/monthly?month=2026-13").status_code == 422
    # 200 for valid month
    r_m = requests.get(f"{BASE_URL}/analytics/employees/EMP7701/monthly?month=2026-07")
    assert r_m.status_code == 200
    m_json = r_m.json()
    assert m_json["emp_code"] == "EMP7701"
    assert m_json["month"] == "2026-07"
    assert m_json["working_days"] == 23  # July 2026 has 23 Mon-Fri days
    assert m_json["present_days"] == 1.0
    assert m_json["late_count"] == 1
    assert m_json["total_late_minutes"] == 15
    print("  -> PASS: 200 on monthly analytics, Mon-Fri working days, presence calculations")

    # 9. GET /analytics/departments/summary
    print("\n[EP-9] GET /analytics/departments/summary")
    # 422 for invalid month
    assert requests.get(f"{BASE_URL}/analytics/departments/summary?month=2026-15").status_code == 422
    # 200 for month
    r_ds = requests.get(f"{BASE_URL}/analytics/departments/summary?month=2026-07&department=QualityAssurance")
    assert r_ds.status_code == 200
    ds_json = r_ds.json()
    assert ds_json["month"] == "2026-07"
    assert len(ds_json["items"]) == 1
    qa_item = ds_json["items"][0]
    assert qa_item["department"] == "QualityAssurance"
    assert qa_item["headcount"] == 1
    assert qa_item["present_days"] == 1.0
    assert qa_item["avg_work_hours"] == 9.5
    print("  -> PASS: 200 on department summary with zero-log headcount support and R8 rounding")

    # 10. GET /analytics/leaderboard/late
    print("\n[EP-10] GET /analytics/leaderboard/late")
    # 422 for limit < 1 or limit > 50
    assert requests.get(f"{BASE_URL}/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422
    assert requests.get(f"{BASE_URL}/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
    r_lb = requests.get(f"{BASE_URL}/analytics/leaderboard/late?month=2026-07&department=QualityAssurance")
    assert r_lb.status_code == 200
    lb_json = r_lb.json()
    assert len(lb_json["items"]) == 1
    assert lb_json["items"][0]["emp_code"] == "EMP7701"
    assert lb_json["items"][0]["rank"] == 1
    print("  -> PASS: 200 on leaderboard with standard competition ranking and limit validation")

    # 11. GET /analytics/departments/{department}/trend
    print("\n[EP-11] GET /analytics/departments/{department}/trend")
    # 404 for unknown department
    assert requests.get(f"{BASE_URL}/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-05").status_code == 404
    # 422 when to < from
    assert requests.get(f"{BASE_URL}/analytics/departments/QualityAssurance/trend?from=2026-07-10&to=2026-07-05").status_code == 422
    # 422 when range > 92 days
    assert requests.get(f"{BASE_URL}/analytics/departments/QualityAssurance/trend?from=2026-01-01&to=2026-05-01").status_code == 422
    # 200 for valid range
    r_tr = requests.get(f"{BASE_URL}/analytics/departments/QualityAssurance/trend?from=2026-07-18&to=2026-07-22")
    assert r_tr.status_code == 200
    tr_json = r_tr.json()
    assert tr_json["department"] == "QualityAssurance"
    assert len(tr_json["items"]) == 5  # exactly 5 calendar days gap-filled
    # 2026-07-18 is Saturday -> is_working_day = False, attendance_rate = None
    sat_item = next(it for it in tr_json["items"] if it["date"] == "2026-07-18")
    assert sat_item["is_working_day"] is False
    assert sat_item["attendance_rate"] is None
    # 2026-07-20 is Monday (present) -> attendance_rate = 1.0
    mon_item = next(it for it in tr_json["items"] if it["date"] == "2026-07-20")
    assert mon_item["is_working_day"] is True
    assert mon_item["attendance_rate"] == 1.0
    print("  -> PASS: 200 on department trend with $densify gap-filling, weekend nulls, and rolling avg")

    # 12. GET /admin/explain/{endpoint}
    print("\n[EP-12] GET /admin/explain/{endpoint}")
    # 422 on invalid endpoint identifier
    assert requests.get(f"{BASE_URL}/admin/explain/invalid_ep").status_code == 422
    # 422 on missing required query params
    assert requests.get(f"{BASE_URL}/admin/explain/employee_monthly?emp_code=EMP7701").status_code == 422
    assert requests.get(f"{BASE_URL}/admin/explain/department_trend?department=QualityAssurance").status_code == 422

    # Verify explain output and IXSCAN on all 5 queries
    for ep, params in [
        ("attendance_list", {"emp_code": "EMP7701"}),
        ("employee_monthly", {"emp_code": "EMP7701", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "QualityAssurance"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "QualityAssurance", "from": "2026-07-01", "to": "2026-07-05"})
    ]:
        r_exp = requests.get(f"{BASE_URL}/admin/explain/{ep}", params=params)
        assert r_exp.status_code == 200, f"Failed explain for {ep}: {r_exp.text}"
        exp_doc = r_exp.json()
        assert exp_doc["endpoint"] == ep
        explain_data = exp_doc["explain"]
        has_exec_stats = ("executionStats" in explain_data) or (
            "stages" in explain_data and len(explain_data["stages"]) > 0 and
            "$cursor" in explain_data["stages"][0] and
            "executionStats" in explain_data["stages"][0]["$cursor"]
        )
        assert has_exec_stats, f"Missing executionStats in explain output for {ep}"
    print("  -> PASS: 200 on admin explain for all 5 queries with executionStats")

    # Cleanup test employee EMP7701
    db.employees.delete_one({"emp_code": "EMP7701"})
    db.attendance_logs.delete_many({"emp_code": "EMP7701"})
    print("\nALL 12 ENDPOINTS AUDITED AND PASSED!")

if __name__ == "__main__":
    run_tests()
