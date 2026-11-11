from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Iterator

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from psycopg import sql
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel

if TYPE_CHECKING:
    from timetable_scheduler import Room, ScheduleInfeasibleError, Session, TimeSlot


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
connection_pool = (
    ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=0,
        max_size=10,
        timeout=10,
        open=False,
    )
    if DATABASE_URL
    else None
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if connection_pool is not None:
        connection_pool.open()
    try:
        yield
    finally:
        if connection_pool is not None:
            connection_pool.close()


app = FastAPI(title="University Timetable Generator", lifespan=lifespan)
MAX_GROUP_SIZE = 20
PERIODS_PER_DAY = 6
PERIOD_DURATION_MINUTES = 100
BREAK_DURATION_MINUTES = 20
FRIDAY = 5
TEACHING_DAYS = (1, 2, 3, 4, 5, 6, 7)
COURSE_ACTIVITY_TYPES = frozenset({"lecture", "section", "lab"})
TIMETABLE_RULES_VERSION = 2

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
            "lecture_online",
        ),
    ),
}
DELETABLE_ENTITIES = frozenset(
    {"departments", "academic_terms", "instructors", "courses", "rooms"}
)


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    if connection_pool is None:
        raise HTTPException(
            status_code=503,
            detail="Set DATABASE_URL to connect the app to PostgreSQL.",
        )
    try:
        with connection_pool.connection() as connection:
            yield connection
    except PoolTimeout as error:
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


