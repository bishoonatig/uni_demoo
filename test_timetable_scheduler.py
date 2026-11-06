import unittest

from fastapi import HTTPException

from app import (
    add_missing_groups,
    build_course_activity_specs,
    build_daily_periods,
    build_group_sizes,
    delete_entity,
    ensure_instructor_part_time_column,
    ensure_schedule_load_fits_slots,
    ENTITIES,
    FRIDAY,
    group_label,
    load_saved_schedule_sessions,
    prepare_course_offering,
    TEACHING_DAYS,
    normalize_teaching_types,
    sync_course_groups,
)
from timetable_scheduler import (
    ReservedAssignment,
    Room,
    ScheduleInfeasibleError,
    Session,
    TimeSlot,
    generate_schedule,
)


class GenerateScheduleTests(unittest.TestCase):
    def setUp(self):
        self.rooms = [Room(1, 30), Room(2, 15)]
        self.slots = [TimeSlot(1, 0, 540, 600), TimeSlot(2, 0, 600, 660)]

    def test_assigns_distinct_slots_for_shared_instructor_and_group(self):
        sessions = [
            Session(1, 10, (100,), 20),
            Session(2, 10, (100,), 20),
        ]

        assignments = generate_schedule(sessions, self.rooms, self.slots)

        self.assertEqual(len(assignments), 2)
        self.assertEqual(len({item.time_slot_id for item in assignments}), 2)

    def test_respects_instructor_and_room_reservations_from_other_terms(self):
        sessions = [
            Session(1, 10, (), 20),
            Session(2, 11, (), 10),
        ]
        reserved = [
            ReservedAssignment(10, 20, self.slots[0].time_slot_id),
            ReservedAssignment(12, 2, self.slots[1].time_slot_id),
        ]

        assignments = generate_schedule(sessions, self.rooms, self.slots, reserved)
        assignments_by_session = {item.session_id: item for item in assignments}

        self.assertNotEqual(
            assignments_by_session[1].time_slot_id, self.slots[0].time_slot_id
        )
        self.assertFalse(
            assignments_by_session[2].room_id == 2
            and assignments_by_session[2].time_slot_id == self.slots[1].time_slot_id
        )

    def test_schedules_all_terms_together_for_shared_instructors(self):
        sessions = [
            Session(1, 10, (100,), 20),
            Session(2, 10, (200,), 20),
        ]

        assignments = generate_schedule(sessions, self.rooms, self.slots)

        self.assertEqual(len(assignments), 2)
        self.assertEqual(len({item.time_slot_id for item in assignments}), 2)

    def test_rejects_sessions_when_all_instructor_slots_are_reserved(self):
        reserved = [
            ReservedAssignment(10, 20, slot.time_slot_id)
            for slot in self.slots
        ]

        with self.assertRaises(ScheduleInfeasibleError):
            generate_schedule(
                [Session(1, 10, (), 20)], self.rooms, self.slots, reserved
            )

    def test_rejects_session_with_no_room_large_enough(self):
        with self.assertRaisesRegex(
            ScheduleInfeasibleError,
            r"31 students.*capacity >= 31.*capacity 30",
        ):
            generate_schedule(
                [Session(1, 10, (100,), 31)], self.rooms, self.slots
            )

    def test_rejects_overlapping_sessions_when_only_one_slot_exists(self):
        with self.assertRaises(ScheduleInfeasibleError):
            generate_schedule(
                [Session(1, 10, (100,), 20), Session(2, 10, (100,), 20)],
                self.rooms,
                [self.slots[0]],
            )

    def test_friday_is_available_only_to_seconded_instructors(self):
        friday_slot = TimeSlot(1, FRIDAY, 540, 640)
        session = Session(1, 10, (100, 101), 40, "lecture")
        room = [Room(1, 40, "LECTURE")]

        with self.assertRaisesRegex(ScheduleInfeasibleError, "seconded instructors"):
            generate_schedule([session], room, [friday_slot])

        assignments = generate_schedule(
            [Session(1, 10, (100, 101), 40, "lecture", friday_available=True)],
            room,
            [friday_slot],
        )
        self.assertEqual(len(assignments), 1)


