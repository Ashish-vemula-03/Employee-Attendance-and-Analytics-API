# DECISIONS.md (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
   I created unique `emp_code` on employees and `(emp_code, date)` on attendance to enforce data integrity. For query performance, I added `(department, joined_on)` and `(date, emp_code)` to ensure index scans for listings and rankings. I rejected an index on `status` alone due to low selectivity.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
   Both requests calculate the date and run `insert_one()`. The database-level unique index on `(emp_code, date)` commits only one insert. The loser throws a `DuplicateKeyError`, which my code catches and turns into a 409 Conflict.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
   I used `$rank` in `$setWindowFields`, which shares ranks for ties and skips numbers (1, 2, 2, 4). Filtering by `rank <= limit` returns both tied employees at the cutoff, since they share the same rank.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
   The pipeline starts from `employees` (filtering `joined_on <= month_end`) and does a `$lookup` into attendance with `preserveNullAndEmptyArrays: true`. This keeps employees with zero logs in the stream so headcount counts them correctly.

5. **One thing you would change** if this had to serve 100x the data.
   I would introduce pre-aggregated summary collections updated via change streams instead of querying raw logs on the fly, and shard the attendance collection by `(department, date)` to distribute write and query loads.
