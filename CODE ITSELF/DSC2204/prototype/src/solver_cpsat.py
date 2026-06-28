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
    """All rooms an activity may legally occupy.

    Priority order:
      1. fixed_room_id pin (specific named room from remarks)
      2. room_type_req + room_cap_req  (room characteristic requirement from remarks)
         → primary: rooms of correct type with capacity ≥ max(room_cap_req, activity.size)
         → fallback: any physical room with capacity ≥ max(room_cap_req, activity.size)
           (type preference relaxed; capacity minimum is never relaxed)
      3. Default activity-type-based filtering (Lecture→LT, Lab→lab/CL, Tutorial→SR/lab)
    """
    if getattr(activity, "fixed_room_id", None):
        pinned = [r for r in universe.rooms if r.id == activity.fixed_room_id]
        if pinned:
            return pinned

    mode = activity.delivery_mode.value
    atype = activity.activity_type.value
    room_type_req = getattr(activity, "room_type_req", None)
    room_cap_req = getattr(activity, "room_cap_req", None)
    min_cap = max(room_cap_req or 0, activity.size)

    _type_map = {
        "seminar_room":    RoomType.SEMINAR_ROOM,
        "computer_lab":    RoomType.COMPUTER_LAB,
        "laboratory":      RoomType.LABORATORY,
        "lecture_theatre": RoomType.LECTURE_THEATRE,
        "other":           RoomType.OTHER,
    }
    req_type = _type_map.get(room_type_req) if room_type_req else None

    rooms: list[Room] = []
    for r in universe.rooms:
        if mode in ("online_sync", "online_async"):
            if r.is_virtual:
                rooms.append(r)
            continue
        if r.is_virtual:
            continue
        if r.capacity < min_cap:
            continue
        if req_type is not None:
            if r.room_type == req_type:
                rooms.append(r)
        elif atype == "Lecture":
            if r.room_type == RoomType.LECTURE_THEATRE:
                rooms.append(r)
        elif atype == "Laboratory":
            if r.room_type in (RoomType.LABORATORY, RoomType.COMPUTER_LAB):
                rooms.append(r)
        elif atype in ("Tutorial", "Seminar"):
            if r.room_type in (RoomType.SEMINAR_ROOM, RoomType.LABORATORY):
                rooms.append(r)
        else:
            rooms.append(r)

    # Fallback: required room type had no matches → any physical room ≥ min_cap.
    # Capacity floor is never relaxed; only the type preference is.
    if req_type is not None and not rooms:
        for r in universe.rooms:
            if r.is_virtual or mode in ("online_sync", "online_async"):
                continue
            if r.capacity >= min_cap:
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
        notes=getattr(activity, "notes", ""),
        shared_cohorts=list(getattr(activity, "shared_cohorts", [])),
    )


# Backwards-compatible alias (the heuristic imports this name)
_build_assignment = build_assignment


# ---------------------------------------------------------------------------
# Post-solve: assign secondary rooms for activities with room_count > 1
# ---------------------------------------------------------------------------

