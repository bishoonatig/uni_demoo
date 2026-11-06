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
COURSE_ACTIVITY_TYPES = frozenset({"lecture", "section", "lab"})

ENTITIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "departments": ("departments", "id", ("name",)),
    "academic_terms": (
        "academic_terms",
        "id",
        ("name", "start_date", "end_date"),
    ),
    "instructors": (
        "instructors",
        "id",
        (
            "name",
            "department_id",
            "part_time",
            "friday_available",
        ),
    ),
    "courses": (
        "courses",
        "id",
        ("code", "name", "department_id"),
    ),
    "rooms": ("rooms", "id", ("name", "capacity", "room_type")),
    "time_slots": (
        "time_slots",
        "id",
        ("day_of_week", "start_time", "end_time"),
    ),
    "student_groups": (
        "student_groups",
        "id",
        ("course_id", "academic_term_id", "group_number", "student_count"),
    ),
    "course_offerings": (
        "course_offerings",
        "id",
        (
            "course_id",
            "academic_term_id",
            "instructor_id",
            "total_students",
            "teaching_types",
        ),
    ),
}
DELETABLE_ENTITIES = frozenset(
    {"departments", "academic_terms", "instructors", "courses", "rooms"}
)


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


def ensure_instructor_part_time_column(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        ALTER TABLE instructors
        ADD COLUMN IF NOT EXISTS part_time BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


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


def normalize_teaching_types(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise HTTPException(
            status_code=422,
            detail="Choose one or more teaching types.",
        )
    normalized = list(dict.fromkeys(str(value).strip().lower() for value in values))
    if not normalized or set(normalized) - COURSE_ACTIVITY_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Select at least one teaching type: lecture, section, or lab.",
        )
    return normalized


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
    connection: psycopg.Connection, offering_id: int
) -> dict[str, Any]:
    offering = connection.execute(
        """
        SELECT co.course_id, co.academic_term_id, co.total_students
        FROM course_offerings co
        WHERE co.id = %s
        FOR UPDATE OF co
        """,
        (offering_id,),
    ).fetchone()
    if offering is None:
        raise HTTPException(status_code=404, detail="Course offering not found.")
    course_id, academic_term_id, total_students = offering

    existing_groups = connection.execute(
        """
        SELECT group_number, student_count
        FROM student_groups
        WHERE course_id = %s AND academic_term_id = %s
        ORDER BY group_number
        FOR UPDATE
        """,
        (course_id, academic_term_id),
    ).fetchall()
    existing_total = sum(student_count for _, student_count in existing_groups)
    if existing_total > total_students:
        raise HTTPException(
            status_code=409,
            detail="Existing course groups contain more students than the course total.",
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
            INSERT INTO student_groups
                (course_id, academic_term_id, group_number, student_count)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (course_id, academic_term_id, group_number, student_count),
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


def sync_course_groups(
    connection: psycopg.Connection, offering_id: int
) -> dict[str, Any]:
    offering = connection.execute(
        """
        SELECT co.course_id, co.academic_term_id, co.total_students
        FROM course_offerings co
        WHERE co.id = %s
        """,
        (offering_id,),
    ).fetchone()
    if offering is None:
        raise HTTPException(status_code=404, detail="Course offering not found.")
    course_id, academic_term_id, total_students = offering
    enrollment_totals = connection.execute(
        """
        SELECT MIN(total_students), MAX(total_students)
        FROM course_offerings
        WHERE course_id = %s AND academic_term_id = %s
        """,
        (course_id, academic_term_id),
    ).fetchone()
    if enrollment_totals[0] != enrollment_totals[1]:
        raise HTTPException(
            status_code=409,
            detail="All offerings of the same course in one term must use the same student count.",
        )
    expected_sizes = build_group_sizes(total_students)
    existing_groups = connection.execute(
        """
        SELECT student_count
        FROM student_groups
        WHERE course_id = %s AND academic_term_id = %s
        ORDER BY group_number
        """,
        (course_id, academic_term_id),
    ).fetchall()
    if [student_count for (student_count,) in existing_groups] != expected_sizes:
        connection.execute(
            """
            DELETE FROM student_groups
            WHERE course_id = %s AND academic_term_id = %s
            """,
            (course_id, academic_term_id),
        )
        return add_missing_groups(connection, offering_id)
    return {"groups": [], "group_count": len(existing_groups)}


def build_course_activity_specs(
    offering_groups: list[tuple[int, int]], teaching_types: list[str]
) -> list[tuple[str, tuple[int, ...], int]]:
    group_pairs = [
        offering_groups[index : index + 2]
        for index in range(0, len(offering_groups), 2)
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
            for group_id, student_count in offering_groups
        )
    if "lab" in teaching_types:
        specs.extend(
            ("lab", (group_id,), student_count)
            for group_id, student_count in offering_groups
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
        if entity == "instructors":
            ensure_instructor_part_time_column(connection)
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
            detail="Student groups are generated automatically from course offering totals.",
        )
    config = ENTITIES.get(entity)
    if config is None:
        raise HTTPException(status_code=404, detail="Unknown data type.")
    table, _, allowed_fields = config
    if not values or set(values) - set(allowed_fields):
        raise HTTPException(status_code=422, detail="Invalid or empty fields.")
    if entity == "courses":
        required_fields = {"code", "name", "department_id"}
        if set(values) != required_fields:
            raise HTTPException(
                status_code=422,
                detail="Enter the course code, name, and department.",
            )
    if entity == "time_slots":
        return replace_weekly_time_slots(values)
    if entity == "course_offerings":
        return create_course_offering(values)
    values = values.copy()
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
            if entity == "instructors":
                ensure_instructor_part_time_column(connection)
            cursor = connection.execute(statement, tuple(values[field] for field in fields))
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
    except psycopg.errors.UndefinedColumn as error:
        raise HTTPException(
            status_code=503,
            detail="The database schema is outdated. Run 02_timetable_schema.sql against this database, then try again.",
        ) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=400, detail="Could not save the record; check required fields and references.") from error
    record = {key: json_value(value) for key, value in zip(columns, row)}
    return record


