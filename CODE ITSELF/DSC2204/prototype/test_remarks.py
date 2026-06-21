"""
Quick smoke-test for src/remarks_parser.py
Run from the prototype/ directory:
    python test_remarks.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.models import Activity, ActivityType, DeliveryMode, Tutor

# ── helpers ──────────────────────────────────────────────────────────────────

def make_activity(notes=""):
    return Activity(
        course_code="DSC1001",
        activity_type=ActivityType.TUTORIAL,
        delivery_mode=DeliveryMode.F2F,
        duration_slots=2,
        weeks=list(range(1, 14)),
        tutor_id="T001",
        group_id="DSC1001/T1",
        size=30,
        notes=notes,
    )

def make_tutor():
    return Tutor(id="T001", name="Test Tutor")

def run(label: str, remark: str):
    print(f"\n{'─'*60}")
    print(f"TEST : {label}")
    print(f"INPUT: {remark!r}")
    act = make_activity()
    tut = make_tutor()
    from src.remarks_parser import parse_remarks
    parse_remarks(remark, act, tut, [tut])
    print(f"  activity.fixed_day         = {act.fixed_day}")
    print(f"  activity.fixed_start_index = {act.fixed_start_index}")
    print(f"  activity.weeks             = {act.weeks}")
    print(f"  activity.notes             = {act.notes!r}")
    print(f"  tutor.availability         = {tut.availability}")

# ── test cases ────────────────────────────────────────────────────────────────

run("pin — explicit day + time",       "Monday, 10am-12pm")
run("pin — casual phrasing",           "pls schedule on thursdays morning")
run("pin — date with day name",        "7 Nov (Friday), 2-4pm")
run("availability — named day window", "AF can only teach on Fridays, 2-4pm")
run("availability — no day given",     "not available before 10am")
run("availability — vague preference", "prefer afternoons only")
run("skip_week",                       "not available week 7")
run("skip multiple weeks",             "skip weeks 3 and 5")
run("unresolved",                      "check with admin")
