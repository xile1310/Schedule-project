"""
Segment 3+4 — Schedule Generator and Optimiser using OR-Tools CP-SAT.

Why CP-SAT
----------
Academic timetabling is a Constraint-Satisfaction problem with a
secondary weighted objective.  Google OR-Tools CP-SAT is the
industry-standard open-source solver for this class of problem,
handles tens of thousands of Boolean variables comfortably, and
exposes a clean Python interface.  Hard constraints become solver
constraints; soft constraints become a weighted objective that the
solver minimises after feasibility is achieved.

Decision variables (per activity i)
    day_i    in [0, |days|-1]
    start_i  in [0, slots_per_day - duration_i]
    room_i   in feasible_rooms_for_i  (Int domain)

Hard constraints
    H1 No two classes in same room at same time
    H2 Tutor cannot teach two simultaneously
    H3 Group cannot attend two simultaneously
    H4 Room capacity (encoded into the room domain)
    H5 Mode/room compatibility (encoded into the room domain)
    H6 Week overlap (only conflict when classes share a teaching week)
    H7 Calendar (only valid teaching weeks used)

Soft constraints (objective)
    S1 mode switches in adjacent slots (tutor or group)
    S2 tutor idle gaps > 2h
    S3 group consecutive teaching > 4h
    S4 short campus days
    S5 online classes outside Mon/Tue
"""
from __future__ import annotations
from typing import Dict, List
from collections import defaultdict

from .models import (
    Activity, Assignment, Room, RoomType, Timetable, Universe,
)


# ---------------------------------------------------------------------------
# Room feasibility (encodes H4 + H5 + room-type preferences)
# ---------------------------------------------------------------------------

def feasible_rooms(activity: Activity, universe: Universe) -> List[Room]:
    """All rooms an activity may legally occupy."""
    rooms: list[Room] = []
    atype = activity.activity_type.value
    mode = activity.delivery_mode.value
    for r in universe.rooms:
        if mode in ("online_sync", "online_async"):
            if r.is_virtual:
                rooms.append(r)
            continue
        if r.is_virtual:
            continue
        if r.capacity < activity.size:
            continue
        if atype == "Lecture":
            if r.room_type != RoomType.LECTURE_THEATRE:
                continue
        elif atype == "Laboratory":
            if r.room_type not in (RoomType.LABORATORY, RoomType.COMPUTER_LAB):
                continue
        elif atype in ("Tutorial", "Seminar"):
            if r.room_type not in (RoomType.SEMINAR_ROOM, RoomType.LABORATORY):
                continue
        # Workshops / quizzes / others accept any sufficiently-large room
        rooms.append(r)
    return rooms


# ---------------------------------------------------------------------------
# Helper used by both solvers to materialise an Assignment
# ---------------------------------------------------------------------------

def build_assignment(activity: Activity, day: str, start: int,
                     room: Room, tutor_names: dict) -> Assignment:
    from .data_loader import DAY_START_HOUR, SLOT_MIN
    start_min = DAY_START_HOUR * 60 + start * SLOT_MIN
    end_min = start_min + activity.duration_slots * SLOT_MIN
    fmt = lambda m: f"{m // 60:02d}{m % 60:02d}"
    return Assignment(
        activity_id=activity.id,
        course_code=activity.course_code,
        activity_type=activity.activity_type.value,
        delivery_mode=activity.delivery_mode.value,
        group_id=activity.group_id,
        tutor_id=activity.tutor_id,
        tutor_name=tutor_names.get(activity.tutor_id, activity.tutor_id),
        room_id=room.id,
        room_name=room.name,
        day=day,
        start_index=start,
        duration_slots=activity.duration_slots,
        start_label=fmt(start_min),
        end_label=fmt(end_min),
        weeks=list(activity.weeks),
        size=activity.size,
        co_tutor_ids=list(activity.co_tutor_ids),
        co_tutor_names=[tutor_names.get(t, t) for t in activity.co_tutor_ids],
    )


# Backwards-compatible alias (the heuristic imports this name)
_build_assignment = build_assignment


# ---------------------------------------------------------------------------
# CP-SAT solver
# ---------------------------------------------------------------------------



# Cohort awareness — same students across module-groups
import re as _re

def _cohort(activity, courses_by_code):
    course = courses_by_code.get(activity.course_code)
    year = course.year if course else 0
    prog = (course.programme if course else activity.course_code[:3]).upper()
    return f"{prog}/Y{year}"