def build_schedule_group_ids(
    student_groups: list[tuple[int, int, int]],
) -> dict[int, int]:
    shared_ids: dict[tuple[int, int], int] = {}
    group_ids_by_id: dict[int, int] = {}
    for group_id, academic_term_id, group_number in student_groups:
        group_key = (academic_term_id, group_number)
        if group_key not in shared_ids:
            shared_ids[group_key] = len(shared_ids) + 1
        group_ids_by_id[group_id] = shared_ids[group_key]
    return group_ids_by_id


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
    group_sizes = build_group_sizes(total_students - existing_total)
    if group_sizes:
        group_rows = connection.execute(
            """
            INSERT INTO student_groups
                (course_id, academic_term_id, group_number, student_count)
            SELECT %s, %s, %s + group_data.ordinality::integer - 1,
                   group_data.student_count
            FROM unnest(%s::integer[]) WITH ORDINALITY
                AS group_data(student_count, ordinality)
            RETURNING id, group_number, student_count
            """,
            (course_id, academic_term_id, first_group_number, group_sizes),
        ).fetchall()
    else:
        group_rows = []
    generated_groups = [
        {
            "id": group_id,
            "group_number": group_number,
            "group_label": group_label(group_number),
            "student_count": student_count,
        }
        for group_id, group_number, student_count in sorted(
            group_rows, key=lambda group: group[1]
        )
    ]

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

            slot_rows = connection.execute(
                """
                INSERT INTO time_slots (day_of_week, start_time, end_time)
                SELECT slot_data.day_of_week, slot_data.start_time, slot_data.end_time
                FROM unnest(%s::smallint[], %s::time[], %s::time[])
                    AS slot_data(day_of_week, start_time, end_time)
                RETURNING id, day_of_week, start_time, end_time
                """,
                (
                    [day for day in TEACHING_DAYS for _ in periods],
                    [start for _ in TEACHING_DAYS for start, _ in periods],
                    [end for _ in TEACHING_DAYS for _, end in periods],
                ),
            ).fetchall()
            created = [
                {
                    "id": slot_id,
                    "day_of_week": day,
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                }
                for slot_id, day, start, end in sorted(
                    slot_rows, key=lambda slot: (slot[1], slot[2])
                )
            ]
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
        cursor = connection.execute(
            sql.SQL("SELECT * FROM {} ORDER BY {}").format(
                sql.Identifier(table), sql.Identifier(primary_key)
            )
        )
        rows = cursor.fetchall()
        columns = [column.name for column in cursor.description]
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
        "lecture_online",
    }
    if set(values) != required_fields:
        raise HTTPException(
            status_code=422,
            detail=(
                "Choose a course and term, enter total students, and select one "
                "or more teaching types. Instructor is optional."
            ),
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
    instructor_id = values["instructor_id"]
    if instructor_id is not None:
        instructor = connection.execute(
            "SELECT id FROM instructors WHERE id = %s",
            (instructor_id,),
        ).fetchone()
        if instructor is None:
            raise HTTPException(status_code=404, detail="Instructor not found.")

    teaching_types = normalize_teaching_types(values["teaching_types"])
    if not isinstance(values["lecture_online"], bool):
        raise HTTPException(
            status_code=422,
            detail="Choose whether the lectures are online.",
        )
    if values["lecture_online"] and "lecture" not in teaching_types:
        raise HTTPException(
            status_code=422,
            detail="Online delivery can only be selected when Lecture is included.",
        )
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
                     has_lab, lecture_online)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    values["lecture_online"],
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
    except psycopg.errors.UndefinedColumn as error:
        raise HTTPException(
            status_code=503,
            detail="The database schema is outdated. Run 02_timetable_schema.sql against this database, then try again.",
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
                    weekly_sections = %s, weekly_labs = %s, has_lab = %s,
                    lecture_online = %s
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
                    values["lecture_online"],
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
    except psycopg.errors.UndefinedColumn as error:
        raise HTTPException(
            status_code=503,
            detail="The database schema is outdated. Run 02_timetable_schema.sql against this database, then try again.",
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


class ScheduleSessionUpdate(BaseModel):
    room_id: int | None = None
    time_slot_id: int


class ScheduleSessionSwap(BaseModel):
    first_session_id: int
    second_session_id: int
    swap_time: bool = True
    swap_room: bool = True


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
               ts.is_online, ts.time_slot_id, ts.instructor_id, i.name,
               (ts.activity_type = 'LECTURE' AND NOT ts.is_online
                   AND COALESCE(i.friday_available, FALSE)) AS is_biweekly,
               ts.schedule_version,
               COALESCE(
                   array_agg(sg.group_id ORDER BY sg.group_id)
                       FILTER (WHERE sg.group_id IS NOT NULL),
                   ARRAY[]::BIGINT[]
               ) AS group_ids
        FROM timetable_sessions ts
        LEFT JOIN session_groups sg ON sg.session_id = ts.id
        LEFT JOIN instructors i ON i.id = ts.instructor_id
        GROUP BY ts.id, i.name, i.friday_available
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
            "is_online": row[6],
            "time_slot_id": row[7],
            "instructor_id": row[8],
            "instructor_name": row[9],
            "is_biweekly": row[10],
            "schedule_version": row[11],
            "group_ids": row[12],
        }
        for row in rows
    ]


@app.get("/api/schedule")
def get_saved_schedule() -> dict[str, Any]:
    try:
        with connect() as connection:
            sessions = load_saved_schedule_sessions(connection)
    except psycopg.errors.UndefinedColumn as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "The database schema is outdated. Run 02_timetable_schema.sql "
                "against this database, then try again."
            ),
        ) from error
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="Could not load the saved timetable."
        ) from error
    return {
        "scheduled_sessions": len(sessions),
        "sessions": sessions,
        "is_current": bool(sessions)
        and all(
            session["schedule_version"] == TIMETABLE_RULES_VERSION
            for session in sessions
        ),
    }


