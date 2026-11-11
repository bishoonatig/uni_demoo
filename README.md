# Campus timetable generator

This project uses PostgreSQL to store the university data and Google OR-Tools CP-SAT to generate a timetable. The solver prevents instructor, room, and student-group conflicts, caps each instructor and group at four sessions per day, requires at least three different courses on each day a student group attends, schedules those sessions without gaps, and gives each group at least two days off per week. Groups with the same group number in the same academic term are treated as the same students across courses. The solver stops after ten seconds if it cannot find a valid timetable.

## Setup

1. Create a PostgreSQL database.
2. Run `02_timetable_schema.sql` against it. This is the schema for `university-erd.md`; `01_create_database.sql` contains a different, older set of tables and is not used by this app.
   If the database already exists, run the schema script again to add the latest columns. Saved schedules from older rule versions are hidden until regenerated.
3. Install the Python packages with `python -m pip install -r requirements.txt`.
4. Copy `.env.example` to `.env` and replace the sample connection values with your PostgreSQL host, port, database name, username, and password. Keep `.env` private; it is excluded from Git.

5. Restart the app with `python -m uvicorn app:app --reload` and open `http://127.0.0.1:8000`.

## Entering data

Add departments and academic terms first, then instructors, courses, cohorts, rooms, the weekly schedule, and course offerings. Instructor selection on an offering is optional; unassigned offerings are shown as `Not assigned` and can be edited later. Their sessions are generated without an instructor and are not available on Fridays until an instructor is assigned. Enter the day start/end once; the app creates six 100-minute periods with 20-minute breaks for every day. Friday periods are available only to instructors marked as seconded. Select one or more teaching types for each instructor. A course offering contains one activity multi-select; its sessions are derived automatically: one lecture per pair of groups, one section per group, and one lab per group when selected. Select `Lecture is online` to schedule lectures without a room; sections and labs remain in-person. Adding a cohort needs its total student count; the app creates groups of at most 20 students and labels them `a`, `b`, `c`, and onward. Existing cohorts have a `Generate missing groups` action that adds only the students not yet assigned to groups. Set room types to `LECTURE`, `SECTION`, or `LAB`. The day number uses Monday = 1 through Sunday = 7.

Sessions are assigned only to instructors enabled for their activity; Friday is considered only when that instructor is marked as seconded. In-person lectures taught by seconded instructors are marked as every other week in the timetable without taking a second consecutive period. The timetable shows each instructor's name, including `Not assigned` for unassigned offerings.

Generating a term replaces its existing rows in `timetable_sessions` and `session_groups`. Make sure the input records and time slots are complete before generating.