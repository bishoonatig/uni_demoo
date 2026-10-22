import os
from datetime import date, datetime, time, timedelta
from typing import Any

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from psycopg import sql
from pydantic import BaseModel

from timetable_scheduler import (
    Room,
    ScheduleInfeasibleError,
    Session,
    TimeSlot,
    generate_schedule,
)


load_dotenv()

app = FastAPI(title="University Timetable Generator")
DATABASE_URL = os.getenv("DATABASE_URL")
MAX_GROUP_SIZE = 20
PERIODS_PER_DAY = 6
PERIOD_DURATION_MINUTES = 100
BREAK_DURATION_MINUTES = 20
FRIDAY = 5
TEACHING_DAYS = (1, 2, 3, 4, 5, 6, 7)

ENTITIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "departments": ("departments", "id", ("name",)),
    "academic_terms": (
        "academic_terms",
        "id",
        ("name", "start_date", "end_date"),
    ),
    "cohorts": (
        "cohorts",
        "id",
        ("department_id", "academic_term_id", "level", "total_students", "group_size"),
    ),
    "instructors": (
        "instructors",
        "id",
        (
            "name",
            "department_id",
            "teaching_types",
            "friday_available",
        ),
    ),
    "courses": ("courses", "id", ("code", "name", "department_id")),
    "rooms": ("rooms", "id", ("name", "capacity", "room_type")),
    "time_slots": (
        "time_slots",
        "id",
        ("day_of_week", "start_time", "end_time"),
    ),
    "student_groups": (
        "student_groups",
        "id",
        ("cohort_id", "group_number", "student_count"),
    ),
    "course_offerings": (
        "course_offerings",
        "id",
        (
            "course_id",
            "cohort_id",
            "academic_term_id",
            "instructor_id",
            "teaching_types",
        ),
    ),
}


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="Set DATABASE_URL to connect the app to PostgreSQL.",
        )
    try:
        return psycopg.connect(DATABASE_URL)
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Could not connect to PostgreSQL.") from error


def json_value(value: Any) -> Any:
    if isinstance(value, (date, time)):
        return value.isoformat()
    return value


def normalize_room_type(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"LECTURE", "SECTION", "LAB"}:
        raise HTTPException(
            status_code=422,
            detail="Room type must be Lecture, Section, or Lab.",
        )
    return normalized


TEACHING_CAPABILITY_FIELDS = (
    "can_teach_lecture",
    "can_teach_section",
    "can_teach_lab",
)
TEACHING_TYPE_FIELDS = {
    "lecture": "can_teach_lecture",
    "section": "can_teach_section",
    "lab": "can_teach_lab",
}