@app.patch("/api/schedule/sessions/{session_id}")
def update_schedule_session(
    session_id: int, update: ScheduleSessionUpdate
) -> dict[str, Any]:
    try:
        with connect() as connection:
            session = connection.execute(
                """
                SELECT ts.instructor_id, ts.activity_type,
                       co.total_students, COALESCE(i.friday_available, FALSE),
                       ts.is_online
                FROM timetable_sessions ts
                JOIN course_offerings co ON co.id = ts.offering_id
                LEFT JOIN instructors i ON i.id = ts.instructor_id
                WHERE ts.id = %s
                FOR UPDATE OF ts
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                raise HTTPException(status_code=404, detail="Scheduled session not found.")
            (
                instructor_id,
                activity_type,
                student_count,
                friday_available,
                is_online,
            ) = session
            session_group_count = connection.execute(
                """
                SELECT SUM(sg.student_count)
                FROM session_groups session_group
                JOIN student_groups sg ON sg.id = session_group.group_id
                WHERE session_group.session_id = %s
                """,
                (session_id,),
            ).fetchone()[0]
            if session_group_count is not None:
                student_count = session_group_count

            slot = connection.execute(
                "SELECT day_of_week FROM time_slots WHERE id = %s",
                (update.time_slot_id,),
            ).fetchone()
            if slot is None:
                raise HTTPException(status_code=404, detail="Time period not found.")
            if slot[0] == FRIDAY and not friday_available:
                raise HTTPException(
                    status_code=422,
                    detail="This instructor is not available on Friday.",
                )

            if is_online and update.room_id is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Online sessions cannot be assigned a room.",
                )
            if not is_online and update.room_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Choose a room for this in-person session.",
                )
            if update.room_id is not None:
                room = connection.execute(
                    "SELECT capacity, room_type FROM rooms WHERE id = %s",
                    (update.room_id,),
                ).fetchone()
                if room is None:
                    raise HTTPException(status_code=404, detail="Room not found.")
                room_capacity, room_type = room
                if room_capacity < student_count:
                    raise HTTPException(
                        status_code=422,
                        detail=f"The selected room must fit all {student_count} students.",
                    )
                if room_type.casefold() != activity_type.casefold():
                    raise HTTPException(
                        status_code=422,
                        detail=f"This session requires a {activity_type.lower()} room.",
                    )

            has_conflict = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM timetable_sessions other
                    JOIN time_slots other_slot ON other_slot.id = other.time_slot_id
                    JOIN time_slots target_slot ON target_slot.id = %s
                    WHERE other.id <> %s
                      AND other_slot.day_of_week = target_slot.day_of_week
                      AND target_slot.start_time < other_slot.end_time
                      AND other_slot.start_time < target_slot.end_time
                      AND (
                          other.room_id = %s
                          OR other.instructor_id = %s
                          OR EXISTS (
                              SELECT 1
                              FROM session_groups current_group
                              JOIN session_groups other_group
                                  ON other_group.session_id = other.id
                              JOIN student_groups current_students
                                  ON current_students.id = current_group.group_id
                              JOIN student_groups other_students
                                  ON other_students.id = other_group.group_id
                              WHERE current_group.session_id = %s
                                AND current_students.academic_term_id
                                    = other_students.academic_term_id
                                AND current_students.group_number
                                    = other_students.group_number
                          )
                      )
                )
                """,
                (
                    update.time_slot_id,
                    session_id,
                    update.room_id,
                    instructor_id,
                    session_id,
                ),
            ).fetchone()[0]
            if has_conflict:
                raise HTTPException(
                    status_code=409,
                    detail="That period conflicts with this room, instructor, or student group.",
                )

            group_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT group_id FROM session_groups WHERE session_id = %s",
                    (session_id,),
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM session_groups WHERE session_id = %s",
                (session_id,),
            )
            connection.execute(
                """
                UPDATE timetable_sessions
                SET room_id = %s, time_slot_id = %s
                WHERE id = %s
                """,
                (update.room_id, update.time_slot_id, session_id),
            )
            connection.cursor().executemany(
                """
                INSERT INTO session_groups (session_id, time_slot_id, group_id)
                VALUES (%s, %s, %s)
                """,
                [
                    (session_id, update.time_slot_id, group_id)
                    for group_id in group_ids
                ],
            )
            updated_session = next(
                item
                for item in load_saved_schedule_sessions(connection)
                if item["session_id"] == session_id
            )
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not update the scheduled session: "
                f"{error.diag.message_primary or 'database error'}"
            ),
        ) from error
    return updated_session


