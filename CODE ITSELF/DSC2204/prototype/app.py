"""
DSC2204 Timetabling — Streamlit web frontend.

Run with:
    streamlit run app.py
"""
from __future__ import annotations
import copy
import sys
import tempfile
import json
import hashlib
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import ortools  # noqa: F401
    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False

# ---------------------------------------------------------------------------
# Pre-solve helpers
# ---------------------------------------------------------------------------

def _split_week_pinned_activities(universe) -> list[str]:
    """Split activities that have different days/times per week into separate
    single-week activities so the solver can honour per-week scheduling.

    Returns a list of human-readable info strings for UI display.
    """
    info: list[str] = []
    for course in universe.courses:
        to_remove = []
        to_add = []
        for activity in course.activities:
            pins = getattr(activity, "week_pins", {})
            if not pins:
                continue

            unique_days = {d for (d, _) in pins.values() if d}
            unique_slots = {s for (_, s) in pins.values() if s is not None}

            if len(unique_days) <= 1 and len(unique_slots) <= 1:
                # All pins agree on day and slot — just apply once and clear
                if unique_days:
                    activity.fixed_day = unique_days.pop()
                if unique_slots:
                    activity.fixed_start_index = unique_slots.pop()
                activity.week_pins = {}
                continue

            # Different days or slots per week — must split into individual activities
            all_pinned_weeks = set(pins.keys())
            remaining_weeks = [w for w in activity.weeks if w not in all_pinned_weeks]

            for wk, (day, slot) in pins.items():
                new_act = copy.deepcopy(activity)
                new_act.weeks = [wk]
                new_act.week_pins = {}
                new_act.weeks_from_default = False
                new_act.fixed_day = day
                new_act.fixed_start_index = slot
                to_add.append(new_act)

            if remaining_weeks:
                activity.weeks = remaining_weeks
                activity.week_pins = {}
                activity.fixed_day = None
                activity.fixed_start_index = None
            else:
                to_remove.append(activity)

            info.append(
                f"{activity.course_code} {activity.activity_type.value}: "
                f"split into {len(pins)} single-week activities "
                f"(weeks {sorted(pins.keys())}) due to per-week day/time pins"
            )

        for a in to_remove:
            course.activities.remove(a)
        for a in to_add:
            course.activities.append(a)

    return info


def _apply_common_modules(universe, registry) -> list[str]:
    """Merge common-module lecture activities from multiple programmes into one
    combined activity and set shared_cohorts on it so clash detection covers
    every participating cohort.

    Returns a list of info messages describing what was merged.
    """
    from src.models import ActivityType
    from collections import defaultdict

    info: list[str] = []
    courses_by_code = {c.code: c for c in universe.courses}

    # Build index: (code_upper, year) -> list of (course, activity)
    idx: dict = defaultdict(list)
    for course in universe.courses:
        for act in course.activities:
            idx[(act.course_code.upper(), course.year)].append((course, act))

    processed: set = set()

    for g in registry.groups:
        key = (g.codes, g.year)
        if key in processed:
            continue
        processed.add(key)

        # Gather lecture activities for every alias code in this group
        candidates: list = []
        for code in g.codes:
            candidates.extend(idx.get((code, g.year), []))

        # Filter to only programmes listed in the common module (unless _ALL_)
        if "_ALL_" not in g.programmes:
            candidates = [
                (c, a) for c, a in candidates
                if c.programme.upper() in g.programmes
            ]

        # Group by programme — keep ONE representative per programme.
        # Multiple activities from the SAME programme (e.g. two DSC lecture
        # rows for different tutors/weeks) must NOT be merged across programmes.
        by_prog: dict = defaultdict(list)
        for c, a in candidates:
            by_prog[c.programme.upper()].append((c, a))

        # One representative per programme (first activity of each)
        prog_reps = [(pairs[0]) for pairs in by_prog.values()]

        # Only act when multiple DIFFERENT programmes share this module
        if len(prog_reps) <= 1:
            continue

        # Split representatives by activity type — only merge whole-cohort types
        by_type: dict = defaultdict(list)
        for c, a in prog_reps:
            by_type[a.activity_type].append((c, a))

        MERGE_TYPES = {
            ActivityType.LECTURE, ActivityType.LECTORIAL,
            ActivityType.SEMINAR, ActivityType.WORKSHOP,
        }

        for atype, pairs in by_type.items():
            if atype not in MERGE_TYPES or len(pairs) <= 1:
                continue

            def _cohort_label(course, yr=g.year):
                return f"{course.programme.upper()}/Y{yr}"

            all_cohort_labels = [_cohort_label(c) for c, _ in pairs]
            combined_size = sum(a.size for _, a in pairs)

            # Keep the first (primary) activity, update its size + shared_cohorts
            primary_course, primary_act = pairs[0]
            primary_act.size = combined_size
            primary_act.shared_cohorts = [
                lbl for lbl in all_cohort_labels
                if lbl != _cohort_label(primary_course)
            ]

            # Remove the duplicate lecture from every other programme
            merged_progs = [primary_course.programme]
            for c, a in pairs[1:]:
                try:
                    c.activities.remove(a)
                    merged_progs.append(c.programme)
                except ValueError:
                    pass

            # One human-readable message per common module × activity type
            prog_list = " and ".join(
                f"{p} Year {g.year}" for p in merged_progs
            )
            info.append(
                f"{primary_act.course_code} {atype.value} — "
                f"{prog_list} both take this module, so their classes are "
                f"combined into one shared session ({combined_size} students total)."
            )

    return info