def prepare_course_offering(
    connection: psycopg.Connection,
    values: dict[str, Any],
    offering_id: int | None = None,
) -> tuple[list[str], int, int, int, int]:
    required_fields = {
        "course_id",
        "academic_term_id",
        "instructor_id",
        "total_students",
        "teaching_types",
    }
    if set(values) != required_fields:
        raise HTTPException(
            status_code=422,
            detail="Choose a course, term, instructor, enter total students, and select one or more teaching types.",
        )
    total_students = values["total_students"]
    if (
        isinstance(total_students, bool)
        or not isinstance(total_students, int)
        or total_students < 1
    ):
        raise HTTPException(
            status_code=422,
            detail="Course offering total students must be a positive integer.",
        )
    teaching_types = normalize_teaching_types(values["teaching_types"])
    course = connection.execute(
        "SELECT id FROM courses WHERE id = %s",
        (values["course_id"],),
    ).fetchone()
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")

    group_count = len(build_group_sizes(total_students))
    enrollment_query = """
        SELECT EXISTS (
            SELECT 1
            FROM course_offerings
            WHERE course_id = %s AND academic_term_id = %s
              AND total_students <> %s
    """
    enrollment_params: tuple[Any, ...] = (
        values["course_id"],
        values["academic_term_id"],
        total_students,
    )
    if offering_id is not None:
        enrollment_query += " AND id <> %s"
        enrollment_params += (offering_id,)
    enrollment_query += ")"
    enrollment_conflict = connection.execute(
        enrollment_query, enrollment_params
    ).fetchone()[0]
    if enrollment_conflict:
        raise HTTPException(
            status_code=409,
            detail="This course already has a different student count in the selected term.",
        )

    instructor = connection.execute(
        "SELECT id FROM instructors WHERE id = %s",
        (values["instructor_id"],),
    ).fetchone()
    if instructor is None:
        raise HTTPException(status_code=404, detail="Instructor not found.")

    return (
        teaching_types,
        total_students,
        (group_count + 1) // 2 if "lecture" in teaching_types else 0,
        group_count if "section" in teaching_types else 0,
        group_count if "lab" in teaching_types else 0,
    )


