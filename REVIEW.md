# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |

Also note anything you looked at and decided was **not** a defect, and why.

|---|---|---|---|---|

| 1 | `compute_late_minutes()` | Calculated shift start on the punch-in date; overnight shift punch-in after midnight was compared against the current day's shift start, resulting in negative duration or incorrect lateness. | Punch-in at 00:15 IST for a 22:00–06:00 shift produced negative elapsed time instead of counting lateness against yesterday's 22:00 start. | Resolved shift start datetime using the IST attendance calendar date (yesterday if punch-in < shift_end) and applied strict > 10 min grace period. |

| 2 | `compute_work_hours()` | Used Python's built-in `round()`, which implements round-half-to-even (banker's rounding) rather than round-half-up. | Elapsed duration of 16,110 seconds (4.475 hours) rounded to 4.47 instead of the required 4.48. | Replaced with `Decimal` division and `quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`. |

| 3 | `compute_overtime()` | Counted positive overtime even when under the 30-minute threshold and constructed shift end on punch date for overnight shifts. | Punch-out 15 minutes after shift end returned 15 instead of 0. | Enforced >= 30 whole minutes threshold (returning 0 otherwise) and resolved shift end on the following day for overnight shifts. |

| 4 | `EmployeeIn` model | Lacked domain validation for `emp_code` regex (`^EMP\d{4,6}$`), email format, string lengths, and permitted identical shift start/end times. | Passing `"emp_code": "INVALID"` or `"shift_start": "09:30", "shift_end": "09:30"` passed validation instead of returning HTTP 422. | Added field regex and length constraints in Pydantic schema and a model validator ensuring `shift_start != shift_end`. |

| 5 | `health()` | Returned `{"status": "ok"}` without verifying database connectivity. | Stopping or disconnecting MongoDB still returned HTTP 200. | Added `db.command("ping")` and raised `HTTPException(503, "Database unavailable")` on failure. |

| 6 | `create_employee()` | Separate check-then-insert permitted duplicate codes under concurrency; returned message dictionary instead of `Employee` schema with epoch ms `created_at`. | Concurrent requests with same `emp_code` created duplicates; response was `{"message": "Employee created successfullly"}` violating OpenAPI contract. | Added unique index on `emp_code`, handled `DuplicateKeyError` as HTTP 409, and returned full `Employee` object with integer epoch millisecond `created_at`. |

| 7 | `list_employees()` | Calculated offset as `page * page_size` (skipping page 1), counted all documents ignoring department filter, returned `created_at` as ISO string/datetime, and lacked pagination bounds. | Page 1 skipped first page of results; filtering by department showed total count of entire collection. | Corrected offset to `(page - 1) * page_size`, applied query filter to `count_documents`, sorted by `emp_code` ascending, and formatted `created_at` as epoch ms. |

| 8 | `punch_in()` | Used check-then-insert without catching `DuplicateKeyError`; did not check if employee existed (server error); added non-contract `id` field; returned `punch_in` as Python datetime. | Non-existent `emp_code` threw 500; concurrent punch-ins threw unhandled 500; response contained `id` and datetime strings. | Added employee existence check (404), wrapped `insert_one` to catch `DuplicateKeyError` (409), removed `id`, and serialized `punch_in` to epoch ms. |

| 9 | `list_attendance()` | Sorted only by `date` descending (omitting `emp_code` ascending); injected non-contract `id`; returned datetimes instead of epoch ms; did not default missing fields on legacy records. | Result sorting was non-deterministic for same date; response exposed MongoDB `_id` as `id`; legacy records lacked default `half_day: False`. | Applied compound sort `[("date", -1), ("emp_code", 1)]`, serialized all instants to epoch ms, stripped `_id`, and normalized legacy records with defaults. |

| 10 | `punch_out()` | Returned HTTP 409 when no attendance record or punch-in existed (inverting 404/409); omitted 422 checks for `punched_at <= punch_in` or `> 24 hours`. | Employee with no records received 409 instead of 404; punching out with `punched_at == punch_in` was accepted. | Returned 404 if no record with `punch_in <= punched_at` exists; validated `punch_in < punched_at <= punch_in + 24h` (422); returned 409 only when record already punched out. |

| 11 | `regularize_attendance()` | Missing `reason` and `regularized_by` in request model caused crash; failed to clear punch times on `ABSENT`/`LEAVE`; wrong history schema (`changed_at` instead of `at`, `old`/`new` instead of `from`/`to`, omitted `by` and derived field changes). | Calling endpoint raised `AttributeError: body.reason`; setting `ABSENT` kept existing punch times; history schema violated contract. | Added required fields to model; enforced ABSENT/LEAVE rules; recorded all changed fields (including derived) with `{"from": ..., "to": ...}`; formatted history with `{at, by, reason, changes}`. |

| 12 | `explain_endpoint()` | Raised HTTP 501 for 4 out of 5 endpoints; plain dictionary command failed on MongoDB Atlas with syntax error (code 8000); raw BSON types caused serialization failure. | Calling `/admin/explain/department_summary` returned 501; calling `attendance_list` failed on Atlas or crashed FastAPI with Timestamp serialization error. | Implemented explain for all 5 endpoints using `SON` wire protocol, validated required query parameters (422), and converted BSON structures using `json_util`. |

---

## Items reviewed and determined not to be defects

1. **`doc = body.model_dump()` in `create_employee()`:** Correctly extracts validated attributes into a dictionary for database operations.
2. **Removing `_id` from document dictionaries:** Removing internal MongoDB `_id` is required because the API specification forbids exposing MongoDB internal identifiers.
3. **Default shift times (`09:30` and `18:30`):** These defaults match the business shift specifications in the assignment contract.
4. **`punch_out=None` and `work_hours=None` at punch-in:** When an employee punches in, the shift is ongoing, so punch-out and work hours must remain null until punch-out occurs.
5. **HTTP 201 for successful creation:** Employee creation and punch-in endpoints are defined in OpenAPI as returning HTTP 201 Created.
6. **String-based date and time fields in MongoDB:** Keeping `date`, `joined_on`, `shift_start`, and `shift_end` as strings is specifically required by `DATA_MODEL.md` to avoid timezone corruption on pure calendar dates and times of day.
