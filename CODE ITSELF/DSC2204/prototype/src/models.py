"""
DSC2204 Integrative Team Project — Timetabling
Segment 1: Data Modelling

Pure-Python dataclasses representing every scheduling resource and the
generated timetable.  Designed to be JSON-serialisable so the CP-SAT
solver, the heuristic fallback, the constraint engine, and the HTML
dashboard all consume identical structures.

Operations-analyst view of the entities:
    Course      — academic offering owned by a programme
    Activity    — one delivery event of a course (Lecture, Tutorial, ...)
    ClassSession— one schedulable instance (an Activity for one Group)
    Room        — physical or virtual space with a capacity
    Tutor       — staff member who can be double-booked => hard violation
    Group       — student grouping that follows a course/activity
    TimeSlot    — 30-minute granular slot within a teaching day
    Calendar    — list of teaching weeks, public holidays, etc.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Tuple
from enum import Enum
import json


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class DeliveryMode(str, Enum):
    F2F = "f2f"
    ONLINE_SYNC = "online_sync"
    ONLINE_ASYNC = "online_async"

    @classmethod
    def parse(cls, raw: str) -> "DeliveryMode":
        s = (raw or "").strip().lower()
        if s in ("f2f", "face-to-face", "facetoface"):
            return cls.F2F
        if "async" in s:
            return cls.ONLINE_ASYNC
        if "sync" in s or "online" in s:
            return cls.ONLINE_SYNC
        raise ValueError(f"Unknown delivery mode: {raw!r}")


class ActivityType(str, Enum):
    LECTURE = "Lecture"
    TUTORIAL = "Tutorial"
    LAB = "Laboratory"
    WORKSHOP = "Workshop"
    SEMINAR = "Seminar"
    QUIZ = "Quiz"
    PRACTICUM = "Practicum"
    LECTORIAL = "Lectorial"
    ASSIGNMENT = "Assignment"
    CLINICAL = "Clinical"
    DISCUSSION = "Discussion"
    FIELD_STUDIES = "Field_Studies"
    FIELDWORK = "Fieldwork"
    INDEPENDENT_STUDY = "Independent_Study"
    PREPARATORY_WORK = "Preparatory_Work"
    PROJECTS = "Projects"
    RESEARCH = "Research"
    SELF_STUDY = "Self_Study"
    SUPERVISION = "Supervision"
    OTHER = "Other"

    @classmethod
    def parse(cls, raw: str) -> "ActivityType":
        s = (raw or "").strip().title()
        for m in cls:
            if m.value.lower() == s.lower():
                return m
        return cls.OTHER


class RoomType(str, Enum):
    LECTURE_THEATRE = "lecture_theatre"
    SEMINAR_ROOM = "seminar_room"
    COMPUTER_LAB = "computer_lab"
    LABORATORY = "laboratory"
    VIRTUAL = "virtual"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Resource models
# ---------------------------------------------------------------------------

@dataclass
class Tutor:
    id: str            # e.g. "A100909"
    name: str          # e.g. "DAVID LIN WEIDONG"
    # Optional availability mask (day -> list of allowed slot indices).
    # Empty dict => available everywhere.
    availability: Dict[str, List[int]] = field(default_factory=dict)


@dataclass
class Room:
    id: str            # e.g. "DV-AP-LT2C"
    name: str
    capacity: int
    room_type: RoomType
    zone: str = ""

    @property
    def is_virtual(self) -> bool:
        return self.room_type == RoomType.VIRTUAL


@dataclass
class Group:
    """A student-group instance for a particular activity, e.g. DSC1001 T1."""
    id: str            # globally unique e.g. "DSC1001/T1"
    course_code: str
    label: str         # e.g. "T1", "L2", "All"
    size: int


@dataclass
class TimeSlot:
    """30-minute slot anchored on a weekday."""
    day: str           # "Mon"…"Fri"
    index: int         # 0-based slot index in the day, 0 = 0700
    start_min: int     # minutes from 00:00, e.g. 7*60
    end_min: int

    @property
    def label(self) -> str:
        h, m = divmod(self.start_min, 60)
        return f"{h:02d}{m:02d}"


@dataclass
class Activity:
    """A teaching event of a course (lecture / tutorial / lab / ...)."""
    course_code: str
    activity_type: ActivityType
    delivery_mode: DeliveryMode
    duration_slots: int      # in 30-min units
    weeks: List[int]         # teaching weeks the activity runs
    tutor_id: str
    group_id: str            # student group attending
    size: int                # number of attendees (for room capacity check)
    fixed_day: Optional[str] = None       # if scheduling is hard-pinned
    fixed_start_index: Optional[int] = None
    fixed_room_id: Optional[str] = None   # if a specific venue is required
    room_count: int = 1                   # number of rooms needed simultaneously
    is_evening: bool = False              # True → MSc/evening; 18:00 end constraint relaxed
    notes: str = ""
    co_tutor_ids: List[str] = field(default_factory=list)  # co-teachers (Staff 2, ...)
    weeks_from_default: bool = False     # True when Teaching Weeks cell was blank
    room_type_req: Optional[str] = None  # required room type from remarks, e.g. "seminar_room"
    room_cap_req: Optional[int] = None   # minimum seat count required from remarks
    # Per-week (day, start_slot_index) set by remarks for multi-date activities.
    # Populated when different weeks need different days/times (e.g. "13 Oct Mon, 27 Oct Tue").
    # Pre-solve, the solver splits these into separate single-week activities.
    week_pins: Dict[int, tuple] = field(default_factory=dict)
    # Common-module cohorts: additional "PROG/Y{year}" labels beyond the primary
    # course programme.  Set by _apply_common_modules() so the solver and checker
    # extend clash detection to every participating cohort.
    shared_cohorts: List[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        base = f"{self.course_code}/{self.activity_type.value}/{self.group_id}"
        # Disambiguate when the SAME (course, type, group) appears across
        # multiple rows in inputs.xlsx because the teaching weeks are split
        # between different tutors — e.g., DSC1001 Tutorial T1 taught by
        # TAN MEOW LOONG in weeks 1-9, YANG SHANSHAN in week 10, and
        # DAVID LIN WEIDONG in weeks 11-13.  Each of those is a distinct
        # scheduling node, so we tag the id with the smallest teaching
        # week.  Activities that run every week still get the legacy id.
        if not self.weeks:
            return base
        if len(self.weeks) >= 13 and min(self.weeks) == 1:
            return base
        return f"{base}@W{min(self.weeks)}"


@dataclass
class Course:
    code: str
    programme: str           # "DSC"
    year: int                # 1, 2, 3 …
    activities: List[Activity] = field(default_factory=list)


@dataclass
class Calendar:
    teaching_weeks: List[int]                 # e.g. [1..6, 8..13]
    week_dates: Dict[int, str] = field(default_factory=dict)  # optional


# ---------------------------------------------------------------------------
# Schedule (the OUTPUT of the generator)
# ---------------------------------------------------------------------------

@dataclass
class Assignment:
    activity_id: str
    course_code: str
    activity_type: str
    delivery_mode: str
    group_id: str
    tutor_id: str
    tutor_name: str
    room_id: str
    room_name: str
    day: str
    start_index: int
    duration_slots: int
    start_label: str        # e.g. "0900"
    end_label: str          # e.g. "1100"
    weeks: List[int]
    size: int
    co_tutor_ids: List[str] = field(default_factory=list)
    co_tutor_names: List[str] = field(default_factory=list)
    room2_id: str = ""
    room2_name: str = ""
    notes: str = ""
    shared_cohorts: List[str] = field(default_factory=list)


@dataclass
class Timetable:
    assignments: List[Assignment]
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "metadata": self.metadata,
            "assignments": [asdict(a) for a in self.assignments],
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), indent=2, **kw)


# ---------------------------------------------------------------------------
# Universe — bundles every scheduling input together
# ---------------------------------------------------------------------------

@dataclass
class Universe:
    courses: List[Course]
    rooms: List[Room]
    tutors: List[Tutor]
    groups: List[Group]
    time_slots: Dict[str, List[TimeSlot]]   # day -> ordered slots
    calendar: Calendar
    days: List[str] = field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])

    # Convenience accessors -----------------------------------------------

    @property
    def slot_count(self) -> int:
        return len(next(iter(self.time_slots.values())))

    def all_activities(self) -> List[Activity]:
        return [a for c in self.courses for a in c.activities]

    def tutor(self, tid: str) -> Tutor:
        for t in self.tutors:
            if t.id == tid:
                return t
        raise KeyError(tid)

    def room(self, rid: str) -> Room:
        for r in self.rooms:
            if r.id == rid:
                return r
        raise KeyError(rid)

    def group(self, gid: str) -> Group:
        for g in self.groups:
            if g.id == gid:
                return g
        raise KeyError(gid)