def create_course_offering(values: dict[str, Any]) -> dict[str, Any]:
    try:
        with connect() as connection:
            (
                teaching_types,
                total_students,
                weekly_lectures,
                weekly_sections,
                weekly_labs,
            ) = prepare_course_offering(connection, values)
            cursor = connection.execute(
                """
                INSERT INTO course_offerings
                    (course_id, academic_term_id, instructor_id,
                     total_students, weekly_lectures, weekly_sections, weekly_labs,
                     has_lab)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    values["course_id"],
                    values["academic_term_id"],
                    values["instructor_id"],
                    total_students,
                    weekly_lectures,
                    weekly_sections,
                    weekly_labs,
                    "lab" in teaching_types,
                ),
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
            generated_groups = sync_course_groups(
                connection, row[columns.index("id")]
            )
            connection.execute(
                "DELETE FROM timetable_sessions WHERE academic_term_id = %s",
                (values["academic_term_id"],),
            )
    except psycopg.errors.ForeignKeyViolation as error:
        raise HTTPException(
            status_code=422,
            detail="One of the selected course, term, or instructor records no longer exists.",
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
    record.update(generated_groups)
    record["teaching_types"] = teaching_types
    record["timetable_invalidated_terms"] = [values["academic_term_id"]]
    return record


@app.patch("/api/data/course_offerings/{offering_id}")
def update_course_offering(offering_id: int, values: dict[str, Any]) -> dict[str, Any]:
    try:
        with connect() as connection:
            existing = connection.execute(
                "SELECT academic_term_id, course_id FROM course_offerings WHERE id = %s FOR UPDATE",
                (offering_id,),
            ).fetchone()
            if existing is None:
                raise HTTPException(status_code=404, detail="Course offering not found.")
            (
                teaching_types,
                total_students,
                weekly_lectures,
                weekly_sections,
                weekly_labs,
            ) = prepare_course_offering(connection, values, offering_id)
            affected_terms = sorted({existing[0], values["academic_term_id"]})
            connection.execute(
                "DELETE FROM timetable_sessions WHERE academic_term_id = ANY(%s)",
                (affected_terms,),
            )
            cursor = connection.execute(
                """
                UPDATE course_offerings
                SET course_id = %s, academic_term_id = %s,
                    instructor_id = %s, total_students = %s, weekly_lectures = %s,
                    weekly_sections = %s, weekly_labs = %s, has_lab = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    values["course_id"],
                    values["academic_term_id"],
                    values["instructor_id"],
                    total_students,
                    weekly_lectures,
                    weekly_sections,
                    weekly_labs,
                    "lab" in teaching_types,
                    offering_id,
                ),
            )
            row = cursor.fetchone()
            columns = [column.name for column in cursor.description]
            generated_groups = sync_course_groups(connection, offering_id)
            if (
                existing[0] != values["academic_term_id"]
                or existing[1] != values["course_id"]
            ):
                remaining_offerings = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM course_offerings
                        WHERE course_id = %s AND academic_term_id = %s
                    )
                    """,
                    (existing[1], existing[0]),
                ).fetchone()[0]
                if not remaining_offerings:
                    connection.execute(
                        """
                        DELETE FROM student_groups
                        WHERE course_id = %s AND academic_term_id = %s
                        """,
                        (existing[1], existing[0]),
                    )
    except psycopg.errors.ForeignKeyViolation as error:
        raise HTTPException(
            status_code=422,
            detail="One of the selected course, term, or instructor records no longer exists.",
        ) from error
    except psycopg.Error as error:
        raise HTTPException(
            status_code=400,
            detail="Could not update the course offering; check the selected records.",
        ) from error

    record = {key: json_value(value) for key, value in zip(columns, row)}
    record["teaching_types"] = teaching_types
    record["derived_sessions"] = {
        "lectures": weekly_lectures,
        "sections": weekly_sections,
        "labs": weekly_labs,
    }
    record.update(generated_groups)
    record["timetable_invalidated_terms"] = affected_terms
    return record


@app.delete("/api/data/course_offerings/{offering_id}")
def delete_course_offering(offering_id: int) -> dict[str, Any]:
    try:
        with connect() as connection:
            offering = connection.execute(
                "SELECT academic_term_id, course_id FROM course_offerings WHERE id = %s FOR UPDATE",
                (offering_id,),
            ).fetchone()
            if offering is None:
                raise HTTPException(status_code=404, detail="Course offering not found.")
            connection.execute(
                "DELETE FROM timetable_sessions WHERE academic_term_id = %s",
                (offering[0],),
            )
            connection.execute(
                "DELETE FROM course_offerings WHERE id = %s",
                (offering_id,),
            )
            has_remaining_offerings = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM course_offerings
                    WHERE course_id = %s AND academic_term_id = %s
                )
                """,
                (offering[1], offering[0]),
            ).fetchone()[0]
            if not has_remaining_offerings:
                connection.execute(
                    """
                    DELETE FROM student_groups
                    WHERE course_id = %s AND academic_term_id = %s
                    """,
                    (offering[1], offering[0]),
                )
    except psycopg.Error as error:
        raise HTTPException(
            status_code=400,
            detail="Could not delete the course offering.",
        ) from error
    return {
        "id": offering_id,
        "timetable_invalidated_terms": [offering[0]],
    }


