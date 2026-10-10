import os
import concurrent.futures
from pymongo import MongoClient
import httpx as requests

BASE_URL = "http://127.0.0.1:8000"

def test_simultaneous_requests():
    print("=====================================================================")
    print("       SIMULTANEOUS CONCURRENCY & RACE-CONDITION TEST SUITE          ")
    print("=====================================================================")
    
    mc = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    db = mc[os.getenv("MONGO_DB", "attendance_db")]
    
    # Setup: Clean test employee
    test_code = "EMP9901"
    db.employees.delete_one({"emp_code": test_code})
    db.attendance_logs.delete_many({"emp_code": test_code})
    
    # -------------------------------------------------------------------------
    # 1. Simultaneous Employee Creation Race Condition
    # -------------------------------------------------------------------------
    emp_payload = {
        "emp_code": test_code,
        "name": "Race Condition Tester",
        "email": "race.test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    
    num_concurrent = 10
    print(f"\n[1] Firing {num_concurrent} simultaneous POST /employees requests for {test_code}...")
    
    def create_emp():
        with requests.Client(base_url=BASE_URL) as client:
            return client.post("/employees", json=emp_payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(create_emp) for _ in range(num_concurrent)]
        statuses = [f.result() for f in futures]
    
    count_201 = statuses.count(201)
    count_409 = statuses.count(409)
    print(f"  Results: 201 Created = {count_201}, 409 Conflict = {count_409}")
    assert count_201 == 1, f"Expected exactly one 201, got {count_201}"
    assert count_409 == num_concurrent - 1, f"Expected {num_concurrent - 1} 409s, got {count_409}"
    print("  -> PASS: Unique index prevented race condition; exactly one insert succeeded.")
    
    # -------------------------------------------------------------------------
    # 2. Simultaneous Punch-In Race Condition on Same Date
    # -------------------------------------------------------------------------
    punch_payload = {
        "emp_code": test_code,
        "punched_at": 1783053000000 # 2026-07-06 09:30:00 IST
    }
    
    print(f"\n[2] Firing {num_concurrent} simultaneous POST /attendance/punch-in requests...")
    def punch_in():
        with requests.Client(base_url=BASE_URL) as client:
            return client.post("/attendance/punch-in", json=punch_payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(punch_in) for _ in range(num_concurrent)]
        statuses = [f.result() for f in futures]
    
    pi_201 = statuses.count(201)
    pi_409 = statuses.count(409)
    print(f"  Results: 201 Created = {pi_201}, 409 Conflict = {pi_409}")
    assert pi_201 == 1, f"Expected exactly one 201 punch-in, got {pi_201}"
    assert pi_409 == num_concurrent - 1, f"Expected {num_concurrent - 1} 409s, got {pi_409}"
    print("  -> PASS: Compound unique index (emp_code, date) prevented race condition; exactly one punch-in succeeded.")
    
    # Cleanup
    db.employees.delete_one({"emp_code": test_code})
    db.attendance_logs.delete_many({"emp_code": test_code})
    print("\nSimultaneous concurrency verification completed and test data cleaned up.")

if __name__ == "__main__":
    test_simultaneous_requests()
