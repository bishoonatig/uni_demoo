# Campus timetable generator

This project uses PostgreSQL to store the university data and Google OR-Tools to generate a timetable. It does not train a language model: the solver checks constraints so the result cannot assign the same instructor, room, or student group to overlapping sessions.

## Setup

1. Create a PostgreSQL database.
2. Run `02_timetable_schema.sql` against it. This is the schema for `university-erd.md`; `01_create_database.sql` contains a different, older set of tables and is not used by this app.
   When updating an existing database after a schema change, run `02_timetable_schema.sql` against that same database again before using the updated app. The schema migration removes the obsolete cohort table, consolidates duplicate groups for the same course and term, and clears saved timetables because group links may change. The app also adds the `instructors.part_time` column automatically when it first loads instructor records.
3. Install the Python packages with `python -m pip install -r requirements.txt`.
4. Copy `.env.example` to `.env` and replace the sample connection values with your PostgreSQL host, port, database name, username, and password. Keep `.env` private; it is excluded from Git.

5. Restart the app with `python -m uvicorn app:app --reload` and open `http://127.0.0.1:8000`.

## Sharing database data

GitHub stores the project files and database schema, not the rows entered while using the app. To see the same data, all collaborators must connect to the same PostgreSQL database: create a hosted PostgreSQL database, run `02_timetable_schema.sql` against it, and set the same database URL in each person's private `.env` file. Never commit `.env` or database backups to GitHub. Moving data from an existing database to the shared one requires exporting and importing that database separately; simply uploading the project does not transfer it.

## Entering data

Add departments, terms, instructors, and each course once; course records contain the code, name, and department only. When creating a course offering, choose the course and enter its student count for that term beside it. Reuse the same course record in other terms and enter the enrollment for each term's offering. If a course has multiple offerings in the same term, they must use the same student count and share the course's groups. Groups contain at most 20 students and are labeled `a`, `b`, `c`, and onward. Sessions are derived from those groups: one lecture per pair of groups, one section per group, and one lab per group when selected. Course offerings have a `Generate missing groups` action for adding students not yet assigned to a group. Generate the timetable once for all terms so the solver prevents instructor, room, and student-group conflicts across the complete schedule. Use the term selector to switch the group timetable without regenerating. Instructor and room views show saved sessions across all terms, with each session labeled by term. Mark instructors as part-time when applicable; this is recorded separately and does not currently change scheduling rules. Enter the day start/end once; the app creates six 100-minute periods with 20-minute breaks for every day. Friday periods are available only to instructors marked as seconded. Set room types to `LECTURE`, `SECTION`, or `LAB`. The day number uses Monday = 1 through Sunday = 7.

Friday is considered only when the assigned instructor is marked as seconded.

Course offerings can be edited or deleted from their row actions. Departments, academic terms, instructors, courses, and rooms can also be deleted from their rows. Other records cannot be deleted while they are still referenced; remove or update those dependent records first. Changing an offering clears the saved timetable for the affected term; generate all terms again after finishing your changes. Generating replaces all rows in `timetable_sessions` and `session_groups`. When migrating existing data, the schema merges duplicate course records with the same code, preserves their offerings and term-specific student counts, rebuilds groups, and clears saved timetable sessions; generate all terms after migration. Make sure the input records and time slots are complete before generating.