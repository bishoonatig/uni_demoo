from dataclasses import dataclass

from ortools.sat.python import cp_model


@dataclass(frozen=True)
class Room:
    room_id: int
    capacity: int
    room_type: str | None = None


@dataclass(frozen=True)
class TimeSlot:
    time_slot_id: int
    day_of_week: int
    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class Session:
    session_id: int
    instructor_id: int | None
    group_ids: tuple[int, ...]
    student_count: int
    activity_type: str | None = None
    friday_available: bool = False
    online: bool = False
    course_id: int | None = None


@dataclass(frozen=True)
class Assignment:
    session_id: int
    room_id: int | None
    time_slot_id: int


class ScheduleInfeasibleError(ValueError):
    pass


class ScheduleTimeoutError(TimeoutError):
    pass


MAX_DAILY_SESSIONS = 4
MIN_DAILY_COURSES = 3
MAX_ATTENDANCE_DAYS = 5
MAX_SOLVE_SECONDS = 10.0


def generate_schedule(
    sessions: list[Session], rooms: list[Room], time_slots: list[TimeSlot]
) -> list[Assignment]:
    """Assign each session a compatible room and time without resource conflicts."""
    if not sessions:
        return []

    slots_by_session: dict[int, list[TimeSlot]] = {}
    rooms_by_session: dict[int, list[Room]] = {}
    for session in sessions:
        eligible_slots = [
            slot
            for slot in time_slots
            if slot.day_of_week != 5 or session.friday_available
        ]
        if not eligible_slots:
            raise ScheduleInfeasibleError(
                f"Session {session.session_id} has no eligible teaching slots; "
                "Friday is only available for seconded instructors."
            )
        slots_by_session[session.session_id] = eligible_slots
        if session.online:
            rooms_by_session[session.session_id] = []
            continue
        compatible_rooms = [
            room
            for room in rooms
            if room.capacity >= session.student_count
            and (
                room.room_type is None
                or session.activity_type is None
                or room.room_type.casefold() == session.activity_type.casefold()
            )
        ]
        if not compatible_rooms:
            type_compatible_rooms = [
                room
                for room in rooms
                if room.room_type is None
                or session.activity_type is None
                or room.room_type.casefold() == session.activity_type.casefold()
            ]
            if type_compatible_rooms:
                largest_capacity = max(room.capacity for room in type_compatible_rooms)
                raise ScheduleInfeasibleError(
                    f"Session {session.session_id} ({session.activity_type or 'activity'}, "
                    f"{session.student_count} students) needs a compatible room with "
                    f"capacity >= {session.student_count}; the largest compatible room "
                    f"has capacity {largest_capacity}."
                )
            raise ScheduleInfeasibleError(
                f"Session {session.session_id} ({session.activity_type or 'activity'}) "
                "has no room of a compatible type."
            )
        rooms_by_session[session.session_id] = compatible_rooms

    weekly_group_courses: dict[int, set[int]] = {}
    for session in sessions:
        course_id = (
            session.course_id
            if session.course_id is not None
            else session.session_id
        )
        for group_id in set(session.group_ids):
            weekly_group_courses.setdefault(group_id, set()).add(course_id)
    groups_with_too_few_courses = [
        group_id
        for group_id, course_ids in weekly_group_courses.items()
        if len(course_ids) < MIN_DAILY_COURSES
    ]
    if groups_with_too_few_courses:
        group_loads = ", ".join(
            f"{group_id} ({len(weekly_group_courses[group_id])} courses)"
            for group_id in groups_with_too_few_courses[:10]
        )
        raise ScheduleInfeasibleError(
            "Each student group must have at least 3 different courses on every "
            "day it attends, with no gaps and at least two days off per week. "
            "These groups have fewer than 3 courses per week: "
            f"{group_loads}. Add more course offerings for those groups."
        )

    model = cp_model.CpModel()
    resource_intervals: dict[tuple[str, int], list[cp_model.IntervalVar]] = {}
    room_intervals: dict[int, list[cp_model.IntervalVar]] = {}
    slot_choices: dict[int, list[TimeSlot]] = {}
    slot_variables: dict[int, dict[int, cp_model.IntVar]] = {}
    room_choices: dict[int, dict[int, cp_model.IntVar]] = {}
    group_slot_variables: dict[tuple[int, int], list[cp_model.IntVar]] = {}
    group_course_slot_variables: dict[
        tuple[int, int, int], list[cp_model.IntVar]
    ] = {}
    instructor_day_variables: dict[tuple[int, int], list[cp_model.IntVar]] = {}

    for session in sessions:
        eligible_slots = slots_by_session[session.session_id]
        slot_choices[session.session_id] = eligible_slots
        slot_variables[session.session_id] = {}
        slot_variables_list = []
        for slot in eligible_slots:
            selected = model.NewBoolVar(
                f"session_{session.session_id}_slot_{slot.time_slot_id}"
            )
            slot_variables[session.session_id][slot.time_slot_id] = selected
            slot_variables_list.append(selected)
            if session.instructor_id is not None:
                instructor_day_variables.setdefault(
                    (session.instructor_id, slot.day_of_week), []
                ).append(selected)
            for group_id in set(session.group_ids):
                group_slot_variables.setdefault(
                    (group_id, slot.time_slot_id), []
                ).append(selected)
                course_id = (
                    session.course_id
                    if session.course_id is not None
                    else session.session_id
                )
                group_course_slot_variables.setdefault(
                    (group_id, course_id, slot.time_slot_id), []
                ).append(selected)
        model.AddExactlyOne(slot_variables_list)

        starts = [
            slot.day_of_week * 1440 + slot.start_minute for slot in eligible_slots
        ]
        ends = [
            slot.day_of_week * 1440 + slot.end_minute for slot in eligible_slots
        ]
        durations = [slot.end_minute - slot.start_minute for slot in eligible_slots]
        start = model.NewIntVar(
            min(starts), max(starts), f"session_{session.session_id}_start"
        )
        end = model.NewIntVar(
            min(ends), max(ends), f"session_{session.session_id}_end"
        )
        duration = model.NewIntVar(
            min(durations), max(durations), f"session_{session.session_id}_duration"
        )
        model.Add(
            start
            == sum(
                (slot.day_of_week * 1440 + slot.start_minute)
                * slot_variables[session.session_id][slot.time_slot_id]
                for slot in eligible_slots
            )
        )
        model.Add(
            end
            == sum(
                (slot.day_of_week * 1440 + slot.end_minute)
                * slot_variables[session.session_id][slot.time_slot_id]
                for slot in eligible_slots
            )
        )
        model.Add(
            duration
            == sum(
                (slot.end_minute - slot.start_minute)
                * slot_variables[session.session_id][slot.time_slot_id]
                for slot in eligible_slots
            )
        )
        interval = model.NewIntervalVar(
            start, duration, end, f"session_{session.session_id}_interval"
        )
        if session.instructor_id is not None:
            resource_intervals.setdefault(
                ("instructor", session.instructor_id), []
            ).append(interval)
        for group_id in set(session.group_ids):
            resource_intervals.setdefault(("group", group_id), []).append(interval)

        if not session.online:
            room_choices[session.session_id] = {}
            for room in rooms_by_session[session.session_id]:
                selected = model.NewBoolVar(
                    f"session_{session.session_id}_room_{room.room_id}"
                )
                room_choices[session.session_id][room.room_id] = selected
                optional_interval = model.NewOptionalIntervalVar(
                    start,
                    duration,
                    end,
                    selected,
                    f"session_{session.session_id}_room_{room.room_id}_interval",
                )
                room_intervals.setdefault(room.room_id, []).append(optional_interval)
            model.AddExactlyOne(list(room_choices[session.session_id].values()))
        else:
            room_choices[session.session_id] = {}

    for selected_sessions in instructor_day_variables.values():
        model.Add(sum(selected_sessions) <= MAX_DAILY_SESSIONS)

    for intervals in (*resource_intervals.values(), *room_intervals.values()):
        if len(intervals) > 1:
            model.AddNoOverlap(intervals)

    groups = {group_id for session in sessions for group_id in session.group_ids}
    slots_by_day: dict[int, list[TimeSlot]] = {}
    for slot in time_slots:
        slots_by_day.setdefault(slot.day_of_week, []).append(slot)
    for day_slots in slots_by_day.values():
        day_slots.sort(key=lambda slot: (slot.start_minute, slot.end_minute))

    for group_id in groups:
        group_attendance_days = []
        for day, day_slots in slots_by_day.items():
            active_by_slot: list[cp_model.IntVar] = []
            daily_session_variables = [
                slot_variables[session.session_id][slot.time_slot_id]
                for session in sessions
                if group_id in session.group_ids
                for slot in slots_by_session[session.session_id]
                if slot.day_of_week == day
            ]
            if not daily_session_variables:
                continue
            for slot in day_slots:
                possible_sessions = group_slot_variables.get(
                    (group_id, slot.time_slot_id), []
                )
                active = model.NewBoolVar(
                    f"group_{group_id}_day_{day}_slot_{slot.time_slot_id}"
                )
                if possible_sessions:
                    model.Add(sum(possible_sessions) >= 1).OnlyEnforceIf(active)
                    model.Add(sum(possible_sessions) == 0).OnlyEnforceIf(
                        active.Not()
                    )
                else:
                    model.Add(active == 0)
                active_by_slot.append(active)

            present = model.NewBoolVar(f"group_{group_id}_day_{day}_present")
            model.Add(sum(daily_session_variables) >= 1).OnlyEnforceIf(present)
            model.Add(sum(daily_session_variables) == 0).OnlyEnforceIf(
                present.Not()
            )
            model.Add(sum(daily_session_variables) <= MAX_DAILY_SESSIONS)
            group_attendance_days.append(present)

            daily_course_variables = []
            course_ids = {
                session.course_id
                if session.course_id is not None
                else session.session_id
                for session in sessions
                if group_id in session.group_ids
            }
            for course_id in course_ids:
                course_session_variables = [
                    selected
                    for slot in day_slots
                    for selected in group_course_slot_variables.get(
                        (group_id, course_id, slot.time_slot_id), []
                    )
                ]
                if not course_session_variables:
                    continue
                course_present = model.NewBoolVar(
                    f"group_{group_id}_course_{course_id}_day_{day}_present"
                )
                model.Add(sum(course_session_variables) >= 1).OnlyEnforceIf(
                    course_present
                )
                model.Add(sum(course_session_variables) == 0).OnlyEnforceIf(
                    course_present.Not()
                )
                daily_course_variables.append(course_present)
            model.Add(
                sum(daily_course_variables) >= MIN_DAILY_COURSES
            ).OnlyEnforceIf(present)

            for first in range(len(active_by_slot)):
                for last in range(first + 2, len(active_by_slot)):
                    for missing in range(first + 1, last):
                        model.Add(active_by_slot[missing] == 1).OnlyEnforceIf(
                            [active_by_slot[first], active_by_slot[last]]
                        )

        if group_attendance_days:
            model.Add(sum(group_attendance_days) <= MAX_ATTENDANCE_DAYS)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_SOLVE_SECONDS
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.Solve(model)
    if status == cp_model.UNKNOWN:
        raise ScheduleTimeoutError(
            f"Could not find a valid timetable within {MAX_SOLVE_SECONDS:g} seconds. "
            "Reduce the number "
            "of sessions or add more eligible rooms and time slots, then try again."
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ScheduleInfeasibleError(
            "No valid timetable exists for these sessions, rooms, and time slots."
        )

    assignments = []
    for session in sessions:
        slot = next(
            slot
            for slot in slot_choices[session.session_id]
            if solver.BooleanValue(
                slot_variables[session.session_id][slot.time_slot_id]
            )
        )
        assigned_room = next(
            (
                room_id
                for room_id, selected in room_choices[session.session_id].items()
                if solver.BooleanValue(selected)
            ),
            None,
        )
        assignments.append(
            Assignment(session.session_id, assigned_room, slot.time_slot_id)
        )
    return assignments