def normalize_teaching_types(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise HTTPException(
            status_code=422,
            detail="Choose one or more teaching types.",
        )
    normalized = list(dict.fromkeys(str(value).strip().lower() for value in values))
    if not normalized or set(normalized) - set(TEACHING_TYPE_FIELDS):
        raise HTTPException(
            status_code=422,
            detail="Select at least one teaching type: lecture, section, or lab.",
        )
    return normalized


def instructor_teaching_types(values: dict[str, Any]) -> list[str]:
    return [
        teaching_type
        for teaching_type, field in TEACHING_TYPE_FIELDS.items()
        if values.get(field, False)
    ]


def group_label(group_number: int) -> str:
    label = ""
    while group_number:
        group_number, remainder = divmod(group_number - 1, 26)
        label = chr(ord("a") + remainder) + label
    return label


def build_group_sizes(total_students: int) -> list[int]:
    return [
        min(MAX_GROUP_SIZE, total_students - start)
        for start in range(0, total_students, MAX_GROUP_SIZE)
    ]


def add_missing_groups(
    connection: psycopg.Connection, cohort_id: int
) -> dict[str, Any]:
    cohort = connection.execute(
        "SELECT total_students FROM cohorts WHERE id = %s FOR UPDATE",
        (cohort_id,),
    ).fetchone()
    if cohort is None:
        raise HTTPException(status_code=404, detail="Cohort not found.")

    existing_groups = connection.execute(
        "SELECT group_number, student_count FROM student_groups WHERE cohort_id = %s ORDER BY group_number",
        (cohort_id,),
    ).fetchall()
    existing_total = sum(student_count for _, student_count in existing_groups)
    total_students = cohort[0]
    if existing_total > total_students:
        raise HTTPException(
            status_code=409,
            detail="Existing groups contain more students than the cohort total.",
        )

    first_group_number = max(
        (group_number for group_number, _ in existing_groups), default=0
    ) + 1
    generated_groups = []
    for group_number, student_count in enumerate(
        build_group_sizes(total_students - existing_total),
        start=first_group_number,
    ):
        group_row = connection.execute(
            """
            INSERT INTO student_groups (cohort_id, group_number, student_count)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (cohort_id, group_number, student_count),
        ).fetchone()
        generated_groups.append(
            {
                "id": group_row[0],
                "group_number": group_number,
                "group_label": group_label(group_number),
                "student_count": student_count,
            }
        )

    return {
        "groups": generated_groups,
        "group_count": len(existing_groups) + len(generated_groups),
    }


def build_course_activity_specs(
    cohort_groups: list[tuple[int, int]], teaching_types: list[str]
) -> list[tuple[str, tuple[int, ...], int]]:
    group_pairs = [
        cohort_groups[index : index + 2]
        for index in range(0, len(cohort_groups), 2)
    ]
    specs = []
    if "lecture" in teaching_types:
        specs.extend(
            (
                "lecture",
                tuple(group_id for group_id, _ in pair),
                sum(student_count for _, student_count in pair),
            )
            for pair in group_pairs
        )
    if "section" in teaching_types:
        specs.extend(
            ("section", (group_id,), student_count)
            for group_id, student_count in cohort_groups
        )
    if "lab" in teaching_types:
        specs.extend(
            ("lab", (group_id,), student_count)
            for group_id, student_count in cohort_groups
        )
    return specs


def offering_teaching_types(offering: dict[str, Any]) -> list[str]:
    return [
        activity
        for activity, field in (
            ("lecture", "weekly_lectures"),
            ("section", "weekly_sections"),
            ("lab", "weekly_labs"),
        )
        if offering[field] > 0
    ]


def build_daily_periods(
    start_time_value: str | time, end_time_value: str | time
) -> list[tuple[time, time]]:
    start_time = (
        time.fromisoformat(start_time_value)
        if isinstance(start_time_value, str)
        else start_time_value
    )
    end_time = (
        time.fromisoformat(end_time_value)
        if isinstance(end_time_value, str)
        else end_time_value
    )
    day = date.today()
    cursor = datetime.combine(day, start_time)
    day_end = datetime.combine(day, end_time)
    required_minutes = (
        PERIODS_PER_DAY * PERIOD_DURATION_MINUTES
        + (PERIODS_PER_DAY - 1) * BREAK_DURATION_MINUTES
    )
    if (day_end - cursor) != timedelta(minutes=required_minutes):
        raise HTTPException(
            status_code=422,
            detail="The daily window must fit 6 periods of 100 minutes with 20-minute breaks (08:30–20:10).",
        )

    periods = []
    for period_number in range(PERIODS_PER_DAY):
        period_end = cursor + timedelta(minutes=PERIOD_DURATION_MINUTES)
        periods.append((cursor.time(), period_end.time()))
        cursor = period_end + timedelta(minutes=BREAK_DURATION_MINUTES)
    return periods


def replace_weekly_time_slots(values: dict[str, Any]) -> list[dict[str, Any]]:
    required_fields = {"start_time", "end_time"}
    if set(values) != required_fields:
        raise HTTPException(
            status_code=422,
            detail="Enter the start and end of the daily schedule.",
        )
    periods = build_daily_periods(values["start_time"], values["end_time"])
    expected = sorted(
        (day, start, end)
        for day in TEACHING_DAYS
        for start, end in periods
    )

    try:
        with connect() as connection:
            existing = connection.execute(
                "SELECT id, day_of_week, start_time, end_time FROM time_slots FOR UPDATE"
            ).fetchall()
            existing_periods = sorted(
                (day, start, end) for _, day, start, end in existing
            )
            if existing_periods == expected:
                return [
                    {
                        "id": slot_id,
                        "day_of_week": day,
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                    }
                    for slot_id, day, start, end in existing
                ]

            existing_ids = [slot_id for slot_id, _, _, _ in existing]
            if existing_ids:
                is_used = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM timetable_sessions WHERE time_slot_id = ANY(%s)
                        UNION ALL
                        SELECT 1 FROM session_groups WHERE time_slot_id = ANY(%s)
                    )
                    """,
                    (existing_ids, existing_ids),
                ).fetchone()[0]
                if is_used:
                    raise HTTPException(
                        status_code=409,
                        detail="These periods are already used by a saved timetable.",
                    )
                connection.execute("DELETE FROM time_slots")

            created = []
            for day in TEACHING_DAYS:
                for start, end in periods:
                    slot_id = connection.execute(
                        """
                        INSERT INTO time_slots (day_of_week, start_time, end_time)
                        VALUES (%s, %s, %s)
                        RETURNING id
                        """,
                        (day, start, end),
                    ).fetchone()[0]
                    created.append(
                        {
                            "id": slot_id,
                            "day_of_week": day,
                            "start_time": start.isoformat(),
                            "end_time": end.isoformat(),
                        }
                    )
            return created
    except psycopg.Error as error:
        raise HTTPException(
            status_code=400,
            detail="Could not save the weekly periods; check the daily time window.",
        ) from error


def ensure_schedule_load_fits_slots(
    sessions: list[Session], time_slots: list[TimeSlot]
) -> None:
    resource_sessions: dict[tuple[str, int], list[Session]] = {}
    for session in sessions:
        for group_id in session.group_ids:
            resource_sessions.setdefault(("student group", group_id), []).append(session)
        if session.instructor_id is not None:
            resource_sessions.setdefault(("instructor", session.instructor_id), []).append(session)

    weekday_slot_count = sum(slot.day_of_week != FRIDAY for slot in time_slots)
    friday_slot_count = sum(slot.day_of_week == FRIDAY for slot in time_slots)
    for (resource_type, _), sessions_for_resource in resource_sessions.items():
        friday_eligible_count = sum(
            session.friday_available for session in sessions_for_resource
        )
        eligible_slot_count = weekday_slot_count + min(
            friday_slot_count, friday_eligible_count
        )
        if len(sessions_for_resource) > eligible_slot_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A {resource_type} requires {len(sessions_for_resource)} sessions per week, "
                    f"but has only {eligible_slot_count} eligible periods. Add teaching days "
                    "or reduce the weekly session counts."
                ),
            )


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/api/health")
def health():
    with connect() as connection:
        connection.execute("SELECT 1")
    return {"database": "connected"}


