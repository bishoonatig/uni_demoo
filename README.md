# Campus timetable generator

This project uses PostgreSQL to store the university data and Google OR-Tools to generate a timetable. It does not train a language model: the solver checks constraints so the result cannot assign the same instructor, room, or student group to overlapping sessions.

## Setup

1. Create a PostgreSQL database.
2. Run `02_timetable_schema.sql` against it. This is the schema for `university-erd.md`; `01_create_database.sql` contains a different, older set of tables and is not used by this app.
3. Install the Python packages with `python -m pip install -r requirements.txt`.
4. Copy `.env.example` to `.env` and replace the sample connection values with your PostgreSQL host, port, database name, username, and password. Keep `.env` private; it is excluded from Git.

5. Restart the app with `python -m uvicorn app:app --reload` and open `http://127.0.0.1:8000`.

## Entering data

Add departments and academic terms first, then instructors, courses, cohorts, rooms, the weekly schedule, and course offerings. Enter the day start/end once; the app creates six 100-minute periods with 20-minute breaks for every day. Friday periods are available only to instructors marked as seconded. Select one or more teaching types for each instructor. A course offering contains one activity multi-select; its sessions are derived automatically: one lecture per pair of groups, one section per group, and one lab per group when selected. Adding a cohort needs its total student count; the app creates groups of at most 20 students and labels them `a`, `b`, `c`, and onward. Existing cohorts have a `Generate missing groups` action that adds only the students not yet assigned to groups. Set room types to `LECTURE`, `SECTION`, or `LAB`. The day number uses Monday = 1 through Sunday = 7.

Sessions are assigned only to instructors enabled for their activity; Friday is considered only when that instructor is marked as seconded.

Generating a term replaces its existing rows in `timetable_sessions` and `session_groups`. Make sure the input records and time slots are complete before generating.