"""
DSC2204 Timetabling — Streamlit web frontend.

Run with:
    streamlit run app.py
"""
from __future__ import annotations
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

            # Save uploaded workbook
            ws_path = tmp / "Timetable.xlsx"
            ws_path.write_bytes(uploaded_bytes)

            rooms_path = str(DEFAULT_ROOMS) if DEFAULT_ROOMS.exists() else None

            # ---- Load data -------------------------------------------------
            with st.spinner("Loading workbook…"):
                try:
                    from src.data_loader import load_from_worksheet
                    universe = load_from_worksheet(str(ws_path), rooms_inputs=rooms_path)
                except Exception as exc:
                    st.error(f"Failed to load workbook: {exc}")
                    st.stop()

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

                    for activity in universe.all_activities():
                        remark = (activity.notes or "").strip()
                        if not remark:
                            continue
                        tutor = tutor_map.get(activity.tutor_id)
                        if tutor is None:
                            continue
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
                        st.info(f"Parsed **{parsed}** remarks → constraints injected")
                    for w in remark_warnings:
                        st.warning(f"⚠️ Room pin issue: {w}")
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

        dl_col1, dl_col2 = st.columns(2)

        with dl_col1:
            st.download_button(
                "📊 timetable.xlsx (detailed)",
                data=st.session_state["dl_timetable_xlsx"],
                file_name="timetable.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            st.download_button(
                "📄 timetable.json",
                data=st.session_state["dl_timetable_json"],
                file_name="timetable.json",
                mime="application/json",
                use_container_width=True,
            )

        with dl_col2:
            st.download_button(
                "📊 results.xlsx (summary)",
                data=st.session_state["dl_results_xlsx"],
                file_name="results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            st.download_button(
                "🌐 schedule_output.html",
                data=st.session_state["dl_schedule_html"],
                file_name="schedule_output.html",
                mime="text/html",
                use_container_width=True,
            )

        st.download_button(
            "📄 violations.json",
            data=st.session_state["dl_violations_json"],
            file_name="violations.json",
            mime="application/json",
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