def _subindex(group_id):
    label = group_id.split("/")[-1] if "/" in group_id else group_id
    if label.lower() == "all": return None
    m = _re.match(r"^[A-Za-z]+(\d+)$", label)
    return int(m.group(1)) if m else None


def solve(universe: Universe, time_limit_s: int = 60,
          enable_soft: bool = True, verbose: bool = True) -> Timetable:
    try:
        from ortools.sat.python import cp_model
    except ImportError as e:
        raise RuntimeError(
            "OR-Tools is not installed. Install with: pip install ortools"
        ) from e

    activities = universe.all_activities()
    days = universe.days
    n_days = len(days)
    n_slots = universe.slot_count

    model = cp_model.CpModel()

    by_id = {a.id: a for a in activities}
    courses_by_code = {c.code: c for c in universe.courses}
    cohort_of = {a.id: _cohort(a, courses_by_code) for a in activities}
    subidx_of = {a.id: _subindex(a.group_id) for a in activities}
    rooms_for: dict[str, list[Room]] = {}
    room_index = {r.id: i for i, r in enumerate(universe.rooms)}
    day_var: dict[str, cp_model.IntVar] = {}
    start_var: dict[str, cp_model.IntVar] = {}
    room_var: dict[str, cp_model.IntVar] = {}

    for a in activities:
        feas = feasible_rooms(a, universe)
        if not feas:
            raise RuntimeError(f"No feasible rooms for {a.id} (size={a.size}, mode={a.delivery_mode.value})")
        rooms_for[a.id] = feas
        day_var[a.id] = model.NewIntVar(0, n_days - 1, f"day_{a.id}")
        from .data_loader import canonical_starts as _canon
        # An explicit pin in Remarks wins over the canonical period block —
        # if pinned, the start domain is just that single value.
        if a.fixed_start_index is not None:
            _domain = [a.fixed_start_index]
        else:
            _domain = _canon(a.duration_slots) or list(range(0, n_slots - a.duration_slots + 1))
        start_var[a.id] = model.NewIntVarFromDomain(
            cp_model.Domain.FromValues(_domain), f"start_{a.id}")
        room_var[a.id] = model.NewIntVarFromDomain(
            cp_model.Domain.FromValues([room_index[r.id] for r in feas]),
            f"room_{a.id}",
        )
        if a.fixed_day:
            model.Add(day_var[a.id] == days.index(a.fixed_day))
        # fixed_start_index is now baked into the domain above

    # Resource exclusion helper
    virtual_idxs = {room_index[r.id] for r in universe.rooms if r.is_virtual}
    v_idx = next(iter(virtual_idxs)) if virtual_idxs else -1

    def add_pair(ai: str, aj: str, share_room: bool, share_tutor: bool, share_group: bool):
        di = by_id[ai].duration_slots
        dj = by_id[aj].duration_slots
        same_day = model.NewBoolVar(f"sd_{ai}_{aj}")
        model.Add(day_var[ai] == day_var[aj]).OnlyEnforceIf(same_day)
        model.Add(day_var[ai] != day_var[aj]).OnlyEnforceIf(same_day.Not())

        before = model.NewBoolVar(f"b_{ai}_{aj}")
        after = model.NewBoolVar(f"a_{ai}_{aj}")
        model.Add(start_var[ai] + di <= start_var[aj]).OnlyEnforceIf(before)
        model.Add(start_var[aj] + dj <= start_var[ai]).OnlyEnforceIf(after)

        if share_tutor or share_group:
            # If same day, must not overlap
            model.AddBoolOr([same_day.Not(), before, after])

        if share_room:
            same_room = model.NewBoolVar(f"sr_{ai}_{aj}")
            model.Add(room_var[ai] == room_var[aj]).OnlyEnforceIf(same_room)
            model.Add(room_var[ai] != room_var[aj]).OnlyEnforceIf(same_room.Not())

            ri_phys = model.NewBoolVar(f"rip_{ai}_{aj}")
            rj_phys = model.NewBoolVar(f"rjp_{ai}_{aj}")
            if v_idx >= 0:
                model.Add(room_var[ai] != v_idx).OnlyEnforceIf(ri_phys)
                model.Add(room_var[ai] == v_idx).OnlyEnforceIf(ri_phys.Not())
                model.Add(room_var[aj] != v_idx).OnlyEnforceIf(rj_phys)
                model.Add(room_var[aj] == v_idx).OnlyEnforceIf(rj_phys.Not())
            else:
                model.Add(ri_phys == 1)
                model.Add(rj_phys == 1)
            # If same physical room AND same day, must not overlap.
            # equivalent to NOT(same_room AND same_day AND ri_phys AND rj_phys) OR before OR after
            model.AddBoolOr([
                same_room.Not(), same_day.Not(),
                ri_phys.Not(), rj_phys.Not(),
                before, after,
            ])

    # H1 + H2 + H3
    by_tutor: dict[str, list[str]] = defaultdict(list)
    by_group: dict[str, list[str]] = defaultdict(list)
    for a in activities:
        for _t in {a.tutor_id, *a.co_tutor_ids}:
            by_tutor[_t].append(a.id)
        by_group[a.group_id].append(a.id)

    ids = [a.id for a in activities]
    for i in range(len(ids)):
        wi = set(by_id[ids[i]].weeks)
        for j in range(i + 1, len(ids)):
            ai, aj = ids[i], ids[j]
            wj = set(by_id[aj].weeks)
            if not (wi & wj):
                continue   # H6: distinct weeks → no conflict possible
            share_tutor = by_id[ai].tutor_id == by_id[aj].tutor_id
            share_group = by_id[ai].group_id == by_id[aj].group_id
            # H8 cohort clash: same year of same programme, and either
            # contains everyone (subgroup=None) or both share subgroup index.
            ci, cj = cohort_of[ai], cohort_of[aj]
            si, sj = subidx_of[ai], subidx_of[aj]
            share_cohort = (
                ci == cj
                and (si is None or sj is None or si == sj)
            )
            # If they already share group_id, H3 handles it — no need to duplicate.
            if share_group:
                share_cohort = False
            add_pair(ai, aj, share_room=True,
                     share_tutor=share_tutor,
                     share_group=share_group or share_cohort)

    # ---- soft objective ---------------------------------------------------
    soft_terms = []
    if enable_soft:
        for a in activities:
            if a.delivery_mode.value in ("online_sync", "online_async"):
                bad = model.NewBoolVar(f"s5_{a.id}")
                model.Add(day_var[a.id] >= 2).OnlyEnforceIf(bad)
                model.Add(day_var[a.id] <= 1).OnlyEnforceIf(bad.Not())
                soft_terms.append(6 * bad)

        for tid, tids in by_tutor.items():
            if len(tids) < 2: continue
            for d in range(n_days):
                load = []
                for aid in tids:
                    on_day = model.NewBoolVar(f"odt_{tid}_{aid}_{d}")
                    model.Add(day_var[aid] == d).OnlyEnforceIf(on_day)
                    model.Add(day_var[aid] != d).OnlyEnforceIf(on_day.Not())
                    load.append(on_day * by_id[aid].duration_slots)
                excess = model.NewIntVar(0, n_slots, f"ex_{tid}_{d}")
                model.Add(excess >= sum(load) - 8)
                soft_terms.append(2 * excess)

        for gid, gids in by_group.items():
            if len(gids) < 2: continue
            for d in range(n_days):
                load = []
                for aid in gids:
                    on_day = model.NewBoolVar(f"odg_{gid}_{aid}_{d}")
                    model.Add(day_var[aid] == d).OnlyEnforceIf(on_day)
                    model.Add(day_var[aid] != d).OnlyEnforceIf(on_day.Not())
                    load.append(on_day * by_id[aid].duration_slots)
                excess = model.NewIntVar(0, n_slots, f"exg_{gid}_{d}")
                model.Add(excess >= sum(load) - 8)
                soft_terms.append(2 * excess)

    if soft_terms:
        model.Minimize(sum(soft_terms))

    # ---- solve ------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"CP-SAT could not find a feasible schedule (status={solver.StatusName(status)})")

    rooms_by_idx = {i: r for i, r in enumerate(universe.rooms)}
    tutor_name = {t.id: t.name for t in universe.tutors}
    assigns = []
    for a in activities:
        d = days[solver.Value(day_var[a.id])]
        s = solver.Value(start_var[a.id])
        r = rooms_by_idx[solver.Value(room_var[a.id])]
        assigns.append(build_assignment(a, d, s, r, tutor_name))

    return Timetable(
        assignments=assigns,
        metadata={
            "solver": "ortools.cp_sat",
            "status": solver.StatusName(status),
        },
    )


# --- end of solver_cpsat.py ---