def _deduplicate_activities(universe) -> list[str]:
    """Remove duplicate Activity objects that have the same (activity_type, group_id)
    within the same course and the same week set.

    This happens when a module appears more than once in the spreadsheet for the
    same Prog/Yr (e.g. INF1003 listed twice for DSC/Y1 with two tutors but the
    data_loader still creates T1/T2 for each row).  Duplicate activities share
    the same .id, causing the solver to add self-contradicting H3 constraints.
    """
    fixes: list[str] = []
    for course in universe.courses:
        seen: dict = {}          # (atype, group_id, frozenset(weeks)) -> first act
        to_remove: list = []
        for act in course.activities:
            key = (act.activity_type, act.group_id, frozenset(act.weeks))
            if key in seen:
                to_remove.append(act)
                fixes.append(
                    f"Removed duplicate {act.course_code} {act.activity_type.value} "
                    f"group_id={act.group_id} ({course.programme}/Y{course.year})"
                )
            else:
                seen[key] = act
        for act in to_remove:
            course.activities.remove(act)
    return fixes

def _fmt_act_dedup(msg: str) -> str:
    """Turn a raw dedup log line into plain English."""
    import re
    m = re.match(
        r"Removed duplicate (\S+) (\S+) group_id=([^\s]+) \(([^/]+)/Y(\d+)\)", msg
    )
    if m:
        code, atype, gid, prog, yr = m.groups()
        label = gid.split("/")[-1]
        label_str = "all-students group" if label.lower() == "all" else f"group {label}"
        return (
            f"Duplicate row removed: {code} {atype} ({label_str}) "
            f"for {prog} Year {yr} — spreadsheet listed it twice, kept one copy."
        )
    return msg


def _deduplicate_group_ids(universe) -> list[str]:
    """When multiple programmes share the same module code (common modules),
    their tutorials and labs can end up with identical group_ids
    (e.g. both DSC and ICT get 'INF1003/T1').  Identical group_ids produce
    identical activity IDs, which causes the solver to add self-contradicting
    constraints and become INFEASIBLE.

    Fix: the first programme keeps the original group_id; every subsequent
    programme gets its group_id prefixed with the programme abbreviation.
    """
    claimed: dict[str, str] = {}  # group_id -> programme that owns it
    fixes: list[str] = []

    for course in universe.courses:
        prog = course.programme.upper()
        for act in course.activities:
            gid = act.group_id
            if gid not in claimed:
                claimed[gid] = prog
            elif claimed[gid] != prog:
                new_gid = f"{prog}_{gid}"
                act.group_id = new_gid
                fixes.append(
                    f"Renamed group_id '{gid}' -> '{new_gid}' "
                    f"({prog} vs {claimed[gid]}, course {act.course_code})"
                )

    return fixes


