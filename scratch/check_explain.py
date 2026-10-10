import urllib.request
import json

BASE = "http://127.0.0.1:8000"

def find_stages(obj, found=None):
    if found is None:
        found = []
    if isinstance(obj, dict):
        if "stage" in obj:
            found.append({
                "stage": obj["stage"],
                "indexName": obj.get("indexName"),
                "filter": obj.get("filter")
            })
        for v in obj.values():
            find_stages(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_stages(item, found)
    return found

for ep, params in [
    ("attendance_list", "emp_code=EMP0001"),
    ("employee_monthly", "emp_code=EMP0001&month=2026-07"),
    ("department_summary", "month=2026-07&department=Engineering"),
    ("late_leaderboard", "month=2026-07"),
    ("department_trend", "department=Engineering&from=2026-07-01&to=2026-07-05")
]:
    url = f"{BASE}/admin/explain/{ep}?{params}"
    req = urllib.request.urlopen(url)
    data = json.loads(req.read())
    exp = data["explain"]
    stages = find_stages(exp)
    print("====================================")
    print("Endpoint:", ep)
    stage_names = [s["stage"] for s in stages]
    print("All stages in plan:", stage_names)
    ixscans = [s for s in stages if s["stage"] == "IXSCAN"]
    collscans = [s for s in stages if s["stage"] == "COLLSCAN"]
    print("  IXSCANs:", ixscans)
    print("  COLLSCANs:", collscans)