@app.get("/api/data/{entity}")
def list_entity(entity: str):
    config = ENTITIES.get(entity)
    if config is None:
        raise HTTPException(status_code=404, detail="Unknown data type.")
    table, primary_key, _ = config
    with connect() as connection:
        rows = connection.execute(
            sql.SQL("SELECT * FROM {} ORDER BY {}").format(
                sql.Identifier(table), sql.Identifier(primary_key)
            )
        ).fetchall()
        columns = [column.name for column in connection.execute(
            sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(table))
        ).description]
    records = [
        {key: json_value(value) for key, value in zip(columns, row)}
        for row in rows
    ]
    if entity == "instructors":
        for record in records:
            record["teaching_types"] = instructor_teaching_types(record)
    if entity == "student_groups":
        for record in records:
            record["group_label"] = group_label(record["group_number"])
    if entity == "course_offerings":
        for record in records:
            record["teaching_types"] = offering_teaching_types(record)
    return records


@app.post("/api/data/{entity}")
def create_entity(entity: str, values: dict[str, Any]):
    if entity == "student_groups":
        raise HTTPException(
            status_code=405,
            detail="Student groups are generated automatically from cohort totals.",
        )
    config = ENTITIES.get(entity)
    if config is None:
        raise HTTPException(status_code=404, detail="Unknown data type.")
    table, _, allowed_fields = config
    if not values or set(values) - set(allowed_fields):
        raise HTTPException(status_code=422, detail="Invalid or empty fields.")
    if entity == "time_slots":
        return replace_weekly_time_slots(values)
    if entity == "course_offerings":
        return create_course_offering(values)
    values = values.copy()
    if entity == "instructors":
        selected_types = normalize_teaching_types(values.pop("teaching_types", None))
        for teaching_type, field in TEACHING_TYPE_FIELDS.items():
            values[field] = teaching_type in selected_types
    if entity == "cohorts":
        values["group_size"] = MAX_GROUP_SIZE
    if entity == "rooms" and "room_type" in values:
        values["room_type"] = normalize_room_type(values["room_type"])
    fields = tuple(values)
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, fields)),
        sql.SQL(", ").join(sql.Placeholder() for _ in fields),
    )
    try:
        with connect() as connection:
            cursor = connection.execute(statement, tuple(values[field] for field in fields))
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
            generated_groups = None
            if entity == "cohorts":
                cohort_id = row[columns.index("id")]
                generated_groups = add_missing_groups(connection, cohort_id)
    except psycopg.Error as error:
        raise HTTPException(status_code=400, detail="Could not save the record; check required fields and references.") from error
    record = {key: json_value(value) for key, value in zip(columns, row)}
    if generated_groups is not None:
        record.update(generated_groups)
    return record