def _check_pin_conflicts(universe, tutor_map: dict) -> list[str]:
    """Detect activities where the remark day pin conflicts with the tutor's
    availability, and return a list of human-readable warning strings.
    """
    warnings_out: list[str] = []
    for activity in universe.all_activities():
        day = getattr(activity, "fixed_day", None)
        if not day:
            continue
        tutor = tutor_map.get(activity.tutor_id)
        if not tutor:
            continue
        day_slots = tutor.availability.get(day)
        if day_slots is not None and len(day_slots) == 0:
            warnings_out.append(
                f"{activity.course_code} {activity.activity_type.value}: "
                f"remark pins to {day} but tutor {tutor.name!r} is blocked "
                f"all day on {day}. Pin wins — solver may be infeasible."
            )
        elif day_slots is not None:
            fs = getattr(activity, "fixed_start_index", None)
            dur = getattr(activity, "duration_slots", 1)
            if fs is not None:
                required = set(range(fs, fs + dur))
                if not required.issubset(set(day_slots)):
                    warnings_out.append(
                        f"{activity.course_code} {activity.activity_type.value}: "
                        f"remark pins {day} slot {fs} but tutor {tutor.name!r} "
                        f"is not available for the full duration. Pin wins."
                    )
    return warnings_out


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="DSC Timetable Scheduler",
    page_icon="📅",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Sidebar — solver settings
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Settings")

    solver_options = []
    if HAS_ORTOOLS:
        solver_options.append("CP-SAT (OR-Tools)")
    solver_options.append("Heuristic (built-in)")

    solver_choice = st.selectbox("Solver", solver_options)
    time_limit = st.slider("Time limit (seconds)", min_value=10, max_value=300, value=60)

    if not HAS_ORTOOLS:
        st.warning("ortools not installed — heuristic solver only.")

    st.divider()
    st.caption(
        "Rooms are sourced from the **Rooms** tab inside the uploaded workbook, "
        "or from `inputs.xlsx` next to it if no Rooms tab exists."
    )

# ---------------------------------------------------------------------------
# Main header
# ---------------------------------------------------------------------------
st.title("📅 DSC Timetable Scheduler")
st.caption("Upload your **Timetable.xlsx** to generate an optimised conflict-free schedule.")

# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------
uploaded = st.file_uploader(
    "Upload Timetable.xlsx",
    type=["xlsx"],
    help="The workbook containing the Module, Tutors, Rooms, and Calendar sheets.",
)

DEFAULT_ROOMS = ROOT.parent / "inputs.xlsx"
TEMPLATE2_PATH = ROOT.parent / "template 2.xlsx"

def _clear_result_state() -> None:
    for key in (
        "report_summary",
        "hard_violations",
        "soft_violations",
        "schedule_html",
        "dl_timetable_xlsx",
        "dl_results_xlsx",
        "dl_timetable_json",
        "dl_violations_json",
        "dl_schedule_html",
        "dl_template2",
    ):
        st.session_state.pop(key, None)

