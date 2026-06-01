"""
Segment 2 — Constraint Engine

Audits a `Timetable` against the project's hard and soft constraints.
Returns a structured violation report so each segment of the system
(generator, optimiser, dashboard) consumes the same evidence.

Design notes
------------
* The engine NEVER mutates inputs — pure, deterministic, easy to test.
* Each check has a short docstring matching the requirement document.
* `check()` always runs every rule, even on infeasible timetables, so
  developers see the *full* picture of what's wrong.

Hard constraints
~~~~~~~~~~~~~~~~
H1  No two classes in the same room at the same time
H2  Tutor cannot teach two classes at the same time
H3  Student group cannot attend two classes at the same time
H4  Room capacity ≥ class enrolment
H5  Online → virtual room only; Face-to-face → never virtual room
H6  Odd/even week courses run on their respective weeks only
    (modelled as: two classes that share *any* week cannot share a slot
    on the same room/tutor/group — the H1/H2/H3 checks therefore must
    consider week overlap)
H7  No classes on public holidays / term breaks
    (handled at week-pattern level — only weeks listed in `Calendar.teaching_weeks`
     may be used, anything else is a violation)

Soft constraints
~~~~~~~~~~~~~~~~
S1  Avoid online ↔ f2f switches in adjacent slots (same tutor or group)
S2  Avoid tutor idle gaps > 2h on the same day
S3  Avoid student group having > 4 consecutive teaching hours
S4  Avoid only 1–2 hours per day on campus (groups)
S5  Schedule online classes within one day (Monday or Tuesday) per programme
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple, Iterable
from collections import defaultdict
import json

from .models import Assignment, Timetable, Universe


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    code: str             # "H1", "S2", ...
    severity: str         # "hard" or "soft"
    message: str
    affected: List[str] = field(default_factory=list)   # activity ids
    weight: int = 0       # used by the optimiser

    def as_dict(self):
        return asdict(self)


@dataclass
class ViolationReport:
    hard: List[Violation] = field(default_factory=list)
    soft: List[Violation] = field(default_factory=list)

    @property
    def is_feasible(self) -> bool:
        return len(self.hard) == 0

    @property
    def soft_score(self) -> int:
        """Lower is better."""
        return sum(v.weight for v in self.soft)

    def summary(self) -> Dict:
        return {
            "feasible": self.is_feasible,
            "hard_count": len(self.hard),
            "soft_count": len(self.soft),
            "soft_score": self.soft_score,
        }

    def as_dict(self):
        return {
            "summary": self.summary(),
            "hard": [v.as_dict() for v in self.hard],
            "soft": [v.as_dict() for v in self.soft],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot_range(a: Assignment) -> range:
    return range(a.start_index, a.start_index + a.duration_slots)


def _weeks_overlap(a: Assignment, b: Assignment) -> bool:
    return bool(set(a.weeks) & set(b.weeks))


def _slots_overlap(a: Assignment, b: Assignment) -> bool:
    if a.day != b.day:
        return False
    return not (a.start_index + a.duration_slots <= b.start_index
                or b.start_index + b.duration_slots <= a.start_index)


# ---------------------------------------------------------------------------
# Soft constraint weights (operations-analyst tunable)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# H8: Cohort clash (students physically can't be in two places)
# ---------------------------------------------------------------------------
import re as _re

def _cohort_of(a, courses_by_code):
    """Cohort label for an activity, e.g. 'DSC/Y1' or 'INF/Y2'."""
    code = a.course_code
    course = courses_by_code.get(code)
    year = course.year if course else 0
    prog = (course.programme if course else code[:3]).upper()
    return f"{prog}/Y{year}"

def _subgroup_index(group_id: str):
    """Extract the trailing subgroup number, e.g. 'DSC1001/T3' -> 3.
    Returns None for 'All' / lecture cohorts that contain every sub-group."""
    label = group_id.split("/")[-1] if "/" in group_id else group_id
    if label.lower() == "all":
        return None
    m = _re.match(r"^[A-Za-z]+(\d+)$", label)
    return int(m.group(1)) if m else None


WEIGHTS = {
    "S1_mode_switch": 5,
    "S2_tutor_gap":   3,    # per excess hour beyond 2h gap
    "S3_long_block":  4,    # per excess hour beyond 4h consecutive
    "S4_short_day":   2,    # per group/day with only 1-2 hours on campus
    "S5_online_day":  6,    # per misplaced online class
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(timetable: Timetable, universe: Universe) -> ViolationReport:
    report = ViolationReport()
    # Cache lookups
    rooms = {r.id: r for r in universe.rooms}
    tutors = {t.id: t for t in universe.tutors}

    a = timetable.assignments

    # ------------------- HARD --------------------------------------------

    # H1: room collisions (week-aware)
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            if a[i].room_id == a[j].room_id and not rooms[a[i].room_id].is_virtual:
                if _slots_overlap(a[i], a[j]) and _weeks_overlap(a[i], a[j]):
                    report.hard.append(Violation(
                        code="H1", severity="hard",
                        message=f"Room {a[i].room_id} double-booked: "
                                f"{a[i].activity_id} vs {a[j].activity_id} on {a[i].day}",
                        affected=[a[i].activity_id, a[j].activity_id],
                    ))

    # H2: tutor double-booking
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            ti = {a[i].tutor_id, *getattr(a[i], "co_tutor_ids", [])}
            tj = {a[j].tutor_id, *getattr(a[j], "co_tutor_ids", [])}
            if ti & tj:
                if _slots_overlap(a[i], a[j]) and _weeks_overlap(a[i], a[j]):
                    shared = ", ".join(sorted(ti & tj))
                    report.hard.append(Violation(
                        code="H2", severity="hard",
                        message=f"Tutor(s) {shared} double-booked on {a[i].day} "
                                f"({a[i].activity_id} vs {a[j].activity_id})",
                        affected=[a[i].activity_id, a[j].activity_id],
                    ))

    # H3: student group double-booking
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            if a[i].group_id == a[j].group_id:
                if _slots_overlap(a[i], a[j]) and _weeks_overlap(a[i], a[j]):
                    report.hard.append(Violation(
                        code="H3", severity="hard",
                        message=f"Group {a[i].group_id} double-booked on {a[i].day}",
                        affected=[a[i].activity_id, a[j].activity_id],
                    ))
    # H8: cohort clash — different module-groups but the same physical students
    courses_by_code = {c.code: c for c in universe.courses}
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            ci = _cohort_of(a[i], courses_by_code)
            cj = _cohort_of(a[j], courses_by_code)
            if ci != cj:
                continue
            if not _slots_overlap(a[i], a[j]) or not _weeks_overlap(a[i], a[j]):
                continue
            si = _subgroup_index(a[i].group_id)
            sj = _subgroup_index(a[j].group_id)
            # If either is the "All" lecture cohort, every student clashes.
            # If both are sub-groups, only same-index sub-groups share students.
            if si is None or sj is None or si == sj:
                # don't double-report what H3 already flagged
                if a[i].group_id == a[j].group_id:
                    continue
                report.hard.append(Violation(
                    code="H8", severity="hard",
                    message=f"Cohort clash ({ci}): {a[i].activity_id} and "
                            f"{a[j].activity_id} share students on {a[i].day}",
                    affected=[a[i].activity_id, a[j].activity_id],
                ))


    # H4: room capacity
    for x in a:
        room = rooms.get(x.room_id)
        if room and not room.is_virtual and room.capacity < x.size:
            report.hard.append(Violation(
                code="H4", severity="hard",
                message=f"Room {room.id} (cap {room.capacity}) too small for "
                        f"{x.activity_id} (size {x.size})",
                affected=[x.activity_id],
            ))

    # H5: mode/room compatibility
    for x in a:
        room = rooms.get(x.room_id)
        if not room:
            continue
        if x.delivery_mode in ("online_sync", "online_async") and not room.is_virtual:
            report.hard.append(Violation(
                code="H5", severity="hard",
                message=f"Online class {x.activity_id} placed in physical room {room.id}",
                affected=[x.activity_id],
            ))
        if x.delivery_mode == "f2f" and room.is_virtual:
            report.hard.append(Violation(
                code="H5", severity="hard",
                message=f"Face-to-face class {x.activity_id} placed in virtual room",
                affected=[x.activity_id],
            ))

    # H7: weeks must be teaching weeks
    valid_weeks = set(universe.calendar.teaching_weeks)
    for x in a:
        bad = [w for w in x.weeks if w not in valid_weeks]
        if bad:
            report.hard.append(Violation(
                code="H7", severity="hard",
                message=f"{x.activity_id} scheduled on non-teaching weeks {bad}",
                affected=[x.activity_id],
            ))

    # ------------------- SOFT --------------------------------------------

    # S1 — mode switches in adjacent slots for same tutor or group
    by_day_tutor: dict[tuple, list[Assignment]] = defaultdict(list)
    by_day_group: dict[tuple, list[Assignment]] = defaultdict(list)
    for x in a:
        by_day_tutor[(x.day, x.tutor_id)].append(x)
        by_day_group[(x.day, x.group_id)].append(x)
    for owner, items in {**by_day_tutor, **by_day_group}.items():
        items.sort(key=lambda r: r.start_index)
        for p, q in zip(items, items[1:]):
            if p.delivery_mode != q.delivery_mode:
                # adjacency = within 30 min of each other
                if 0 <= q.start_index - (p.start_index + p.duration_slots) <= 1:
                    report.soft.append(Violation(
                        code="S1", severity="soft",
                        weight=WEIGHTS["S1_mode_switch"],
                        message=f"Mode switch {p.delivery_mode}→{q.delivery_mode} "
                                f"adjacent for {owner[1]} on {p.day}",
                        affected=[p.activity_id, q.activity_id],
                    ))

    # S2 — tutor idle gaps > 2h
    for (day, tid), items in by_day_tutor.items():
        items.sort(key=lambda r: r.start_index)
        for p, q in zip(items, items[1:]):
            gap_slots = q.start_index - (p.start_index + p.duration_slots)
            gap_hours = gap_slots * 0.5
            if gap_hours > 2:
                excess = gap_hours - 2
                report.soft.append(Violation(
                    code="S2", severity="soft",
                    weight=int(WEIGHTS["S2_tutor_gap"] * excess * 2),
                    message=f"Tutor {tid} has {gap_hours:.1f}h idle gap on {day}",
                    affected=[p.activity_id, q.activity_id],
                ))

    # S3 — group consecutive hours > 4
    for (day, gid), items in by_day_group.items():
        items.sort(key=lambda r: r.start_index)
        run_start = None
        run_end = None
        run_acts: list[str] = []
        for x in items:
            if run_end is None or x.start_index > run_end:
                # close previous run
                if run_start is not None:
                    _flag_long_run(report, gid, day, run_start, run_end, run_acts)
                run_start = x.start_index
                run_end = x.start_index + x.duration_slots
                run_acts = [x.activity_id]
            else:
                run_end = max(run_end, x.start_index + x.duration_slots)
                run_acts.append(x.activity_id)
        if run_start is not None:
            _flag_long_run(report, gid, day, run_start, run_end, run_acts)

    # S4 — short campus day (1-2 contact hours) per group
    for (day, gid), items in by_day_group.items():
        f2f_minutes = sum(x.duration_slots * 30 for x in items if x.delivery_mode == "f2f")
        if 0 < f2f_minutes <= 120:
            report.soft.append(Violation(
                code="S4", severity="soft",
                weight=WEIGHTS["S4_short_day"],
                message=f"Group {gid} only has {f2f_minutes//60}h on campus on {day}",
                affected=[x.activity_id for x in items if x.delivery_mode == "f2f"],
            ))

    # S5 — online classes for a programme should all sit on Mon or Tue
    online_days_by_prog: dict[str, set[str]] = defaultdict(set)
    online_per_prog: dict[str, list[Assignment]] = defaultdict(list)
    for x in a:
        if x.delivery_mode in ("online_sync", "online_async"):
            prog = x.course_code[:3]
            online_days_by_prog[prog].add(x.day)
            online_per_prog[prog].append(x)
    for prog, days in online_days_by_prog.items():
        # offending = any online class outside Mon/Tue
        bad = [x for x in online_per_prog[prog] if x.day not in ("Mon", "Tue")]
        # also penalise spreading across Mon AND Tue
        spread_penalty = 1 if {"Mon", "Tue"}.issubset(days) else 0
        for x in bad:
            report.soft.append(Violation(
                code="S5", severity="soft",
                weight=WEIGHTS["S5_online_day"],
                message=f"Online class {x.activity_id} not on Mon/Tue (programme {prog})",
                affected=[x.activity_id],
            ))
        if spread_penalty:
            report.soft.append(Violation(
                code="S5", severity="soft",
                weight=WEIGHTS["S5_online_day"] // 2,
                message=f"Programme {prog} online classes split across Mon and Tue",
                affected=[x.activity_id for x in online_per_prog[prog]],
            ))

    return report


def _flag_long_run(report: ViolationReport, gid: str, day: str,
                   start: int, end: int, acts: list[str]):
    hours = (end - start) * 0.5
    if hours > 4:
        excess = hours - 4
        report.soft.append(Violation(
            code="S3", severity="soft",
            weight=int(WEIGHTS["S3_long_block"] * excess * 2),
            message=f"Group {gid} has {hours:.1f}h consecutive teaching on {day}",
            affected=acts,
        ))