def create_course_offering(values: dict[str, Any]) -> dict[str, Any]:
    required_fields = {
        "course_id",
        "cohort_id",
        "academic_term_id",
        "instructor_id",
        "teaching_types",
    }
    if set(values) != required_fields:
        raise HTTPException(
            status_code=422,
            detail="Choose a course, level, term, instructor, and one or more teaching types.",
        )
    teaching_types = normalize_teaching_types(values["teaching_types"])

    try:
        with connect() as connection:
            cohort = connection.execute(
                "SELECT academic_term_id FROM cohorts WHERE id = %s",
                (values["cohort_id"],),
            ).fetchone()
            if cohort is None:
                raise HTTPException(status_code=404, detail="Level not found.")
            if cohort[0] != values["academic_term_id"]:
                raise HTTPException(
                    status_code=422,
                    detail="The selected term does not match the selected level.",
                )

            group_count = connection.execute(
                "SELECT count(*) FROM student_groups WHERE cohort_id = %s",
                (values["cohort_id"],),
            ).fetchone()[0]
            if group_count == 0:
                raise HTTPException(
                    status_code=422,
                    detail="Generate the level's student groups before adding its courses.",
                )

            instructor = connection.execute(
                """
                SELECT can_teach_lecture, can_teach_section, can_teach_lab
                FROM instructors
                WHERE id = %s
                """,
                (values["instructor_id"],),
            ).fetchone()
            if instructor is None:
                raise HTTPException(status_code=404, detail="Instructor not found.")
            unavailable_types = [
                activity
                for activity, enabled in zip(
                    ("lecture", "section", "lab"), instructor
                )
                if activity in teaching_types and not enabled
            ]
            if unavailable_types:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "The selected instructor is not enabled for: "
                        + ", ".join(unavailable_types)
                        + ". Update the instructor's teaching types or choose another instructor."
                    ),
                )

            weekly_lectures = (
                (group_count + 1) // 2 if "lecture" in teaching_types else 0
            )
            weekly_sections = group_count if "section" in teaching_types else 0
            weekly_labs = group_count if "lab" in teaching_types else 0
            cursor = connection.execute(
                """
                INSERT INTO course_offerings
                    (course_id, cohort_id, academic_term_id, instructor_id,
                     weekly_lectures, weekly_sections, weekly_labs, has_lab)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    values["course_id"],
                    values["cohort_id"],
                    values["academic_term_id"],
                    values["instructor_id"],
                    weekly_lectures,
                    weekly_sections,
                    weekly_labs,
                    "lab" in teaching_types,
                ),
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
    except psycopg.errors.ForeignKeyViolation as error:
        raise HTTPException(
            status_code=422,
            detail="One of the selected course, level, term, or instructor records no longer exists.",
        ) from error
    except psycopg.Error as error:
        raise HTTPException(
            status_code=400,
            detail="Could not save the course offering; check the selected records.",
        ) from error

    record = {key: json_value(value) for key, value in zip(columns, row)}
    record["derived_sessions"] = {
        "lectures": weekly_lectures,
        "sections": weekly_sections,
        "labs": weekly_labs,
    }
    record["teaching_types"] = teaching_types
    return record


@app.post("/api/data/cohorts/{cohort_id}/generate-groups")
def generate_missing_cohort_groups(cohort_id: int):
    with connect() as connection:
        return add_missing_groups(connection, cohort_id)


class GenerateRequest(BaseModel):
    academic_term_id: int


class FridayAvailabilityUpdate(BaseModel):
    friday_available: bool


class InstructorPermissionsUpdate(BaseModel):
    teaching_types: list[str] | None = None
    friday_available: bool | None = None


@app.patch("/api/data/instructors/{instructor_id}/permissions")
def update_instructor_permissions(
    instructor_id: int, update: InstructorPermissionsUpdate
):
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Choose a permission to update.")
    try:
        with connect() as connection:
            current = connection.execute(
                """
                SELECT can_teach_lecture, can_teach_section, can_teach_lab
                FROM instructors
                WHERE id = %s
                FOR UPDATE
                """,
                (instructor_id,),
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="Instructor not found.")
            next_capabilities = dict(zip(TEACHING_CAPABILITY_FIELDS, current))
            if "teaching_types" in updates:
                selected_types = normalize_teaching_types(updates.pop("teaching_types"))
                next_capabilities.update(
                    {
                        field: teaching_type in selected_types
                        for teaching_type, field in TEACHING_TYPE_FIELDS.items()
                    }
                )
                updates.update(
                    {
                        field: teaching_type in selected_types
                        for teaching_type, field in TEACHING_TYPE_FIELDS.items()
                    }
                )
            if any(field in updates for field in TEACHING_CAPABILITY_FIELDS):
                selected_types = instructor_teaching_types(next_capabilities)
                if not selected_types:
                    raise HTTPException(
                        status_code=422,
                        detail="Select at least one teaching type: lecture, section, or lab.",
                    )

            assignments = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(field)) for field in updates
            )
            query = sql.SQL(
                "UPDATE instructors SET {} WHERE id = %s RETURNING id, name, can_teach_lecture, can_teach_section, can_teach_lab, friday_available"
            ).format(assignments)
            row = connection.execute(
                query, (*updates.values(), instructor_id)
            ).fetchone()
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="Could not update instructor permissions."
        ) from error
    result = {
        "id": row[0],
        "name": row[1],
        "can_teach_lecture": row[2],
        "can_teach_section": row[3],
        "can_teach_lab": row[4],
        "friday_available": row[5],
    }
    result["teaching_types"] = instructor_teaching_types(result)
    return result


@app.patch("/api/data/instructors/{instructor_id}/friday-availability")
def update_friday_availability(
    instructor_id: int, update: FridayAvailabilityUpdate
):
    try:
        with connect() as connection:
            row = connection.execute(
                """
                UPDATE instructors
                SET friday_available = %s
                WHERE id = %s
                RETURNING id, name, friday_available
                """,
                (update.friday_available, instructor_id),
            ).fetchone()
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="Could not update instructor availability."
        ) from error
    if row is None:
        raise HTTPException(status_code=404, detail="Instructor not found.")
    return {"id": row[0], "name": row[1], "friday_available": row[2]}


@app.post("/api/generate")
def generate(request: GenerateRequest):
    try:
        with connect() as connection:
            offerings = connection.execute(
                """
                SELECT co.id, co.instructor_id, i.friday_available,
                       i.can_teach_lecture, i.can_teach_section, i.can_teach_lab,
                      co.weekly_lectures, co.weekly_sections, co.weekly_labs,
                      co.cohort_id
                FROM course_offerings co
                JOIN instructors i ON i.id = co.instructor_id
                WHERE co.academic_term_id = %s
                ORDER BY co.id
                """,
                (request.academic_term_id,),
            ).fetchall()
            groups_by_cohort: dict[int, list[tuple[int, int]]] = {}
            for cohort_id, group_id, student_count in connection.execute(
                """
                SELECT c.id, sg.id, sg.student_count
                FROM cohorts c
                JOIN student_groups sg ON sg.cohort_id = c.id
                WHERE c.academic_term_id = %s
                ORDER BY sg.id
                """,
                (request.academic_term_id,),
            ).fetchall():
                groups_by_cohort.setdefault(cohort_id, []).append((group_id, student_count))
            room_rows = connection.execute(
                "SELECT id, capacity, room_type FROM rooms ORDER BY id"
            ).fetchall()
            slot_rows = connection.execute(
                """
                SELECT id, day_of_week,
                       EXTRACT(HOUR FROM start_time)::int * 60 + EXTRACT(MINUTE FROM start_time)::int,
                       EXTRACT(HOUR FROM end_time)::int * 60 + EXTRACT(MINUTE FROM end_time)::int
                FROM time_slots ORDER BY day_of_week, start_time
                """
            ).fetchall()

            sessions: list[Session] = []
            metadata: dict[int, tuple[int, int, str]] = {}
            next_session_id = 1
            for offering_id, instructor_id, friday_available, can_lecture, can_section, can_lab, weekly_lectures, weekly_sections, weekly_labs, cohort_id in offerings:
                cohort_groups = groups_by_cohort.get(cohort_id, [])
                if not cohort_groups:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cohort {cohort_id} needs student groups before its timetable can be generated.",
                    )
                teaching_types = [
                    activity
                    for activity, weekly_count in (
                        ("lecture", weekly_lectures),
                        ("section", weekly_sections),
                        ("lab", weekly_labs),
                    )
                    if weekly_count > 0
                ]
                activity_specs = build_course_activity_specs(
                    cohort_groups, teaching_types
                )
                for activity_type, group_ids, student_count in activity_specs:
                    can_teach_activity = {
                        "lecture": can_lecture,
                        "section": can_section,
                        "lab": can_lab,
                    }[activity_type]
                    if not can_teach_activity:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"The assigned instructor is not enabled to teach {activity_type}. "
                                "Update the instructor's teaching permissions or assign another instructor."
                            ),
                        )
                    sessions.append(
                        Session(
                            next_session_id,
                            instructor_id,
                            group_ids,
                            student_count,
                            activity_type,
                            friday_available,
                        )
                    )
                    metadata[next_session_id] = (offering_id, len([key for key, value in metadata.items() if value[0] == offering_id and value[2] == activity_type]) + 1, activity_type)
                    next_session_id += 1

            if not sessions:
                raise HTTPException(status_code=400, detail="No course offerings with weekly sessions were found for this term.")

            time_slots = [TimeSlot(*row) for row in slot_rows]
            ensure_schedule_load_fits_slots(sessions, time_slots)
            assignments = generate_schedule(
                sessions,
                [Room(*row) for row in room_rows],
                time_slots,
            )

            connection.execute(
                "DELETE FROM timetable_sessions WHERE academic_term_id = %s",
                (request.academic_term_id,),
            )
            saved = []
            for assignment in assignments:
                offering_id, session_number, activity_type = metadata[assignment.session_id]
                instructor_id = next(
                    session.instructor_id
                    for session in sessions
                    if session.session_id == assignment.session_id
                )
                row = connection.execute(
                    """
                    INSERT INTO timetable_sessions
                        (offering_id, academic_term_id, instructor_id, room_id, time_slot_id, activity_type, session_number)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        offering_id,
                        request.academic_term_id,
                        instructor_id,
                        assignment.room_id,
                        assignment.time_slot_id,
                        activity_type.upper(),
                        session_number,
                    ),
                ).fetchone()
                session_id = row[0]
                group_ids = next(
                    session.group_ids
                    for session in sessions
                    if session.session_id == assignment.session_id
                )
                for group_id in group_ids:
                    connection.execute(
                        "INSERT INTO session_groups (session_id, time_slot_id, group_id) VALUES (%s, %s, %s)",
                        (session_id, assignment.time_slot_id, group_id),
                    )
                saved.append(
                    {
                        "session_id": session_id,
                        "offering_id": offering_id,
                        "activity_type": activity_type,
                        "session_number": session_number,
                        "room_id": assignment.room_id,
                        "time_slot_id": assignment.time_slot_id,
                        "group_ids": list(group_ids),
                    }
                )
    except ScheduleInfeasibleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Database operation failed.") from error
    return {"scheduled_sessions": len(saved), "sessions": saved}