if uploaded is not None:
    uploaded_bytes = uploaded.getvalue()
    uploaded_fingerprint = hashlib.sha256(uploaded_bytes).hexdigest()

    if st.session_state.get("uploaded_fingerprint") not in (None, uploaded_fingerprint):
        _clear_result_state()
    st.session_state["uploaded_fingerprint"] = uploaded_fingerprint

    run_btn = st.button("🚀 Generate Schedule", type="primary")

    if run_btn:
        with tempfile.TemporaryDirectory() as _tmp:
            tmp = Path(_tmp)

            # Save uploaded workbook (also keep a debug copy)
            ws_path = tmp / "Timetable.xlsx"
            ws_path.write_bytes(uploaded_bytes)
            (ROOT / "debug_upload.xlsx").write_bytes(uploaded_bytes)

            rooms_path = str(DEFAULT_ROOMS) if DEFAULT_ROOMS.exists() else None

            # ---- Load data -------------------------------------------------
            with st.spinner("Loading workbook…"):
                try:
                    from src.data_loader import load_from_worksheet
                    universe = load_from_worksheet(str(ws_path), rooms_inputs=rooms_path)
                except Exception as exc:
                    st.error(f"Failed to load workbook: {exc}")
                    st.stop()

            # ---- Deduplicate same-course duplicate activities ---------------
            _act_dedup = _deduplicate_activities(universe)

            # ---- Common modules merge --------------------------------------
            _cm_path = ROOT.parent / "Common Modules(Sheet1).csv"
            _cm_info: list[str] = []
            _dedup_info: list[str] = []
            if _cm_path.exists():
                from src.common_modules import load as _load_cm
                _cm_registry = _load_cm(str(_cm_path))
                _cm_info = _apply_common_modules(universe, _cm_registry)
                _dedup_info = _deduplicate_group_ids(universe)

            _all_cm = _cm_info + _dedup_info
            _inf_acts = [(c.programme, c.year, a) for c in universe.courses
                         for a in c.activities if a.course_code == "INF1003"]
            _prep_parts = []
            if _act_dedup:
                _prep_parts.append(f"{len(_act_dedup)} duplicate row(s) removed")
            if _cm_info:
                _prep_parts.append(f"{len(_cm_info)} shared module(s) combined")
            if _dedup_info:
                _prep_parts.append(f"{len(_dedup_info)} group label(s) renamed")
            _prep_title = "Schedule preparation: " + (", ".join(_prep_parts) or "nothing to clean up")
            with st.expander(_prep_title, expanded=bool(_act_dedup or _all_cm)):
                if _act_dedup:
                    st.markdown("**Duplicate spreadsheet rows removed**")
                    for msg in _act_dedup:
                        st.caption("• " + _fmt_act_dedup(msg))
                if _cm_info:
                    st.markdown("**Shared modules combined into joint sessions**")
                    for msg in _cm_info:
                        st.caption("• " + msg)
                if _dedup_info:
                    st.markdown("**Group labels renamed to avoid clashes**")
                    for msg in _dedup_info:
                        st.caption("• " + msg)
                if not (_act_dedup or _all_cm):
                    st.caption("No changes needed — all data looks clean.")

            n_activities = sum(len(c.activities) for c in universe.courses)
            st.info(
                f"Loaded **{len(universe.courses)}** courses · "
                f"**{n_activities}** activities · "
                f"**{len(universe.rooms)}** rooms · "
                f"**{len(universe.tutors)}** tutors"
            )

            # ---- Parse remarks (LLM) ---------------------------------------
            with st.spinner("Parsing Data...."):
                try:
                    from src.remarks_parser import parse_remarks, is_llm_available
                    from src.models import Room, RoomType

                    if not is_llm_available():
                        st.warning(
                            "⚠️ LLM unavailable — no API key or `anthropic` package not installed. "
                            "Remarks are being processed with regex fallback only: room pins may "
                            "over-capture and non-pin constraints (block_day, availability, "
                            "skip_week) will be ignored. Set ANTHROPIC_API_KEY in "
                            "`prototype/.env` to fix this."
                        )

                    tutor_map = {t.id: t for t in universe.tutors}
                    parsed = 0
                    remark_warnings = []

                    _dummy_tutor_cache: dict = {}

                    for activity in universe.all_activities():
                        remark = (activity.notes or "").strip()
                        if not remark:
                            continue
                        tutor = tutor_map.get(activity.tutor_id)
                        if tutor is None:
                            # Tutor not found — create a temporary stand-in so
                            # parse_remarks can still extract date/week/room pins
                            # from the remark. Availability mutations on this
                            # object are discarded (it is never added to universe).
                            from src.models import Tutor as _Tutor
                            tutor = _dummy_tutor_cache.setdefault(
                                activity.tutor_id,
                                _Tutor(id=activity.tutor_id, name=activity.tutor_id),
                            )
                            remark_warnings.append(
                                f"{activity.id}: tutor {activity.tutor_id!r} not found "
                                f"in tutor list — date/room pins extracted, "
                                f"availability constraints ignored."
                            )
                        try:
                            parse_remarks(remark, activity, tutor, universe.tutors)
                            parsed += 1
                        except Exception as exc:
                            remark_warnings.append(f"{activity.id}: {exc}")

                    # Register any venue-pinned rooms that are not in the formal room list.
                    existing_ids = {r.id for r in universe.rooms}
                    event_space_ids: set[str] = set()
                    for activity in universe.all_activities():
                        rid = getattr(activity, "fixed_room_id", None)
                        if rid and rid not in existing_ids:
                            # Online activities must use the virtual room — an unrecognised
                            # room pin (e.g. LLM returns "Virtual room" instead of "VIRTUAL")
                            # would create a fake physical room and trigger an H5 violation.
                            # Clear the bad pin; the solver will assign the virtual room.
                            if activity.delivery_mode.value in ("online_sync", "online_async"):
                                activity.fixed_room_id = None
                            else:
                                universe.rooms.append(Room(
                                    id=rid, name=rid, capacity=10_000,
                                    room_type=RoomType.OTHER, zone="Event Space",
                                ))
                                existing_ids.add(rid)
                                event_space_ids.add(rid)

                    # Light validation for pinned rooms so the dashboard can report bad pins.
                    room_by_id = {r.id: r for r in universe.rooms}
                    for activity in universe.all_activities():
                        rid = getattr(activity, "fixed_room_id", None)
                        if not rid or rid in event_space_ids:
                            continue
                        room = room_by_id.get(rid)
                        if room is None:
                            remark_warnings.append(
                                f"Pinned room {rid!r} not found in room list for activity {activity.id}"
                            )
                        elif activity.delivery_mode.value == "f2f" and room.is_virtual:
                            remark_warnings.append(
                                f"{activity.id}: f2f activity pinned to virtual room {rid!r}"
                            )

                    if parsed:
                        st.info(f"Parsed **{parsed}** remarks -> constraints injected")
                    for w in remark_warnings:
                        st.warning(f"Room pin issue: {w}")

                    # Split activities with per-week different day/time pins
                    split_info = _split_week_pinned_activities(universe)
                    for msg in split_info:
                        st.info(f"Split: {msg}")

                    # Warn when a remark pin directly conflicts with tutor availability
                    conflict_warns = _check_pin_conflicts(universe, tutor_map)
                    for w in conflict_warns:
                        st.warning(f"Constraint conflict: {w}")

                except Exception as exc:
                    st.warning(f"Remarks parsing skipped: {exc}")

            # ---- Solve -----------------------------------------------------
            with st.spinner(f"Solving with {solver_choice}…"):
                try:
                    if "CP-SAT" in solver_choice:
                        from src.solver_cpsat import solve
                        try:
                            timetable = solve(universe, time_limit_s=time_limit, verbose=False)
                        except Exception as exc:
                            st.warning(f"CP-SAT failed ({exc}); falling back to heuristic solver.")
                            from src.solver_heuristic import solve as heur_solve
                            timetable = heur_solve(universe, time_limit_s=time_limit, verbose=False)
                    else:
                        from src.solver_heuristic import solve
                        timetable = solve(universe, time_limit_s=time_limit, verbose=False)
                except Exception as exc:
                    st.error(f"Solver failed: {exc}")
                    st.stop()

            # ---- Validate --------------------------------------------------
            from src.constraint_engine import check
            report = check(timetable, universe)

            # ---- Export ----------------------------------------------------
            out_dir = tmp / "output"
            out_dir.mkdir()

            from src.exporter import (
                write_json, write_dashboard,
                write_timetable_xlsx, write_simple_results, write_template2,
            )
            write_json(timetable, report, out_dir)
            write_dashboard(timetable, report, universe, out_dir / "schedule_output.html")
            write_timetable_xlsx(timetable, universe, out_dir / "timetable.xlsx")
            write_simple_results(timetable, universe, out_dir / "results.xlsx")

            # Write into template 2 if the file exists next to the prototype folder
            t2_out = out_dir / "template2_output.xlsx"
            if TEMPLATE2_PATH.exists():
                write_template2(timetable, universe, str(TEMPLATE2_PATH), str(t2_out))
            else:
                t2_out = None

            # ---- Store results in session state ----------------------------
            st.session_state["report_summary"] = {
                "feasible": report.is_feasible,
                "hard_count": len(report.hard),
                "soft_count": len(report.soft),
                "soft_score": report.soft_score,
            }
            st.session_state["hard_violations"] = [
                {"code": v.code, "message": v.message} for v in report.hard
            ]
            st.session_state["soft_violations"] = [
                {"code": v.code, "message": v.message, "weight": v.weight}
                for v in report.soft
            ]
            st.session_state["schedule_html"] = (
                out_dir / "schedule_output.html"
            ).read_text(encoding="utf-8")
            st.session_state["dl_timetable_xlsx"] = (
                out_dir / "timetable.xlsx"
            ).read_bytes()
            st.session_state["dl_results_xlsx"] = (
                out_dir / "results.xlsx"
            ).read_bytes()
            st.session_state["dl_timetable_json"] = (
                out_dir / "timetable.json"
            ).read_text(encoding="utf-8")
            st.session_state["dl_violations_json"] = (
                out_dir / "violations.json"
            ).read_text(encoding="utf-8")
            st.session_state["dl_schedule_html"] = (
                out_dir / "schedule_output.html"
            ).read_bytes()
            st.session_state["dl_template2"] = (
                t2_out.read_bytes() if t2_out and t2_out.exists() else None
            )
            st.session_state["schedule_render_nonce"] = st.session_state.get("schedule_render_nonce", 0) + 1

