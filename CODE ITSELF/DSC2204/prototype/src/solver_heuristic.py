"""
Segment 3+4 fallback — pure-Python heuristic solver.

Used when OR-Tools is unavailable (e.g. in restricted sandboxes) so we
can still demonstrate the prototype end-to-end.  The deliverable solver
is `solver_cpsat`; this one shares the *same* `Timetable` interface and
constraint engine, so the dashboard and validation pipeline don't care
which produced the result.

Algorithm (operations-analyst view)
-----------------------------------
1. **Order activities by difficulty.**  Difficulty = a heuristic score
   combining number of week-overlapping conflicts, narrowest room
   feasibility set and largest enrolment.  Hardest first to fail fast.
2. **Greedy placement** — for each activity in order, scan (day, start, room)
   in a soft-cost-aware order and pick the first slot that violates no
   hard constraint of any already-placed activity.
3. **Local search optimisation** — randomised hill-climbing.  Try
   `swap` and `reassign` moves; accept anything that lowers the soft
   score (constraint engine's `soft_score`).
"""
from __future__ import annotations
import random
from collections import defaultdict
from typing import List, Dict, Tuple

from .models import (
    Activity, Assignment, Room, Timetable, Universe,
)
from .solver_cpsat import feasible_rooms, _build_assignment, assign_secondary_rooms
from .constraint_engine import check, _slots_overlap, _weeks_overlap


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