@app.delete("/api/schedule/sessions/{session_id}")
def delete_schedule_session(session_id: int) -> dict[str, int]:
    try:
        with connect() as connection:
            deleted = connection.execute(
                "DELETE FROM timetable_sessions WHERE id = %s RETURNING id",
                (session_id,),
            ).fetchone()
            if deleted is None:
                raise HTTPException(status_code=404, detail="Scheduled session not found.")
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503, detail="Could not delete the scheduled session."
        ) from error
    return {"id": session_id}


@app.post("/api/schedule/sessions/swap")
def swap_schedule_sessions(update: ScheduleSessionSwap) -> dict[str, Any]:
    if update.first_session_id == update.second_session_id:
        raise HTTPException(status_code=422, detail="Choose two different sessions.")
    if not update.swap_time and not update.swap_room:
        raise HTTPException(
            status_code=422, detail="Choose whether to swap the time, the room, or both."
        )

    session_ids = [update.first_session_id, update.second_session_id]
    try:
        with connect() as connection:
            rows = connection.execute(
                """
                SELECT ts.id, ts.instructor_id, ts.activity_type,
                       co.total_students, COALESCE(i.friday_available, FALSE),
                       ts.room_id, ts.is_online, ts.time_slot_id,
                       ts.academic_term_id
                FROM timetable_sessions ts
                JOIN course_offerings co ON co.id = ts.offering_id
                LEFT JOIN instructors i ON i.id = ts.instructor_id
                WHERE ts.id = ANY(%s)
                ORDER BY ts.id
                FOR UPDATE OF ts
                """,
                (session_ids,),
            ).fetchall()
            if len(rows) != 2:
                raise HTTPException(status_code=404, detail="One or both sessions were not found.")

            sessions = {
                row[0]: {
                    "id": row[0],
                    "instructor_id": row[1],
                    "activity_type": row[2],
                    "student_count": row[3],
                    "friday_available": row[4],
                    "room_id": row[5],
                    "is_online": row[6],
                    "time_slot_id": row[7],
                    "academic_term_id": row[8],
                }
                for row in rows
            }
            group_rows = connection.execute(
                """
                SELECT sg.session_id, sg.group_id, student_group.student_count,
                       student_group.academic_term_id,
                       student_group.group_number
                FROM session_groups sg
                JOIN student_groups student_group ON student_group.id = sg.group_id
                WHERE sg.session_id = ANY(%s)
                """,
                (session_ids,),
            ).fetchall()
            group_ids = {session_id: [] for session_id in session_ids}
            group_keys = {session_id: set() for session_id in session_ids}
            group_student_counts: dict[int, int] = {}
            for (
                session_id,
                group_id,
                student_count,
                academic_term_id,
                group_number,
            ) in group_rows:
                group_ids[session_id].append(group_id)
                group_keys[session_id].add((academic_term_id, group_number))
                group_student_counts[group_id] = student_count
            for session_id in session_ids:
                if group_ids[session_id]:
                    sessions[session_id]["student_count"] = sum(
                        group_student_counts[group_id]
                        for group_id in group_ids[session_id]
                    )

            first = sessions[update.first_session_id]
            second = sessions[update.second_session_id]
            if (
                update.swap_room
                and first["is_online"] != second["is_online"]
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Rooms cannot be swapped between online and in-person sessions.",
                )
            proposed = {
                first["id"]: {
                    "room_id": second["room_id"] if update.swap_room else first["room_id"],
                    "time_slot_id": second["time_slot_id"] if update.swap_time else first["time_slot_id"],
                },
                second["id"]: {
                    "room_id": first["room_id"] if update.swap_room else second["room_id"],
                    "time_slot_id": first["time_slot_id"] if update.swap_time else second["time_slot_id"],
                },
            }
            slots = {
                row[0]: row[1]
                for row in connection.execute(
                    """
                    SELECT id, day_of_week
                    FROM time_slots
                    WHERE id = ANY(%s)
                    """,
                    ([item["time_slot_id"] for item in proposed.values()],),
                ).fetchall()
            }
            if any(
                assignment["time_slot_id"] not in slots
                for assignment in proposed.values()
            ):
                raise HTTPException(status_code=404, detail="A session time period was not found.")

            for session_id in session_ids:
                session = sessions[session_id]
                assignment = proposed[session_id]
                if slots[assignment["time_slot_id"]] == FRIDAY and not session["friday_available"]:
                    raise HTTPException(
                        status_code=422,
                        detail="One of the instructors is not available on Friday.",
                    )
                if session["is_online"]:
                    if assignment["room_id"] is not None:
                        raise HTTPException(
                            status_code=422,
                            detail="Online sessions cannot be assigned a room.",
                        )
                else:
                    room = connection.execute(
                        "SELECT capacity, room_type FROM rooms WHERE id = %s",
                        (assignment["room_id"],),
                    ).fetchone()
                    if room is None:
                        raise HTTPException(status_code=404, detail="A session room was not found.")
                    if room[0] < session["student_count"]:
                        raise HTTPException(
                            status_code=422,
                            detail=f"The selected room must fit all {session['student_count']} students.",
                        )
                    if room[1].casefold() != session["activity_type"].casefold():
                        raise HTTPException(
                            status_code=422,
                            detail=f"Each session must stay in a {session['activity_type'].lower()} room.",
                        )

                has_conflict = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM timetable_sessions other
                        JOIN time_slots other_slot ON other_slot.id = other.time_slot_id
                        JOIN time_slots target_slot ON target_slot.id = %s
                        WHERE NOT (other.id = ANY(%s))
                          AND other_slot.day_of_week = target_slot.day_of_week
                          AND target_slot.start_time < other_slot.end_time
                          AND other_slot.start_time < target_slot.end_time
                          AND (
                              other.room_id = %s
                              OR other.instructor_id = %s
                              OR EXISTS (
                                  SELECT 1
                                  FROM session_groups current_group
                                  JOIN session_groups other_group
                                          ON other_group.session_id = other.id
                                      JOIN student_groups current_students
                                          ON current_students.id = current_group.group_id
                                      JOIN student_groups other_students
                                          ON other_students.id = other_group.group_id
                                  WHERE current_group.session_id = %s
                                        AND current_students.academic_term_id
                                            = other_students.academic_term_id
                                        AND current_students.group_number
                                            = other_students.group_number
                                  )
                          )
                    )
                    """,
                    (
                        assignment["time_slot_id"],
                        session_ids,
                        assignment["room_id"],
                        session["instructor_id"],
                        session_id,
                    ),
                ).fetchone()[0]
                if has_conflict:
                    raise HTTPException(
                        status_code=409,
                        detail="The swap conflicts with another room, instructor, or student group.",
                    )

            first_slot = connection.execute(
                """
                SELECT day_of_week, start_time, end_time
                FROM time_slots WHERE id = %s
                """,
                (proposed[first["id"]]["time_slot_id"],),
            ).fetchone()
            second_slot = connection.execute(
                """
                SELECT day_of_week, start_time, end_time
                FROM time_slots WHERE id = %s
                """,
                (proposed[second["id"]]["time_slot_id"],),
            ).fetchone()
            overlap = (
                first_slot[0] == second_slot[0]
                and first_slot[1] < second_slot[2]
                and second_slot[1] < first_slot[2]
            )
            shared_group = bool(
                group_keys[first["id"]] & group_keys[second["id"]]
            )
            same_resource = (
                (
                    proposed[first["id"]]["room_id"] is not None
                    and proposed[first["id"]]["room_id"]
                    == proposed[second["id"]]["room_id"]
                )
                or first["instructor_id"] == second["instructor_id"]
                or shared_group
            )
            if overlap and same_resource:
                raise HTTPException(
                    status_code=409,
                    detail="The swapped sessions would conflict with each other.",
                )

            connection.execute(
                "DELETE FROM session_groups WHERE session_id = ANY(%s)",
                (session_ids,),
            )
            cursor = connection.cursor()
            if update.swap_time:
                temporary_slots: dict[int, int] = {}
                for session_id in session_ids:
                    session = sessions[session_id]
                    excluded_temporary_slots = {
                        temporary_slot
                        for other_id, temporary_slot in temporary_slots.items()
                        if (
                            sessions[other_id]["academic_term_id"]
                            == session["academic_term_id"]
                            and sessions[other_id]["instructor_id"]
                            == session["instructor_id"]
                        )
                    }
                    temporary_slot = connection.execute(
                        """
                        SELECT slot.id
                        FROM time_slots slot
                        WHERE NOT (slot.id = ANY(%s))
                          AND NOT EXISTS (
                              SELECT 1
                              FROM timetable_sessions occupied
                              WHERE occupied.id <> ALL(%s)
                                AND occupied.academic_term_id = %s
                                AND occupied.instructor_id = %s
                                AND occupied.time_slot_id = slot.id
                          )
                        ORDER BY slot.id
                        LIMIT 1
                        """,
                        (
                            list(excluded_temporary_slots),
                            session_ids,
                            session["academic_term_id"],
                            session["instructor_id"],
                        ),
                    ).fetchone()
                    if temporary_slot is None:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "There is no free temporary period to safely swap these "
                                "sessions. Move one session to a free period first."
                            ),
                        )
                    temporary_slots[session_id] = temporary_slot[0]

                for session_id in session_ids:
                    cursor.execute(
                        "UPDATE timetable_sessions SET time_slot_id = %s WHERE id = %s",
                        (temporary_slots[session_id], session_id),
                    )

            for session_id in session_ids:
                assignment = proposed[session_id]
                cursor.execute(
                    """
                    UPDATE timetable_sessions
                    SET room_id = %s, time_slot_id = %s
                    WHERE id = %s
                    """,
                    (
                        assignment["room_id"],
                        assignment["time_slot_id"] if update.swap_time else sessions[session_id]["time_slot_id"],
                        session_id,
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO session_groups (session_id, time_slot_id, group_id)
                    VALUES (%s, %s, %s)
                    """,
                    [
                        (session_id, assignment["time_slot_id"], group_id)
                        for group_id in group_ids[session_id]
                    ],
                )

            updated_sessions = [
                item
                for item in load_saved_schedule_sessions(connection)
                if item["session_id"] in session_ids
            ]
    except psycopg.Error as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not swap the scheduled sessions: "
                f"{error.diag.message_primary or 'database error'}"
            ),
        ) from error
    return {"sessions": updated_sessions}