@app.delete("/api/data/{entity}/{record_id}")
def delete_entity(entity: str, record_id: int) -> dict[str, Any]:
    if entity not in DELETABLE_ENTITIES:
        raise HTTPException(status_code=404, detail="Unknown or non-deletable data type.")
    table, primary_key, _ = ENTITIES[entity]
    invalidated_terms: list[int] = []
    try:
        with connect() as connection:
            cursor = connection.execute(
                sql.SQL("DELETE FROM {} WHERE {} = %s RETURNING {}").format(
                    sql.Identifier(table),
                    sql.Identifier(primary_key),
                    sql.Identifier(primary_key),
                ),
                (record_id,),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail="Record not found.")
    except psycopg.errors.ForeignKeyViolation as error:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete this record because it is still used by other records. Delete or update those records first.",
        ) from error
    except psycopg.Error as error:
        raise HTTPException(
            status_code=400,
            detail="Could not delete the record.",
        ) from error
    return {
        "id": record_id,
        "timetable_invalidated_terms": invalidated_terms,
    }


@app.post("/api/data/course_offerings/{offering_id}/generate-groups")
def generate_missing_offering_groups(offering_id: int):
    with connect() as connection:
        result = sync_course_groups(connection, offering_id)
        if result["groups"]:
            term_id = connection.execute(
                "SELECT academic_term_id FROM course_offerings WHERE id = %s",
                (offering_id,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM timetable_sessions WHERE academic_term_id = %s",
                (term_id,),
            )
            result["timetable_invalidated_terms"] = [term_id]
        return result


class FridayAvailabilityUpdate(BaseModel):
    friday_available: bool


class InstructorPermissionsUpdate(BaseModel):
    part_time: bool | None = None
    friday_available: bool | None = None


@app.patch("/api/data/instructors/{instructor_id}/permissions")
def update_instructor_permissions(
    instructor_id: int, update: InstructorPermissionsUpdate
):
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Choose an instructor attribute to update.")
    if set(updates) - {"part_time", "friday_available"}:
        raise HTTPException(status_code=422, detail="Invalid instructor attribute.")
    try:
        with connect() as connection:
            ensure_instructor_part_time_column(connection)
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(field)) for field in updates
            )
            query = sql.SQL(
                "UPDATE instructors SET {} WHERE id = %s RETURNING id, name, part_time, friday_available"
            ).format(assignments)
            row = connection.execute(
                query, (*updates.values(), instructor_id)
            ).fetchone()
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="Could not update instructor attributes."
        ) from error
    if row is None:
        raise HTTPException(status_code=404, detail="Instructor not found.")
    result = {
        "id": row[0],
        "name": row[1],
        "part_time": row[2],
        "friday_available": row[3],
    }
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


