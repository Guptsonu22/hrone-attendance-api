import os
from datetime import datetime, date as date_cls, timedelta, timezone
from pymongo import MongoClient

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client["attendance_db"]

department = "Engineering"
# Date range where there are NO records for Engineering
from_str = "2026-09-01"
to_str = "2026-09-05"

from_d = date_cls.fromisoformat(from_str)
to_d = date_cls.fromisoformat(to_str)
dt_from = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
dt_to_bound = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

pipeline = [
    {
        "$match": {
            "date": {"$gte": from_str, "$lte": to_str}
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
]

res = list(db.nonexistent_coll.aggregate(pipeline))
print(f"Result count for empty collection: {len(res)}")
for r in res:
    print(r)