class CourseGroupGenerationTests(unittest.TestCase):
    def test_saved_schedule_loader_keeps_sessions_from_every_term(self):
        class FakeCursor:
            def fetchall(self):
                return [
                    (1, 21, 4, "LECTURE", 1, 7, 12, [100]),
                    (2, 22, 6, "SECTION", 1, 8, 15, [200]),
                ]

        class FakeConnection:
            def execute(self, query):
                self.query = query
                return FakeCursor()

        connection = FakeConnection()

        sessions = load_saved_schedule_sessions(connection)

        self.assertIn("FROM timetable_sessions ts", connection.query)
        self.assertNotIn("WHERE ts.academic_term_id", connection.query)
        self.assertEqual([session["academic_term_id"] for session in sessions], [4, 6])
        self.assertEqual([session["activity_type"] for session in sessions], ["lecture", "section"])

    def test_course_offering_values_derive_weekly_session_counts(self):
        class FakeCursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, query, params):
                if "FROM courses" in query:
                    return FakeCursor((1,))
                if "SELECT EXISTS" in query:
                    return FakeCursor((False,))
                if "FROM instructors" in query:
                    return FakeCursor((13,))
                raise AssertionError(f"Unexpected query: {query}")

        values = {
            "course_id": 1,
            "academic_term_id": 7,
            "instructor_id": 13,
            "total_students": 81,
            "teaching_types": ["lecture", "section", "lab"],
        }

        prepared = prepare_course_offering(FakeConnection(), values)

        self.assertEqual(prepared, (["lecture", "section", "lab"], 81, 3, 5, 5))

    def test_rejects_different_enrollment_for_same_course_and_term(self):
        class FakeCursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, query, params):
                if "FROM courses" in query:
                    return FakeCursor((1,))
                if "SELECT EXISTS" in query:
                    return FakeCursor((True,))
                raise AssertionError(f"Unexpected query: {query}")

        values = {
            "course_id": 1,
            "academic_term_id": 7,
            "instructor_id": 13,
            "total_students": 81,
            "teaching_types": ["lecture"],
        }

        with self.assertRaises(HTTPException) as context:
            prepare_course_offering(FakeConnection(), values)

        self.assertEqual(context.exception.status_code, 409)

    def test_groups_are_generated_once_per_course_and_term(self):
        class FakeCursor:
            def __init__(self, row=None, rows=None):
                self.row = row
                self.rows = rows or []

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class FakeConnection:
            def __init__(self):
                self.insertions = []

            def execute(self, query, params):
                if "FROM course_offerings co" in query:
                    return FakeCursor((3, 7, 45))
                if "FROM student_groups" in query:
                    return FakeCursor(rows=[])
                if "INSERT INTO student_groups" in query:
                    self.insertions.append(params)
                    return FakeCursor((len(self.insertions),))
                raise AssertionError(f"Unexpected query: {query}")

        connection = FakeConnection()

        result = add_missing_groups(connection, 11)

        self.assertEqual([group["student_count"] for group in result["groups"]], [20, 20, 5])
        self.assertEqual(result["group_count"], 3)
        self.assertEqual(
            connection.insertions,
            [(3, 7, 1, 20), (3, 7, 2, 20), (3, 7, 3, 5)],
        )

    def test_syncing_second_offering_reuses_existing_course_term_groups(self):
        class FakeCursor:
            def __init__(self, row=None, rows=None):
                self.row = row
                self.rows = rows or []

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class FakeConnection:
            def __init__(self):
                self.inserts = 0

            def execute(self, query, params):
                if "FROM course_offerings co" in query:
                    return FakeCursor((3, 7, 45))
                if "MIN(total_students)" in query:
                    return FakeCursor((45, 45))
                if "FROM student_groups" in query:
                    return FakeCursor(rows=[(20,), (20,), (5,)])
                if "INSERT INTO student_groups" in query:
                    self.inserts += 1
                    raise AssertionError("Groups should be shared, not duplicated.")
                raise AssertionError(f"Unexpected query: {query}")

        connection = FakeConnection()

        result = sync_course_groups(connection, 12)

        self.assertEqual(result, {"groups": [], "group_count": 3})
        self.assertEqual(connection.inserts, 0)

    def test_cohorts_are_removed_from_supported_entities(self):
        self.assertNotIn("cohorts", ENTITIES)
        with self.assertRaises(HTTPException) as context:
            delete_entity("cohorts", 12)
        self.assertEqual(context.exception.status_code, 404)

    def test_delete_rejects_non_deletable_data_types(self):
        with self.assertRaises(HTTPException) as context:
            delete_entity("student_groups", 12)

        self.assertEqual(context.exception.status_code, 404)

    def test_part_time_schema_migration_is_additive_and_idempotent(self):
        class RecordingConnection:
            def __init__(self):
                self.statements = []

            def execute(self, statement):
                self.statements.append(statement)

        connection = RecordingConnection()

        ensure_instructor_part_time_column(connection)

        self.assertEqual(len(connection.statements), 1)
        self.assertIn("ADD COLUMN IF NOT EXISTS part_time", connection.statements[0])
        self.assertIn("DEFAULT FALSE", connection.statements[0])

    def test_instructor_attributes_do_not_include_teaching_types(self):
        instructor_fields = set(ENTITIES["instructors"][2])

        self.assertIn("part_time", instructor_fields)
        self.assertIn("friday_available", instructor_fields)
        self.assertNotIn("teaching_types", instructor_fields)

    def test_course_activity_teaching_types_are_validated(self):
        with self.assertRaisesRegex(Exception, "Select at least one teaching type"):
            normalize_teaching_types([])

        self.assertEqual(
            normalize_teaching_types(["lecture", "lab"]), ["lecture", "lab"]
        )

    def test_splits_students_into_groups_of_at_most_twenty(self):
        self.assertEqual(build_group_sizes(45), [20, 20, 5])

    def test_group_labels_continue_after_z(self):
        self.assertEqual(
            [group_label(number) for number in (1, 2, 26, 27, 28)],
            ["a", "b", "z", "aa", "ab"],
        )

    def test_course_activity_counts_derive_from_groups(self):
        groups = [(1, 20), (2, 20), (3, 20), (4, 20)]

        without_lab = build_course_activity_specs(groups, ["lecture", "section"])
        with_lab = build_course_activity_specs(groups, ["lecture", "section", "lab"])

        self.assertEqual(len(without_lab), 6)
        self.assertEqual(without_lab[:2], [("lecture", (1, 2), 40), ("lecture", (3, 4), 40)])
        self.assertEqual(sum(activity == "section" for activity, _, _ in without_lab), 4)
        self.assertEqual(len(with_lab), 10)
        self.assertEqual(sum(activity == "lab" for activity, _, _ in with_lab), 4)

    def test_daily_window_expands_to_six_periods_with_breaks(self):
        periods = build_daily_periods("08:30", "20:10")

        self.assertEqual(
            [(start.strftime("%H:%M"), end.strftime("%H:%M")) for start, end in periods],
            [
                ("08:30", "10:10"),
                ("10:30", "12:10"),
                ("12:30", "14:10"),
                ("14:30", "16:10"),
                ("16:30", "18:10"),
                ("18:30", "20:10"),
            ],
        )

    def test_weekly_schedule_has_six_periods_for_each_day(self):
        self.assertEqual(TEACHING_DAYS, (1, 2, 3, 4, 5, 6, 7))
        self.assertEqual(
            len(TEACHING_DAYS) * len(build_daily_periods("08:30", "20:10")),
            42,
        )

    def test_rejects_group_load_greater_than_available_slots(self):
        sessions = [Session(index, 1, (10,), 20) for index in range(1, 38)]
        weekday_slots = [
            TimeSlot(index, day, 540 + period * 100, 640 + period * 100)
            for index, (day, period) in enumerate(
                ((day, period) for day in (1, 2, 3, 4, 6, 7) for period in range(6)),
                start=1,
            )
        ]

        with self.assertRaisesRegex(Exception, "requires 37 sessions per week.*36 eligible periods"):
            ensure_schedule_load_fits_slots(sessions, weekday_slots)


if __name__ == "__main__":
    unittest.main()