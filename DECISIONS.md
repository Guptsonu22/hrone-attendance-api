# DECISIONS.md

1. **Indexes.**
   - `employees: { emp_code: 1 }` (unique): Enforces immutable employee uniqueness and instant lookups.
   - `employees: { department: 1, joined_on: 1 }`: Covers department filtering and eligibility date range checks.
   - `attendance_logs: { emp_code: 1, date: 1 }` (unique natural key): Prevents duplicate punch-ins for the same day.
   - `attendance_logs: { date: -1, emp_code: 1 }`: Powers sorted pagination for `/attendance` without in-memory sorting.
   - `attendance_logs: { emp_code: 1, date: -1 }`: Speeds up range scans for employee monthly summaries.
   - `attendance_logs: { emp_code: 1, punch_in: -1 }`: Fast-paths open record matching on punch-out across shifts.
   - `attendance_logs: { date: 1, late_minutes: 1 }`: Indexes date filtering for leaderboard queries.
   - *Rejected Index:* A standalone index on `{ status: 1 }`. Status has low cardinality (5 values), making an index scan less selective and slower than compound date/employee scans.

2. **Punch-in race.**
   Both requests compute the attendance date in IST and issue an atomic `insert_one`. The database-level unique compound index on `(emp_code, date)` guarantees MongoDB accepts the first insert (returning 201) and rejects the second with a `DuplicateKeyError` (code 11000). The app catches this error and returns 409 Conflict. There is no check-then-insert race window.

3. **Ties.**
   The pipeline ranks via `$setWindowFields` with standard competition ranking (`$rank: {}`). Tied employees share the same rank (e.g., 1, 2, 2, 4). The `limit` filter (`rank <= limit`) applies *after* ranking, so both employees tied at rank 2 are retained even if results exceed the limit count, sorted secondarily by `emp_code` ascending.

4. **Headcount.**
   The aggregation begins on `employees` (filtering `joined_on <= month_end`) and performs a `$lookup` into `attendance_logs`. Employees without logs receive an empty `logs: []` array; their attendance metrics sum to 0, but the `$group` stage still counts them toward department headcount.

5. **One change for 100x the data.**
   Implement daily pre-aggregated attendance rollups using MongoDB Change Streams or scheduled materialized views (`$merge`). Computing trend lines and monthly summaries from pre-aggregated daily tallies replaces expensive cross-collection joins with instant point queries.
