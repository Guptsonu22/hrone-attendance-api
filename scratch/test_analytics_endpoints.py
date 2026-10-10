import os
import sys
sys.path.insert(0, ".")
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from fastapi.testclient import TestClient
from pymongo import MongoClient
import os

from app.main import app, db

client = TestClient(app)

def test_analytics_suite():
    print("\n--- RUNNING COMPREHENSIVE ANALYTICS TEST SUITE ---")
    
    # 1. Reseed initial sample data
    import sample_seed
    print("Seed reloaded.")

    # =========================================================================
    # Test 1: GET /analytics/departments/summary
    # =========================================================================
    print("\n[Test 1] Department Summary:")
    
    # 1a. Query for 2026-07
    r = client.get("/analytics/departments/summary?month=2026-07")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) > 0
    print(f"Summary items count: {len(data['items'])}")
    for item in data["items"]:
        print(f"  Dept: {item['department']}, HC: {item['headcount']}, Present: {item['present_days']}, "
              f"AvgWH: {item['avg_work_hours']}, Late: {item['late_count']} ({item['total_late_minutes']}m), "
              f"Leave: {item['leave_count']}, OnDuty: {item['on_duty_count']}")
        assert item["headcount"] > 0
        assert isinstance(item["present_days"], (int, float))
        assert item["avg_work_hours"] is None or isinstance(item["avg_work_hours"], (int, float))

    # 1b. Specific department filter
    r_eng = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert r_eng.status_code == 200
    d_eng = r_eng.json()
    assert len(d_eng["items"]) == 1
    assert d_eng["items"][0]["department"] == "Engineering"

    # 1c. Joined after reporting period should not be counted in headcount
    # Insert a future employee joining in August 2026
    db.employees.delete_one({"emp_code": "EMP9999"})
    db.employees.insert_one({
        "emp_code": "EMP9999",
        "name": "Future Joiner",
        "email": "future@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
        "created_at": datetime.now(timezone.utc)
    })
    r_eng_after = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert r_eng_after.status_code == 200
    # Headcount should remain the same (2), future joiner excluded!
    assert r_eng_after.json()["items"][0]["headcount"] == d_eng["items"][0]["headcount"]

    # 1d. Employee with zero logs still counted in headcount
    # EMP9998 joined in 2026-07-01 with 0 logs
    db.employees.delete_one({"emp_code": "EMP9998"})
    db.employees.insert_one({
        "emp_code": "EMP9998",
        "name": "Zero Logs Guy",
        "email": "zerologs@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
        "created_at": datetime.now(timezone.utc)
    })
    r_eng_zero = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert r_eng_zero.status_code == 200
    # Headcount should increase by 1
    assert r_eng_zero.json()["items"][0]["headcount"] == d_eng["items"][0]["headcount"] + 1

    # Cleanup temp employees
    db.employees.delete_many({"emp_code": {"$in": ["EMP9999", "EMP9998"]}})

    # 1e. Validation error on invalid month
    r_inv_m = client.get("/analytics/departments/summary?month=2026-13")
    assert r_inv_m.status_code == 422
    print("[PASS] Department summary tests passed.")

    # =========================================================================
    # Test 2: GET /analytics/leaderboard/late
    # =========================================================================
    print("\n[Test 2] Late Leaderboard:")

    # Setup controlled dataset for ranking & ties
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^TESTL"}})
    db.employees.delete_many({"emp_code": {"$regex": "^TESTL"}})

    # Create 5 test employees in "TestDept"
    for i in range(1, 6):
        db.employees.insert_one({
            "emp_code": f"TESTL00{i}",
            "name": f"Tester {i}",
            "email": f"test{i}@example.com",
            "department": "TestDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc)
        })

    # Late minutes setup:
    # TESTL001: 60 mins late
    # TESTL002: 40 mins late (Tie with TESTL003)
    # TESTL003: 40 mins late (Tie with TESTL002)
    # TESTL004: 20 mins late
    # TESTL005: 0 mins late (should be excluded)
    test_lates = [
        ("TESTL001", 60),
        ("TESTL002", 40),
        ("TESTL003", 40),
        ("TESTL004", 20),
    ]
    for emp_c, lm in test_lates:
        db.attendance_logs.insert_one({
            "emp_code": emp_c,
            "date": "2026-07-02",
            "status": "PRESENT",
            "punch_in": 1783053000000,
            "punch_out": 1783085400000,
            "work_hours": 9.0,
            "late_minutes": lm,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })

    # Query leaderboard with limit=2 for department TestDept
    r_lb = client.get("/analytics/leaderboard/late?month=2026-07&department=TestDept&limit=2")
    assert r_lb.status_code == 200, r_lb.text
    lb_data = r_lb.json()
    assert lb_data["month"] == "2026-07"
    items = lb_data["items"]
    print("Leaderboard items (limit=2 with tie at cutoff):")
    for it in items:
        print(f"  Rank: {it['rank']}, Emp: {it['emp_code']}, Late: {it['total_late_minutes']}, Count: {it['late_count']}")

    # Standard competition ranking:
    # TESTL001 -> Rank 1
    # TESTL002 -> Rank 2
    # TESTL003 -> Rank 2
    # With limit=2, rank <= 2: ALL 3 must be returned! (limit retained ties at cutoff)
    assert len(items) == 3, f"Expected 3 items due to tie retaining at rank 2, got {len(items)}"
    assert items[0]["emp_code"] == "TESTL001" and items[0]["rank"] == 1
    assert items[1]["rank"] == 2
    assert items[2]["rank"] == 2
    # Verify secondary sort by emp_code ascending for ties
    assert items[1]["emp_code"] == "TESTL002"
    assert items[2]["emp_code"] == "TESTL003"

    # Query with limit=10:
    r_lb_all = client.get("/analytics/leaderboard/late?month=2026-07&department=TestDept&limit=10")
    items_all = r_lb_all.json()["items"]
    # Ranks: 1, 2, 2, 4 (Standard competition ranking: next rank after tie is 4, NOT 3!)
    assert len(items_all) == 4  # TESTL005 with 0 late minutes is excluded
    assert items_all[3]["emp_code"] == "TESTL004"
    assert items_all[3]["rank"] == 4, f"Expected standard competition rank 4, got {items_all[3]['rank']}"

    # Validation: limit < 1 or limit > 50 -> 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422

    # Cleanup test leaderboard data
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^TESTL"}})
    db.employees.delete_many({"emp_code": {"$regex": "^TESTL"}})
    print("[PASS] Late leaderboard tests passed.")

    # =========================================================================
    # Test 3: GET /analytics/departments/{department}/trend
    # =========================================================================
    print("\n[Test 3] Department Trend:")

    # 3a. Validation: 404 for unknown department
    r_404 = client.get("/analytics/departments/NoSuchDept/trend?from=2026-07-01&to=2026-07-05")
    assert r_404.status_code == 404

    # 3b. Validation: to < from -> 422
    r_to_before_from = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05")
    assert r_to_before_from.status_code == 422

    # 3c. Validation: > 92 days -> 422
    r_over_92 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")  # ~121 days
    assert r_over_92.status_code == 422

    # 3d. Gap filling and date count check: 2026-07-01 to 2026-07-15 (15 calendar days)
    r_trend = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-15")
    assert r_trend.status_code == 200, r_trend.text
    trend_data = r_trend.json()
    assert trend_data["department"] == "Engineering"
    items = trend_data["items"]
    assert len(items) == 15, f"Expected 15 days, got {len(items)}"

    print("Trend items for Engineering (2026-07-01 to 2026-07-15):")
    for it in items:
        print(f"  {it['date']}: WorkDay={it['is_working_day']}, HC={it['headcount']}, "
              f"Pres={it['present_count']}, Late={it['late_count']}, "
              f"Rate={it['attendance_rate']}, MA7={it['moving_avg_7d']}")

    # Check joining during the range:
    # In sample data, EMP0001 joined 2026-01-05, EMP0002 joined 2026-07-13.
    # Therefore, headcount on 2026-07-12 is 1, and headcount on 2026-07-13 is 2!
    day_12 = next(x for x in items if x["date"] == "2026-07-12")
    day_13 = next(x for x in items if x["date"] == "2026-07-13")
    assert day_12["headcount"] == 1, f"Expected HC=1 on 2026-07-12, got {day_12['headcount']}"
    assert day_13["headcount"] == 2, f"Expected HC=2 on 2026-07-13, got {day_13['headcount']}"

    # Check weekends: attendance_rate must be null
    # 2026-07-04 is Saturday, 2026-07-05 is Sunday
    sat = next(x for x in items if x["date"] == "2026-07-04")
    sun = next(x for x in items if x["date"] == "2026-07-05")
    assert sat["is_working_day"] is False
    assert sat["attendance_rate"] is None
    assert sun["is_working_day"] is False
    assert sun["attendance_rate"] is None

    # Check empty days / days with no attendance records:
    # A working day with no records has rate 0.0 (per contract)
    # E.g. 2026-07-09 is Thursday (working day) with no logs for Engineering
    day_09 = next(x for x in items if x["date"] == "2026-07-09")
    assert day_09["is_working_day"] is True
    assert day_09["present_count"] == 0.0
    assert day_09["attendance_rate"] == 0.0

    # Check zero headcount behavior:
    # Create department "NewDept" where only employee joins on 2026-10-01
    db.employees.delete_many({"department": "NewDept"})
    db.employees.insert_one({
        "emp_code": "NEW0001",
        "name": "Late Joiner",
        "email": "late@example.com",
        "department": "NewDept",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-10-01",
        "created_at": datetime.now(timezone.utc)
    })
    r_new_dept = client.get("/analytics/departments/NewDept/trend?from=2026-09-01&to=2026-09-05")
    assert r_new_dept.status_code == 200
    new_items = r_new_dept.json()["items"]
    assert len(new_items) == 5
    for nit in new_items:
        # Headcount is 0 because joined_on is in October!
        assert nit["headcount"] == 0
        # Per contract: only on working days with headcount > 0, otherwise null!
        assert nit["attendance_rate"] is None
        assert nit["moving_avg_7d"] is None

    # Cleanup NewDept
    db.employees.delete_many({"department": "NewDept"})

    print("[PASS] Department trend tests passed.")
    print("\nALL ANALYTICS TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_analytics_suite()
