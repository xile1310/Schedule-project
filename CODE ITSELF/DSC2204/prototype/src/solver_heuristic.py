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
from .solver_cpsat import feasible_rooms, _build_assignment
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
            # Otherwise restrict to SIT canonical period starts.
            from .data_loader import canonical_starts
            canon = canonical_starts(a.duration_slots)
            if canon:
                start_order = [s for s in canon if s + a.duration_slots <= n_slots]
            else:
                start_order = sorted(range(n_slots - a.duration_slots + 1),
                                     key=lambda s: (abs(s - 4), s))
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

    return Timetable(
        assignments=list(placed.values()),
        metadata={
            "solver": "greedy+local_search",
            "iterations": iters,
            "soft_score": best_score,
        },
    )
