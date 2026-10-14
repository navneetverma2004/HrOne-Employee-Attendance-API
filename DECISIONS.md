# DECISIONS.md

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

I created a unique index on `employees.emp_code` for identity enforcement, plus indexes on `employees.department`, `employees.joined_on`, and compound `(department, joined_on)` to support department-level headcount filtering without COLLSCAN. On `attendance_logs`, I created a unique compound index on `(emp_code, date)` to enforce one record per day, `(date: -1, emp_code: 1)` to back paginated attendance listing, `(date: 1, late_minutes: 1, emp_code: 1)` to accelerate late leaderboard matching, and `(emp_code: 1, punch_in: -1)` for fast punch-out lookups. I considered and rejected a standalone index on `punch_out` because of low query selectivity and unnecessary index maintenance overhead.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

Both requests may pass the initial application check simultaneously. When both invoke `insert_one()`, the unique compound index on `(emp_code, date)` serializes them atomically in WiredTiger. Exactly one insert succeeds and returns HTTP 201. The competing insert raises a `DuplicateKeyError`, which the endpoint catches and returns as HTTP 409 Conflict without unhandled exceptions.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

The aggregation pipeline computes standard competition ranking using `$setWindowFields` with `$rank` (ties share a rank and subsequent ranks are skipped, e.g. 1, 2, 2, 4). The cutoff filter `{"rank": {"$lte": limit}}` runs after ranking, so all employees tied at or below the cutoff rank are returned, even if total items exceed `limit`. Tied rows are ordered deterministically by `emp_code` ascending.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?

The pipeline originates on the `employees` collection matching `{"joined_on": {"$lte": month_end}}` and joins attendance logs using `$lookup` with `preserveNullAndEmptyArrays: True`. Employees with zero logs in the month are preserved in the stream, contributing 1 to the department's `headcount` and 0 to attendance sums.

5. **One thing you would change** if this had to serve 100x the data.

At 100x scale (~10M+ attendance logs), I would shard `attendance_logs` using a hashed compound shard key on `(emp_code, date)` and implement incremental pre-aggregated daily/monthly rollup documents via MongoDB Change Streams or scheduled triggers, enabling O(1) analytics queries without runtime cross-collection lookups.
