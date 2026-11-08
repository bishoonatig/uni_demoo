import unittest

from app import (
    build_course_activity_specs,
    build_daily_periods,
    build_group_sizes,
    ensure_schedule_load_fits_slots,
    FRIDAY,
    group_label,
    TEACHING_DAYS,
    normalize_teaching_types,
)
from timetable_scheduler import (
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


class CohortGroupGenerationTests(unittest.TestCase):
    def test_instructor_must_have_at_least_one_teaching_type(self):
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