def load_saved_schedule_sessions(connection: psycopg.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT ts.id, ts.offering_id, ts.academic_term_id,
               ts.activity_type, ts.session_number, ts.room_id,
               ts.time_slot_id,
               COALESCE(
                   array_agg(sg.group_id ORDER BY sg.group_id)
                       FILTER (WHERE sg.group_id IS NOT NULL),
                   ARRAY[]::BIGINT[]
               ) AS group_ids
        FROM timetable_sessions ts
        LEFT JOIN session_groups sg ON sg.session_id = ts.id
        GROUP BY ts.id
        ORDER BY ts.academic_term_id, ts.id
        """
    ).fetchall()
    return [
        {
            "session_id": row[0],
            "offering_id": row[1],
            "academic_term_id": row[2],
            "activity_type": row[3].lower(),
            "session_number": row[4],
            "room_id": row[5],
            "time_slot_id": row[6],
            "group_ids": row[7],
        }
        for row in rows
    ]


@app.get("/api/schedule")
def get_saved_schedule() -> dict[str, Any]:
    try:
        with connect() as connection:
            sessions = load_saved_schedule_sessions(connection)
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="Could not load the saved timetable."
        ) from error
    return {"scheduled_sessions": len(sessions), "sessions": sessions}


@app.post("/api/generate")
def generate():
    try:
        with connect() as connection:
            offerings = connection.execute(
                """
                SELECT co.id, co.instructor_id, i.friday_available,
                       co.weekly_lectures, co.weekly_sections, co.weekly_labs,
                       co.course_id, co.academic_term_id
                FROM course_offerings co
                JOIN instructors i ON i.id = co.instructor_id
                ORDER BY co.id
                """,
            ).fetchall()
            groups_by_course_term: dict[tuple[int, int], list[tuple[int, int]]] = {}
            for course_id, academic_term_id, group_id, student_count in connection.execute(
                """
                SELECT sg.course_id, sg.academic_term_id, sg.id, sg.student_count
                FROM student_groups sg
                ORDER BY sg.id
                """,
            ).fetchall():
                groups_by_course_term.setdefault(
                    (course_id, academic_term_id), []
                ).append((group_id, student_count))
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
            metadata: dict[int, tuple[int, int, int, str]] = {}
            session_numbers: dict[tuple[int, str], int] = {}
            next_session_id = 1
            for offering_id, instructor_id, friday_available, weekly_lectures, weekly_sections, weekly_labs, course_id, academic_term_id in offerings:
                course_groups = groups_by_course_term.get(
                    (course_id, academic_term_id), []
                )
                if not course_groups:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Course offering {offering_id} in term {academic_term_id} needs student groups before the timetable can be generated.",
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
                    course_groups, teaching_types
                )
                for activity_type, group_ids, student_count in activity_specs:
                    session_key = (offering_id, activity_type)
                    session_numbers[session_key] = session_numbers.get(session_key, 0) + 1
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
                    metadata[next_session_id] = (
                        offering_id,
                        academic_term_id,
                        session_numbers[session_key],
                        activity_type,
                    )
                    next_session_id += 1

            if not sessions:
                raise HTTPException(
                    status_code=400,
                    detail="No course offerings with weekly sessions were found.",
                )

            time_slots = [TimeSlot(*row) for row in slot_rows]
            ensure_schedule_load_fits_slots(sessions, time_slots)
            assignments = generate_schedule(
                sessions,
                [Room(*row) for row in room_rows],
                time_slots,
            )

            connection.execute("DELETE FROM timetable_sessions")
            for assignment in assignments:
                (
                    offering_id,
                    academic_term_id,
                    session_number,
                    activity_type,
                ) = metadata[assignment.session_id]
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
                        academic_term_id,
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
            schedule_sessions = load_saved_schedule_sessions(connection)
    except ScheduleInfeasibleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Database operation failed.") from error
    return {
        "scheduled_sessions": len(schedule_sessions),
        "sessions": schedule_sessions,
    }