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


@dataclass(frozen=True)
class Assignment:
    session_id: int
    room_id: int
    time_slot_id: int


class ScheduleInfeasibleError(ValueError):
    pass


def generate_schedule(
    sessions: list[Session], rooms: list[Room], time_slots: list[TimeSlot]
) -> list[Assignment]:
    """Assign each session a compatible room and time without resource conflicts."""
    if not sessions:
        return []

    model = cp_model.CpModel()
    options: dict[tuple[int, int, int], cp_model.IntVar] = {}
    session_options: dict[int, list[cp_model.IntVar]] = {
        session.session_id: [] for session in sessions
    }

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
        compatible_rooms = [
            room
            for room in rooms
            if room.room_type is None
            or session.activity_type is None
            or room.room_type.casefold() == session.activity_type.casefold()
        ]
        for room in rooms:
            if room.capacity < session.student_count:
                continue
            if (
                room.room_type is not None
                and session.activity_type is not None
                and room.room_type.casefold() != session.activity_type.casefold()
            ):
                continue
            for slot in eligible_slots:
                variable = model.NewBoolVar(
                    f"session_{session.session_id}_room_{room.room_id}_slot_{slot.time_slot_id}"
                )
                options[(session.session_id, room.room_id, slot.time_slot_id)] = variable
                session_options[session.session_id].append(variable)

        if not session_options[session.session_id]:
            if compatible_rooms:
                largest_capacity = max(room.capacity for room in compatible_rooms)
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
        model.AddExactlyOne(session_options[session.session_id])

    objective_terms: list[cp_model.LinearExpr] = []
    session_by_id = {session.session_id: session for session in sessions}
    slot_by_id = {slot.time_slot_id: slot for slot in time_slots}
    slot_order = {
        slot.time_slot_id: index
        for day in {slot.day_of_week for slot in time_slots}
        for index, slot in enumerate(
            sorted(
                (item for item in time_slots if item.day_of_week == day),
                key=lambda item: item.start_minute,
            )
        )
    }

    for index, first in enumerate(sessions):
        for second in sessions[index + 1 :]:
            shares_instructor = (
                first.instructor_id is not None
                and first.instructor_id == second.instructor_id
            )
            shares_group = bool(set(first.group_ids).intersection(second.group_ids))
            if not shares_instructor and not shares_group:
                continue

            for first_key, first_var in options.items():
                if first_key[0] != first.session_id:
                    continue
                first_slot = slot_by_id[first_key[2]]
                for second_key, second_var in options.items():
                    if second_key[0] != second.session_id:
                        continue
                    second_slot = slot_by_id[second_key[2]]
                    overlaps = (
                        first_slot.day_of_week == second_slot.day_of_week
                        and first_slot.start_minute < second_slot.end_minute
                        and second_slot.start_minute < first_slot.end_minute
                    )
                    same_room = first_key[1] == second_key[1]
                    if overlaps and (shares_instructor or shares_group or same_room):
                        model.Add(first_var + second_var <= 1)
                    elif (
                        first_slot.day_of_week == second_slot.day_of_week
                        and not overlaps
                    ):
                        gap = abs(slot_order[first_slot.time_slot_id] - slot_order[second_slot.time_slot_id]) - 1
                        if gap > 0:
                            together = model.NewBoolVar(
                                f"gap_{first.session_id}_{second.session_id}_{first_key[2]}_{second_key[2]}"
                            )
                            model.Add(together <= first_var)
                            model.Add(together <= second_var)
                            model.Add(together >= first_var + second_var - 1)
                            objective_terms.append(gap * together)

    for index, first in enumerate(sessions):
        for second in sessions[index + 1 :]:
            for first_key, first_var in options.items():
                if first_key[0] != first.session_id:
                    continue
                for second_key, second_var in options.items():
                    if second_key[0] == second.session_id and first_key[1] == second_key[1]:
                        first_slot = slot_by_id[first_key[2]]
                        second_slot = slot_by_id[second_key[2]]
                        if (
                            first_slot.day_of_week == second_slot.day_of_week
                            and first_slot.start_minute < second_slot.end_minute
                            and second_slot.start_minute < first_slot.end_minute
                        ):
                            model.Add(first_var + second_var <= 1)

    if objective_terms:
        model.Minimize(sum(objective_terms))

    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ScheduleInfeasibleError(
            "No valid timetable exists for these sessions, rooms, and time slots."
        )

    assignments = []
    for (session_id, room_id, time_slot_id), variable in options.items():
        if solver.BooleanValue(variable):
            assignments.append(Assignment(session_id, room_id, time_slot_id))
    return assignments