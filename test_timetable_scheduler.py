import unittest
from unittest.mock import Mock

from app import (
    build_course_activity_specs,
    build_daily_periods,
    build_group_sizes,
    build_schedule_group_ids,
    ensure_schedule_load_fits_slots,
    FRIDAY,
    group_label,
    prepare_course_offering,
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
            Session(3, 10, (100,), 20),
        ]
        slots = [*self.slots, TimeSlot(3, 0, 660, 720)]

        assignments = generate_schedule(sessions, self.rooms, slots)

        self.assertEqual(len(assignments), 3)
        self.assertEqual(len({item.time_slot_id for item in assignments}), 3)

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
                [
                    Session(session_id, 10, (100,), 20)
                    for session_id in range(1, 4)
                ],
                self.rooms,
                [self.slots[0]],
            )

    def test_online_session_cannot_overlap_another_class_for_the_same_group(self):
        sessions = [
            Session(1, 10, (100,), 20, "lecture", online=True, course_id=1),
            Session(2, 11, (100,), 20, "lecture", course_id=2),
            Session(3, 12, (100,), 20, "lecture", course_id=3),
        ]

        with self.assertRaises(ScheduleInfeasibleError):
            generate_schedule(sessions, self.rooms, [self.slots[0]])

    def test_online_session_cannot_overlap_another_class_for_its_instructor(self):
        sessions = [
            Session(1, 10, (100,), 20, "lecture", online=True, course_id=1),
            Session(2, 11, (100,), 20, "lecture", course_id=2),
            Session(3, 12, (100,), 20, "lecture", course_id=3),
            Session(4, 10, (101,), 20, "lecture", online=True, course_id=4),
            Session(5, 13, (101,), 20, "lecture", course_id=5),
            Session(6, 14, (101,), 20, "lecture", course_id=6),
        ]
        slots = [
            TimeSlot(index, 1, 540 + (index - 1) * 100, 640 + (index - 1) * 100)
            for index in range(1, 4)
        ]

        assignments = generate_schedule(
            sessions,
            [Room(room_id, 30) for room_id in range(1, 5)],
            slots,
        )
        online_slots = {
            assignment.time_slot_id
            for assignment in assignments
            if assignment.session_id in {1, 4}
        }

        self.assertEqual(len(online_slots), 2)

    def test_rejects_overlapping_sessions_that_need_the_same_room(self):
        sessions = [
            *[
                Session(session_id, session_id, (100,), 20)
                for session_id in range(1, 4)
            ],
            *[
                Session(session_id, session_id, (101,), 20)
                for session_id in range(4, 7)
            ],
        ]

        with self.assertRaises(ScheduleInfeasibleError):
            generate_schedule(
                sessions,
                [Room(1, 30)],
                [
                    *self.slots,
                    TimeSlot(3, 0, 660, 720),
                ],
            )

    def test_rejects_student_groups_with_fewer_than_three_weekly_courses(self):
        with self.assertRaisesRegex(
            ScheduleInfeasibleError,
            r"fewer than 3 courses per week.*100 \(2 courses\)",
        ):
            generate_schedule(
                [
                    Session(1, 10, (100,), 20),
                    Session(2, 11, (100,), 20),
                ],
                self.rooms,
                self.slots,
            )

    def test_repeated_sessions_of_one_course_count_as_one_daily_course(self):
        sessions = [
            Session(1, 10, (100,), 20, course_id=1),
            Session(2, 11, (100,), 20, course_id=1),
            Session(3, 12, (100,), 20, course_id=2),
            Session(4, 13, (100,), 20, course_id=3),
        ]
        slots = [
            TimeSlot(index, 1, 540 + (index - 1) * 120, 640 + (index - 1) * 120)
            for index in range(4)
        ]

        assignments = generate_schedule(
            sessions,
            [Room(room_id, 30) for room_id in range(1, 5)],
            slots,
        )

        self.assertEqual(len(assignments), 4)
        assigned_courses = {
            next(
                session.course_id
                for session in sessions
                if session.session_id == assignment.session_id
            )
            for assignment in assignments
        }
        self.assertEqual(len(assigned_courses), 3)

    def test_student_has_no_more_than_four_sessions_per_day(self):
        sessions = [
            Session(session_id, session_id, (100,), 20)
            for session_id in range(1, 7)
        ]
        slots = [
            TimeSlot(index, 1, 540 + index * 100, 640 + index * 100)
            for index in range(6)
        ]
        rooms = [Room(room_id, 30) for room_id in range(1, 7)]

        with self.assertRaises(ScheduleInfeasibleError):
            generate_schedule(sessions, rooms, slots)

    def test_student_has_no_gaps_and_at_least_two_days_off(self):
        sessions = [
            Session(session_id, session_id, (100,), 20)
            for session_id in range(1, 7)
        ]
        slots = [
            TimeSlot(index, 1, 540 + (index - 1) * 100, 640 + (index - 1) * 100)
            for index in range(1, 5)
        ] + [
            TimeSlot(index + 4, 2, 540 + index * 100, 640 + index * 100)
            for index in range(4)
        ]
        assignments = generate_schedule(
            sessions,
            [Room(room_id, 30) for room_id in range(1, 7)],
            slots,
        )
        slots_by_id = {slot.time_slot_id: slot for slot in slots}
        sessions_by_day = {}
        for assignment in assignments:
            slot = slots_by_id[assignment.time_slot_id]
            sessions_by_day.setdefault(slot.day_of_week, []).append(slot)

        self.assertEqual(len(sessions_by_day), 2)
        self.assertGreaterEqual(7 - len(sessions_by_day), 2)
        for daily_slots in sessions_by_day.values():
            ordered = sorted(daily_slots, key=lambda slot: slot.start_minute)
            self.assertGreaterEqual(len(ordered), 3)
            self.assertLessEqual(len(ordered), 4)
            self.assertEqual(
                [slot.time_slot_id for slot in ordered],
                list(
                    range(
                        ordered[0].time_slot_id,
                        ordered[0].time_slot_id + len(ordered),
                    )
                ),
            )

    def test_instructor_has_no_more_than_four_sessions_per_day(self):
        sessions = [
            Session(session_id, 10, (group_id,), 20)
            for group_id in range(100, 105)
            for session_id in range(group_id * 10, group_id * 10 + 3)
        ]
        slots = [
            TimeSlot(index, 1, 540 + index * 100, 640 + index * 100)
            for index in range(6)
        ]
        rooms = [Room(room_id, 30) for room_id in range(1, 7)]

        with self.assertRaises(ScheduleInfeasibleError):
            generate_schedule(sessions, rooms, slots)

    def test_schedules_many_independent_sessions_without_slow_solver_search(self):
        sessions = [
            Session(index, index, ((index - 1) // 3 + 1,), 20, "lecture")
            for index in range(1, 25)
        ]
        rooms = [Room(room_id, 30) for room_id in range(1, 21)]
        slots = [
            TimeSlot(
                index,
                day,
                540 + period * 120,
                640 + period * 120,
            )
            for index, (day, period) in enumerate(
                ((day, period) for day in TEACHING_DAYS for period in range(6)),
                start=1,
            )
        ]

        assignments = generate_schedule(sessions, rooms, slots)

        self.assertEqual(len(assignments), len(sessions))

    def test_friday_is_available_only_to_seconded_instructors(self):
        friday_slot = TimeSlot(1, FRIDAY, 540, 640)
        session = Session(1, 10, (100,), 20, "lecture")
        room = [Room(1, 40, "LECTURE")]

        with self.assertRaisesRegex(ScheduleInfeasibleError, "seconded instructors"):
            generate_schedule([session], room, [friday_slot])

        assignments = generate_schedule(
            [
                Session(1, 10, (100,), 20, "lecture", friday_available=True),
                Session(2, 11, (100,), 20, "lecture", friday_available=True),
                Session(3, 12, (100,), 20, "lecture", friday_available=True),
            ],
            room,
            [
                friday_slot,
                TimeSlot(2, FRIDAY, 660, 760),
                TimeSlot(3, FRIDAY, 780, 880),
            ],
        )
        self.assertEqual(len(assignments), 3)

    def test_online_sessions_are_scheduled_without_rooms(self):
        sessions = [
            *[
                Session(session_id, session_id, (100,), 20, "lecture", online=True)
                for session_id in range(1, 4)
            ],
            *[
                Session(session_id, session_id, (101,), 20, "lecture", online=True)
                for session_id in range(4, 7)
            ],
        ]

        slots = [*self.slots, TimeSlot(3, 0, 660, 720)]
        assignments = generate_schedule(sessions, [], slots)

        self.assertEqual(len(assignments), 6)
        self.assertTrue(all(assignment.room_id is None for assignment in assignments))
        self.assertEqual(len({assignment.time_slot_id for assignment in assignments}), 3)

    def test_schedules_sessions_without_an_assigned_instructor(self):
        sessions = [
            Session(1, None, (100,), 20),
            Session(2, None, (100,), 20),
            Session(3, None, (100,), 20),
        ]

        assignments = generate_schedule(
            sessions, self.rooms, [*self.slots, TimeSlot(3, 0, 660, 720)]
        )

        self.assertEqual(len(assignments), 3)
        self.assertEqual(len({assignment.time_slot_id for assignment in assignments}), 3)

    def test_online_session_does_not_conflict_with_a_room_user(self):
        sessions = [
            *[
                Session(session_id, session_id + 10, (100,), 20, "lecture", online=True)
                for session_id in range(1, 4)
            ],
            *[
                Session(session_id, session_id + 10, (101,), 20, "lecture")
                for session_id in range(4, 7)
            ],
        ]

        assignments = generate_schedule(
            sessions,
            [Room(1, 30)],
            [
                TimeSlot(1, 1, 540, 640),
                TimeSlot(2, 1, 640, 740),
                TimeSlot(3, 1, 740, 840),
                TimeSlot(4, 2, 540, 640),
                TimeSlot(5, 2, 640, 740),
                TimeSlot(6, 2, 740, 840),
            ],
        )

        self.assertEqual(len(assignments), 6)
        self.assertEqual(
            {assignment.room_id for assignment in assignments},
            {None, 1},
        )


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

    def test_schedule_groups_share_identity_by_term_and_group_number(self):
        shared_ids = build_schedule_group_ids(
            [
                (10, 1, 1),
                (20, 1, 1),
                (30, 1, 2),
                (40, 2, 1),
            ]
        )

        self.assertEqual(shared_ids[10], shared_ids[20])
        self.assertNotEqual(shared_ids[10], shared_ids[30])
        self.assertNotEqual(shared_ids[10], shared_ids[40])

    def test_course_offering_can_be_prepared_without_an_instructor(self):
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(1,))),
            Mock(fetchone=Mock(return_value=(False,))),
        ]

        offering = prepare_course_offering(
            connection,
            {
                "course_id": 1,
                "academic_term_id": 2,
                "instructor_id": None,
                "total_students": 21,
                "teaching_types": ["lecture"],
                "lecture_online": False,
            },
        )

        self.assertEqual(offering, (["lecture"], 21, 1, 0, 0))
        self.assertEqual(connection.execute.call_count, 2)

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