def assign_secondary_rooms(timetable: "Timetable", universe: Universe,
                           act_by_id: dict) -> None:
    """Mutate assignments in place: find a second compatible room for any
    activity whose room_count > 1 (e.g. a remark said '2 rooms').

    The secondary room must be compatible (capacity, mode) and not already
    booked at the same day/time/weeks combination.
    """
    # Build a quick-lookup of what's already booked: (room_id, day, slot) -> set[week]
    booked: dict = defaultdict(set)
    for asn in timetable.assignments:
        for sl in range(asn.start_index, asn.start_index + asn.duration_slots):
            booked[(asn.room_id, asn.day, sl)].update(asn.weeks)

    for asn in timetable.assignments:
        act = act_by_id.get(asn.activity_id)
        if not act or getattr(act, "room_count", 1) <= 1:
            continue
        # Candidates: same eligibility rules, not the primary room, not virtual
        candidates = [r for r in feasible_rooms(act, universe)
                      if r.id != asn.room_id and not r.is_virtual]
        for room in candidates:
            clash = False
            for sl in range(asn.start_index, asn.start_index + asn.duration_slots):
                if booked.get((room.id, asn.day, sl), set()) & set(asn.weeks):
                    clash = True
                    break
            if not clash:
                asn.room2_id = room.id
                asn.room2_name = room.name
                # Mark secondary room as booked so it isn't double-used
                for sl in range(asn.start_index, asn.start_index + asn.duration_slots):
                    booked[(room.id, asn.day, sl)].update(asn.weeks)
                break


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
        # All activities get full start-slot freedom in the hard model.
        # Restricting to canonical periods (09:00/12:00/14:00/16:00) causes a
        # pigeonhole infeasibility for cohorts that have ≥15 mutually-exclusive
        # activities — canonical starts are instead encouraged via soft constraints.
        if a.fixed_start_index is not None:
            _domain = [a.fixed_start_index]
        else:
            _domain = list(range(0, n_slots - a.duration_slots + 1))
        start_var[a.id] = model.NewIntVarFromDomain(
            cp_model.Domain.FromValues(_domain), f"start_{a.id}")
        room_var[a.id] = model.NewIntVarFromDomain(
            cp_model.Domain.FromValues([room_index[r.id] for r in feas]),
            f"room_{a.id}",
        )
        if a.fixed_day:
            model.Add(day_var[a.id] == days.index(a.fixed_day))
        # ------------------------------------------------------------------
        # Additional hard time-window rules from policy
        # A1: No classes before 09:00
        # A1/A2/A3, protected windows: lunch, Wed afternoon ban from 13:00,
        # Fri protected window 12:00-14:00, and no Friday classes after 17:00.
        from .data_loader import DAY_START_HOUR
        _slot = lambda hh: (hh - DAY_START_HOUR) * 2
        slot_09 = _slot(9)
        slot_12 = _slot(12)
        slot_13 = _slot(13)
        slot_14 = _slot(14)
        slot_17 = _slot(17)
        slot_18 = _slot(18)

        # Enforce earliest start at 09:00
        model.Add(start_var[a.id] >= slot_09)

        # MSc/evening activities (column M ticked) are exempt from the 18:00 cutoff.
        # They still end before day-end (n_slots). All other activities end by 18:00.
        _evening = a.is_evening
        if _evening:
            model.Add(start_var[a.id] + a.duration_slots <= n_slots)
        else:
            model.Add(start_var[a.id] + a.duration_slots <= slot_18)

        # Wednesday: no classes in the afternoon starting/overlapping from 13:00
        # Evening (MSc) activities are exempt — they start at 19:00, well past 13:00.
        if not _evening and "Wed" in days:
            is_wed = model.NewBoolVar(f"is_wed_{a.id}")
            model.Add(day_var[a.id] == days.index("Wed")).OnlyEnforceIf(is_wed)
            model.Add(day_var[a.id] != days.index("Wed")).OnlyEnforceIf(is_wed.Not())
            model.Add(start_var[a.id] + a.duration_slots <= slot_13).OnlyEnforceIf(is_wed)

        # Friday protected window 12:00-14:00 and stricter end-by-17:00
        # Evening (MSc) activities are exempt — 19:00-21:00 is outside both windows.
        if not _evening and "Fri" in days:
            is_fri = model.NewBoolVar(f"is_fri_{a.id}")
            model.Add(day_var[a.id] == days.index("Fri")).OnlyEnforceIf(is_fri)
            model.Add(day_var[a.id] != days.index("Fri")).OnlyEnforceIf(is_fri.Not())
            before = model.NewBoolVar(f"fri_before_{a.id}")
            after = model.NewBoolVar(f"fri_after_{a.id}")
            model.Add(start_var[a.id] + a.duration_slots <= slot_12).OnlyEnforceIf(before)
            model.Add(start_var[a.id] >= slot_14).OnlyEnforceIf(after)
            # If it's Friday, either before 12:00 or after 14:00 must hold
            model.AddBoolOr([before, after, is_fri.Not()])
            # Additionally, no Friday classes ending after 17:00
            model.Add(start_var[a.id] + a.duration_slots <= slot_17).OnlyEnforceIf(is_fri)
        # fixed_start_index is now baked into the domain above

    # Tutor availability — enforce tutor.availability windows (full-day blocks and
    # partial-day windows parsed from Column K remarks).
    tutor_map = {t.id: t for t in universe.tutors}
    for a in activities:
        for tid in {a.tutor_id, *a.co_tutor_ids}:
            t = tutor_map.get(tid)
            if not t or not t.availability:
                continue
            for day_name, allowed_slots in t.availability.items():
                if day_name not in days:
                    continue
                day_idx = days.index(day_name)
                if len(allowed_slots) == 0:
                    # Day completely blocked for this tutor
                    model.Add(day_var[a.id] != day_idx)
                else:
                    # Partial day availability — restrict to slots where the full
                    # activity fits within the tutor's available window.
                    allowed_set = set(allowed_slots)
                    duration = a.duration_slots
                    valid_starts = {
                        s for s in range(n_slots - duration + 1)
                        if all((s + k) in allowed_set for k in range(duration))
                    }
                    if not valid_starts:
                        # Activity can't fit in the window at all — block the day.
                        model.Add(day_var[a.id] != day_idx)
                    elif len(valid_starts) < n_slots - duration + 1:
                        # Some starts on this day are forbidden.
                        forbidden = [
                            (day_idx, s)
                            for s in range(n_slots - duration + 1)
                            if s not in valid_starts
                        ]
                        model.AddForbiddenAssignments(
                            [day_var[a.id], start_var[a.id]], forbidden
                        )

    # Build tutor/group activity maps early — needed by lunch gap AND H1/H2/H3.
    by_tutor: dict[str, list[str]] = defaultdict(list)
    by_group: dict[str, list[str]] = defaultdict(list)
    for a in activities:
        for _t in {a.tutor_id, *a.co_tutor_ids}:
            by_tutor[_t].append(a.id)
        by_group[a.group_id].append(a.id)

    # H7 (lunch gap) is enforced by the heuristic solver week-by-week.
    # CP-SAT cannot model it correctly because day_var is week-agnostic:
    # a Quiz in week 6 and a Lecture running weeks 1-13 both appear "on Tuesday"
    # simultaneously, making the constraint spuriously infeasible.

    # Public holiday constraints: if (week W, day D) is a public holiday,
    # any activity that runs during week W cannot be placed on day D.
    # day_var is week-agnostic, so this conservatively forbids the day across
    # all weeks — correct because the same slot repeats every week.
    _ph_week_day: list[tuple[int, int]] = []  # (teaching_week, day_index)
    if universe.calendar.public_holidays and universe.calendar.week_dates:
        from datetime import date as _phdate, timedelta as _phtd
        _DOW = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        _wk1_iso = universe.calendar.week_dates.get(min(universe.calendar.teaching_weeks))
        if _wk1_iso:
            _sem_start = _phdate.fromisoformat(_wk1_iso)
            for _iso in universe.calendar.public_holidays:
                try:
                    _hd = _phdate.fromisoformat(_iso)
                    _delta = (_hd - _sem_start).days
                    if _delta < 0:
                        continue
                    _hwk = _delta // 7 + 1
                    _hwdi = _hd.weekday()   # 0=Mon ... 4=Fri
                    if _hwdi >= 5:
                        continue
                    _hday = _DOW[_hwdi]
                    if _hday in days:
                        _ph_week_day.append((_hwk, days.index(_hday)))
                except (ValueError, TypeError):
                    pass
    if _ph_week_day:
        for a in activities:
            # Only constrain single-week activities. Multi-week activities
            # (e.g. a lecture running weeks 1-13) keep their day assignment and
            # simply don't meet on the holiday date — that occurrence is cancelled
            # by the calendar, not by changing the scheduled day for all weeks.
            if len(a.weeks) != 1:
                continue
            _a_weeks = set(a.weeks)
            for (_h_wk, _h_dix) in _ph_week_day:
                if _h_wk in _a_weeks:
                    model.Add(day_var[a.id] != _h_dix)

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
            # H8 cohort clash: same year of same programme (or shared via common module),
            # and either contains everyone (subgroup=None) or both share subgroup index.
            ci_set = {cohort_of[ai]} | set(getattr(by_id[ai], 'shared_cohorts', []))
            cj_set = {cohort_of[aj]} | set(getattr(by_id[aj], 'shared_cohorts', []))
            si, sj = subidx_of[ai], subidx_of[aj]
            share_cohort = (
                bool(ci_set & cj_set)
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
                model.Add(excess >= sum(load) - 6)  # penalise >3h/day per tutor
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
                model.Add(excess >= sum(load) - 6)  # penalise >3h/day per group (S3)
                soft_terms.append(2 * excess)

        # S_UTIL: prefer room utilisation >= 60%
        room_list = universe.rooms
        for a in activities:
            for r in rooms_for[a.id]:
                if r.is_virtual or r.capacity == 0:
                    continue
                util = a.size / r.capacity
                if util < 0.6:
                    b = model.NewBoolVar(f"sutil_{a.id}_{r.id}")
                    model.Add(room_var[a.id] == room_index[r.id]).OnlyEnforceIf(b)
                    model.Add(room_var[a.id] != room_index[r.id]).OnlyEnforceIf(b.Not())
                    penalty = max(1, round((0.6 - util) * 10))
                    soft_terms.append(penalty * b)

        # S_FIRSTLAST: avoid first/last slot of day for groups
        for a in activities:
            b_first = model.NewBoolVar(f"sfirst_{a.id}")
            model.Add(start_var[a.id] == slot_09).OnlyEnforceIf(b_first)
            model.Add(start_var[a.id] != slot_09).OnlyEnforceIf(b_first.Not())
            soft_terms.append(2 * b_first)
            if not a.is_evening:
                b_last = model.NewBoolVar(f"slast_{a.id}")
                model.Add(start_var[a.id] >= slot_17).OnlyEnforceIf(b_last)
                model.Add(start_var[a.id] < slot_17).OnlyEnforceIf(b_last.Not())
                soft_terms.append(2 * b_last)

        # S_END17: prefer end by 17:00 on Mon-Thu (Fri already hard-constrained)
        for a in activities:
            if a.is_evening:
                continue
            ends_late = model.NewBoolVar(f"end17_{a.id}")
            model.Add(start_var[a.id] + a.duration_slots > slot_17).OnlyEnforceIf(ends_late)
            model.Add(start_var[a.id] + a.duration_slots <= slot_17).OnlyEnforceIf(ends_late.Not())
            soft_terms.append(3 * ends_late)

        # S_CANON: prefer canonical SIT period starts (09:00/12:00/14:00/16:00).
        # The hard model allows any slot (to avoid pigeonhole infeasibility);
        # this soft term steers the solver back toward the standard periods.
        from .data_loader import canonical_starts as _canon_soft
        for a in activities:
            if a.fixed_start_index is not None or a.is_evening:
                continue
            canon = _canon_soft(a.duration_slots)
            if not canon:
                continue
            off_canon = model.NewBoolVar(f"soff_{a.id}")
            model.AddLinearExpressionInDomain(
                start_var[a.id],
                cp_model.Domain.FromValues(canon),
            ).OnlyEnforceIf(off_canon.Not())
            soft_terms.append(4 * off_canon)

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

    timetable = Timetable(
        assignments=assigns,
        metadata={
            "solver": "ortools.cp_sat",
            "status": solver.StatusName(status),
        },
    )
    assign_secondary_rooms(timetable, universe, by_id)
    return timetable


# --- end of solver_cpsat.py ---