@app.post("/api/generate")
def generate():
    from timetable_scheduler import (
        Room,
        ScheduleInfeasibleError,
        ScheduleTimeoutError,
        Session,
        TimeSlot,
        generate_schedule,
    )

    try:
        with connect() as connection:
            offerings = connection.execute(
                """
                SELECT co.id, co.instructor_id,
                       COALESCE(i.friday_available, FALSE),
                       co.weekly_lectures, co.weekly_sections, co.weekly_labs,
                       co.course_id, co.academic_term_id, co.lecture_online
                FROM course_offerings co
                LEFT JOIN instructors i ON i.id = co.instructor_id
                ORDER BY co.id
                """,
            ).fetchall()
            groups_by_course_term: dict[tuple[int, int], list[tuple[int, int]]] = {}
            group_rows = connection.execute(
                """
                SELECT sg.course_id, sg.academic_term_id, sg.id,
                       sg.student_count, sg.group_number
                FROM student_groups sg
                ORDER BY sg.academic_term_id, sg.group_number, sg.id
                """,
            ).fetchall()
            shared_group_ids_by_group_id = build_schedule_group_ids(
                [
                    (group_id, academic_term_id, group_number)
                    for _, academic_term_id, group_id, _, group_number
                    in group_rows
                ]
            )
            for course_id, academic_term_id, group_id, student_count, _ in group_rows:
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
            database_group_ids_by_session: dict[int, tuple[int, ...]] = {}
            session_numbers: dict[tuple[int, str], int] = {}
            next_session_id = 1
            for offering_id, instructor_id, friday_available, weekly_lectures, weekly_sections, weekly_labs, course_id, academic_term_id, lecture_online in offerings:
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
                    database_group_ids_by_session[next_session_id] = group_ids
                    session_key = (offering_id, activity_type)
                    session_numbers[session_key] = (
                        session_numbers.get(session_key, 0) + 1
                    )
                    sessions.append(
                        Session(
                            next_session_id,
                            instructor_id,
                            tuple(
                                shared_group_ids_by_group_id[group_id]
                                for group_id in group_ids
                            ),
                            student_count,
                            activity_type,
                            friday_available,
                            online=activity_type == "lecture" and lecture_online,
                            course_id=course_id,
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

            sessions_by_id = {
                session.session_id: session for session in sessions
            }
            session_rows = []
            for assignment in assignments:
                (
                    offering_id,
                    academic_term_id,
                    session_number,
                    activity_type,
                ) = metadata[assignment.session_id]
                session = sessions_by_id[assignment.session_id]
                session_rows.append(
                    (
                        offering_id,
                        academic_term_id,
                        session.instructor_id,
                        assignment.room_id,
                        assignment.time_slot_id,
                        activity_type.upper(),
                        session_number,
                        session.online,
                        TIMETABLE_RULES_VERSION,
                    )
                )

            connection.execute("DELETE FROM timetable_sessions")
            if session_rows:
                row_values = sql.SQL(", ").join(
                    sql.SQL("({})").format(
                        sql.SQL(", ").join(sql.Placeholder() for _ in row)
                    )
                    for row in session_rows
                )
                inserted_sessions = connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO timetable_sessions
                            (offering_id, academic_term_id, instructor_id, room_id,
                             time_slot_id, activity_type, session_number, is_online,
                             schedule_version)
                        VALUES {}
                        RETURNING id, offering_id, activity_type, session_number
                        """
                    ).format(row_values),
                    tuple(value for row in session_rows for value in row),
                ).fetchall()
                session_ids = {
                    (offering_id, activity_type, session_number): session_id
                    for session_id, offering_id, activity_type, session_number
                    in inserted_sessions
                }
                group_rows = []
                for assignment in assignments:
                    offering_id, _, session_number, activity_type = metadata[
                        assignment.session_id
                    ]
                    session_id = session_ids[
                        (offering_id, activity_type.upper(), session_number)
                    ]
                    group_rows.extend(
                        (
                            session_id,
                            assignment.time_slot_id,
                            group_id,
                        )
                        for group_id in database_group_ids_by_session[
                            assignment.session_id
                        ]
                    )
                if group_rows:
                    connection.execute(
                        """
                        INSERT INTO session_groups
                            (session_id, time_slot_id, group_id)
                        SELECT group_data.session_id,
                               group_data.time_slot_id,
                               group_data.group_id
                        FROM unnest(%s::bigint[], %s::bigint[], %s::bigint[])
                            AS group_data(session_id, time_slot_id, group_id)
                        """,
                        (
                            [row[0] for row in group_rows],
                            [row[1] for row in group_rows],
                            [row[2] for row in group_rows],
                        ),
                    )
            schedule_sessions = load_saved_schedule_sessions(connection)
    except ScheduleTimeoutError as error:
        raise HTTPException(status_code=504, detail=str(error)) from error
    except ScheduleInfeasibleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except psycopg.errors.UndefinedColumn as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "The database schema is outdated. Run 02_timetable_schema.sql "
                "against this database, then try again."
            ),
        ) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Database operation failed.") from error
    return {
        "scheduled_sessions": len(schedule_sessions),
        "sessions": schedule_sessions,
        "is_current": True,
    }