def solve(universe: Universe, time_limit_s: int = 30,
          seed: int = 42, verbose: bool = True) -> Timetable:
    rng = random.Random(seed)
    activities = universe.all_activities()
    courses_by_code = {c.code: c for c in universe.courses}

    from .data_loader import DAY_START_HOUR
    _slot = lambda hh: (hh - DAY_START_HOUR) * 2
    slot_09 = _slot(9)
    slot_11 = _slot(11)
    slot_12 = _slot(12)
    slot_13 = _slot(13)
    slot_14 = _slot(14)
    slot_17 = _slot(17)
    slot_18 = _slot(18)
    LUNCH_PAIRS = list(range(slot_11, slot_14 - 1))

    def _tutor_has_lunch_gap(tid: str, day: str, new_start: int, new_dur: int) -> bool:
        occupied: set[int] = set()
        for other in placed.values():
            if other.day != day:
                continue
            if tid in {other.tutor_id, *getattr(other, "co_tutor_ids", [])}:
                occupied.update(range(other.start_index,
                                      other.start_index + other.duration_slots))
        occupied.update(range(new_start, new_start + new_dur))
        return any(p not in occupied and (p + 1) not in occupied for p in LUNCH_PAIRS)

    def _cohort_has_lunch_gap(coh: str, si, day: str, new_start: int, new_dur: int) -> bool:
        occupied: set[int] = set()
        for aid, other in placed.items():
            if other.day != day:
                continue
            oc = cohort_of.get(aid)
            os = subidx_of.get(aid)
            if oc != coh:
                continue
            if si is not None and os is not None and os != si:
                continue
            occupied.update(range(other.start_index,
                                  other.start_index + other.duration_slots))
        occupied.update(range(new_start, new_start + new_dur))
        return any(p not in occupied and (p + 1) not in occupied for p in LUNCH_PAIRS)

    tutor_avail: dict[str, dict[str, list[int]]] = {
        t.id: t.availability for t in universe.tutors
    }
    cohort_of = {a.id: _cohort(a, courses_by_code) for a in activities}
    subidx_of = {a.id: _subindex(a.group_id) for a in activities}
    days = universe.days
    n_slots = universe.slot_count

    # Pre-compute feasible rooms for each activity.
    rooms_for: dict[str, list[Room]] = {a.id: feasible_rooms(a, universe) for a in activities}
    for a in activities:
        if not rooms_for[a.id]:
            raise RuntimeError(f"No feasible rooms for {a.id}")

    # 1. Difficulty ordering ------------------------------------------------
    by_id = {a.id: a for a in activities}
    week_overlap_count = {a.id: sum(
        1 for b in activities if a is not b and set(a.weeks) & set(b.weeks)
    ) for a in activities}

    def difficulty(a: Activity) -> tuple:
        # Sorted ASCENDING — smaller tuple = placed first.
        # Pinned activities first (most constrained).
        # Then fewer feasible rooms (harder to fit).
        # Then bigger groups (harder to find a room for).
        # Then more week-overlapping conflicts.
        pinned = 0 if (a.fixed_day or a.fixed_start_index is not None) else 1
        return (
            pinned,                         # 0 = pinned first, 1 = unpinned
            len(rooms_for[a.id]),           # fewer rooms => placed earlier
            -a.size,                        # bigger groups => placed earlier
            -week_overlap_count[a.id],
            a.id,
        )

    ordered = sorted(activities, key=difficulty)

    # 2. Greedy placement ---------------------------------------------------
    placed: dict[str, Assignment] = {}
    tutor_name = {t.id: t.name for t in universe.tutors}

    def feasible_slot(a: Activity, day: str, start: int, room: Room) -> bool:
        # mode→room compat already enforced via rooms_for
        # fixed pins
        if a.fixed_day and day != a.fixed_day: return False
        if a.fixed_start_index is not None and start != a.fixed_start_index: return False
        end = start + a.duration_slots
        if end > n_slots: return False

        # MSc/evening activities (column M ticked) are exempt from daytime window rules.
        _evening = a.is_evening

        # Time-window rules (mirror the CP-SAT solver hard constraints)
        if start < slot_09: return False                              # A1: no classes before 09:00
        if not _evening:
            if end > slot_18: return False                            # global end-by-18:00
            if day == "Wed" and end > slot_13: return False          # Wed afternoon ban
            if day == "Fri":
                if not (end <= slot_12 or start >= slot_14): return False  # Fri protected 12:00–14:00
                if end > slot_17: return False                        # Fri end-by-17:00

        # Partial tutor availability
        for tid in {a.tutor_id, *a.co_tutor_ids}:
            avail = tutor_avail.get(tid)
            if not avail or day not in avail:
                continue
            day_slots = avail[day]
            if not day_slots:
                return False  # day fully blocked
            allowed = set(day_slots)
            if any((start + k) not in allowed for k in range(a.duration_slots)):
                return False

        # Flexible lunch gap — tutor and cohort must keep a free 1h in 11:00–14:00
        for tid in {a.tutor_id, *a.co_tutor_ids}:
            if not _tutor_has_lunch_gap(tid, day, start, a.duration_slots):
                return False
        if not _cohort_has_lunch_gap(cohort_of[a.id], subidx_of[a.id],
                                     day, start, a.duration_slots):
            return False

        # collide check vs already placed (week-aware)
        for other in placed.values():
            if other.day != day: continue
            ob = by_id[other.activity_id]
            if not (set(a.weeks) & set(ob.weeks)): continue
            # time overlap?
            if not (other.start_index + other.duration_slots <= start
                    or end <= other.start_index):
                _at = {a.tutor_id, *a.co_tutor_ids}
                _ot = {other.tutor_id, *getattr(other, "co_tutor_ids", [])}
                if (other.room_id == room.id and not room.is_virtual) \
                   or (_at & _ot) \
                   or other.group_id == a.group_id:
                    return False
                # H8 cohort clash: different module-groups, same physical students
                if cohort_of.get(a.id) == cohort_of.get(other.activity_id):
                    si = subidx_of.get(a.id); sj = subidx_of.get(other.activity_id)
                    if si is None or sj is None or si == sj:
                        return False
        return True

    # Slot scan order — prefer Mon/Tue for online classes; otherwise spread evenly
    def slot_iter(a: Activity):
        if a.delivery_mode.value in ("online_sync", "online_async"):
            day_order = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        else:
            day_order = ["Wed", "Thu", "Fri", "Mon", "Tue"]
        # An explicit pin in Remarks wins over the canonical period block.
        if a.fixed_start_index is not None:
            start_order = [a.fixed_start_index]
        else:
            # Use SIT canonical period starts, then fall back to all valid starts
            # so that activities whose tutors are available only outside canonical
            # windows (e.g. "prefer late-morning" → 10:00, not a canonical period)
            # can still be placed.
            from .data_loader import canonical_starts
            canon = canonical_starts(a.duration_slots)
            all_valid = list(range(n_slots - a.duration_slots + 1))
            if canon:
                canon_set = set(s for s in canon if s + a.duration_slots <= n_slots)
                # Extra starts not already in canon (sorted to prefer centre-day)
                extras = sorted(
                    (s for s in all_valid if s not in canon_set),
                    key=lambda s: (abs(s - 4), s),
                )
                start_order = [s for s in canon if s + a.duration_slots <= n_slots] + extras
            else:
                start_order = sorted(all_valid, key=lambda s: (abs(s - 4), s))
        if a.fixed_day:
            day_order = [a.fixed_day]
        for d in day_order:
            for s in start_order:
                for r in rooms_for[a.id]:
                    yield d, s, r

    for a in ordered:
        chosen = None
        for d, s, r in slot_iter(a):
            if feasible_slot(a, d, s, r):
                chosen = (d, s, r)
                break
        if chosen is None:
            raise RuntimeError(f"Greedy could not place {a.id}; consider relaxing rooms or duration")
        d, s, r = chosen
        placed[a.id] = _build_assignment(a, d, s, r, tutor_name)
        if verbose:
            print(f"  placed {a.id:30s} → {d} {placed[a.id].start_label} {r.id}")

    # 3. Local search -------------------------------------------------------
    timetable = Timetable(assignments=list(placed.values()),
                          metadata={"solver": "greedy+local_search"})
    best_score = check(timetable, universe).soft_score
    if verbose:
        print(f"  greedy soft score = {best_score}")

    import time
    deadline = time.time() + time_limit_s
    iters = 0
    while time.time() < deadline:
        iters += 1
        # pick a random activity and attempt a re-placement
        target = rng.choice(activities)
        candidates = []
        for d, s, r in slot_iter(target):
            # temporarily remove, test, restore
            saved = placed.pop(target.id)
            if feasible_slot(target, d, s, r):
                placed[target.id] = _build_assignment(target, d, s, r, tutor_name)
                tt = Timetable(assignments=list(placed.values()))
                rep = check(tt, universe)
                if rep.is_feasible:
                    candidates.append((rep.soft_score, (d, s, r)))
                placed.pop(target.id)
            placed[target.id] = saved
            if len(candidates) >= 8:    # bounded scan per move
                break
        if not candidates:
            continue
        sc, (d, s, r) = min(candidates, key=lambda x: x[0])
        if sc < best_score:
            placed[target.id] = _build_assignment(target, d, s, r, tutor_name)
            best_score = sc
            if verbose:
                print(f"  iter {iters:4d}: improved → {sc}")

    if verbose:
        print(f"  done after {iters} iters; final soft score = {best_score}")

    timetable = Timetable(
        assignments=list(placed.values()),
        metadata={
            "solver": "greedy+local_search",
            "iterations": iters,
            "soft_score": best_score,
        },
    )
    assign_secondary_rooms(timetable, universe, by_id)
    return timetable