# ---------------------------------------------------------------------------
# Display results (persists across reruns via session_state)
# ---------------------------------------------------------------------------
if "report_summary" in st.session_state:
    summary = st.session_state["report_summary"]

    st.divider()

    # Status metrics
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Status", "✅ Feasible" if summary["feasible"] else "❌ Infeasible")
    col2.metric("Hard Violations", summary["hard_count"])
    col3.metric("Soft Violations", summary["soft_count"])
    col4.metric("Soft Score", summary["soft_score"])

    # Tabs
    tab_schedule, tab_violations, tab_downloads = st.tabs(
        ["📅 Schedule", "⚠️ Violations", "📥 Downloads"]
    )

    with tab_schedule:
        schedule_html = st.session_state["schedule_html"]
        render_nonce = st.session_state.get("schedule_render_nonce", 0)
        st.components.v1.html(
            f"<!-- render:{render_nonce} -->\n" + schedule_html,
            height=900,
            scrolling=True,
        )

    with tab_violations:
        hard = st.session_state["hard_violations"]
        soft = st.session_state["soft_violations"]

        if hard:
            st.subheader("Hard Violations")
            for v in hard:
                st.error(f"**{v['code']}**: {v['message']}")
        else:
            st.success("No hard violations — schedule is feasible.")

        if soft:
            st.subheader(f"Soft Violations ({len(soft)} total)")
            for v in soft:
                st.warning(f"**{v['code']}** (weight {v['weight']}): {v['message']}")

    with tab_downloads:
        st.subheader("Download Outputs")

        st.download_button(
            "📊 timetable.xlsx (detailed)",
            data=st.session_state["dl_timetable_xlsx"],
            file_name="timetable.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

        t2_bytes = st.session_state.get("dl_template2")
        if t2_bytes:
            st.download_button(
                "📋 template 2.xlsx (SIT format)",
                data=t2_bytes,
                file_name="template 2.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.caption("ℹ️ template 2.xlsx not found next to the prototype folder — SIT export unavailable.")

        # Hidden outputs — functions retained, buttons disabled until needed:
        # results.xlsx      → write_simple_results  / dl_results_xlsx
        # timetable.json    → write_json            / dl_timetable_json
        # violations.json   → write_json            / dl_violations_json
        # schedule_output   → write_dashboard       / dl_schedule_html
