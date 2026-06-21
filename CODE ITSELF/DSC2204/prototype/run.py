"""
DSC2204 Timetabling — CLI entry point.

Default workflow (zero flags needed):
    1. Put your data in   ../Timetable.xlsx
    2. (optional) ../Output.xlsx will be created/refreshed automatically.
    3. Run:               python run.py

Flag overrides:
    python run.py --worksheet "../Timetable.xlsx"
    python run.py --template  "../Output.xlsx"
    python run.py --solver heuristic --time-limit 60 --quiet

Outputs:
    output/timetable.json    — canonical schedule
    output/violations.json   — constraint engine report
    output/schedule_output.html — static schedule snapshot
    output/timetable.xlsx    — detailed multi-sheet output
    output/results.xlsx      — simple one-sheet results
    ../Output.xlsx           — populated SIT template (in place)
"""
from __future__ import annotations
import argparse, sys, os, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.data_loader import load_dsc_universe
from src.constraint_engine import check
from src.exporter import write_json, write_dashboard


def main():
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inputs",   default=str(ROOT.parent / "inputs.xlsx"))
    p.add_argument("--worksheet", default=None)
    p.add_argument("--template",  default=None,
                   help="Path to Output.xlsx (auto-detected at project root).")
    p.add_argument("--ignore-remarks", action="store_true")
    p.add_argument("--modules-xlsx",   default=None)
    p.add_argument("--resources-xlsx", default=None)
    p.add_argument("--solver", choices=["auto","cp-sat","heuristic"], default="auto")
    p.add_argument("--time-limit", type=int, default=60)
    p.add_argument("--seed",       type=int, default=42)
    p.add_argument("--out", default=str(ROOT / "output"))
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    say = (lambda *a, **kw: None) if args.quiet else print
    say("=" * 64); say("DSC2204 Timetabling"); say("=" * 64)

    # Auto-detect the source workbook (Timetable.xlsx, with legacy fallbacks)
    if not args.worksheet and not (args.modules_xlsx and args.resources_xlsx):
        for cand in ("Timetable.xlsx", "DSC timetable.xlsx", "ENG timetable.xlsx"):
            cp = ROOT.parent / cand
            if cp.exists():
                args.worksheet = str(cp); break

    if args.worksheet:
        from src.data_loader import load_from_worksheet
        if not os.path.exists(args.worksheet):
            raise SystemExit(f"worksheet file not found: {args.worksheet}")
        universe = load_from_worksheet(args.worksheet, rooms_inputs=args.inputs,
                                       ignore_remarks=args.ignore_remarks)
        say("Loaded from worksheet: " + args.worksheet)
    elif args.modules_xlsx and args.resources_xlsx:
        universe = load_dsc_universe(args.modules_xlsx, args.resources_xlsx)
    else:
        from src.data_loader import load_from_inputs
        if not os.path.exists(args.inputs):
            raise SystemExit(f"inputs file not found: {args.inputs}")
        universe = load_from_inputs(args.inputs)

    say(f"Loaded {len(universe.courses)} courses, "
        f"{sum(len(c.activities) for c in universe.courses)} activities, "
        f"{len(universe.rooms)} rooms, {len(universe.tutors)} tutors")

    if not args.ignore_remarks:
        from src.remarks_parser import parse_all_remarks
        parsed, remark_warnings = parse_all_remarks(universe)
        say(f"Remarks parsed: {parsed} constraints injected from Column K")
        for w in remark_warnings:
            say(f"  WARNING (room pin): {w}")

    solver = args.solver
    if solver == "auto":
        try:
            import ortools  # noqa: F401
            solver = "cp-sat"
        except ImportError:
            solver = "heuristic"
    say(f"Solver: {solver}")

    if solver == "cp-sat":
        from src.solver_cpsat import solve as cpsat_solve
        timetable = cpsat_solve(universe, time_limit_s=args.time_limit, verbose=not args.quiet)
    else:
        from src.solver_heuristic import solve as heur_solve
        timetable = heur_solve(universe, time_limit_s=args.time_limit, seed=args.seed, verbose=not args.quiet)

    say(f"\nGenerated {len(timetable.assignments)} assignments.")
    report = check(timetable, universe)
    say(f"Validation: hard={len(report.hard)} soft={len(report.soft)} soft_score={report.soft_score}")
    if not report.is_feasible:
        for v in report.hard[:10]:
            say(f"  HARD: {v.code}: {v.message}")
        if len(report.hard) > 10:
            say(f"  ... ({len(report.hard)-10} more)")

    out = Path(args.out)
    from src.exporter import (write_timetable_xlsx, write_back_staff,
                              write_simple_results, write_template2)

    def safe(label, fn):
        try:
            fn(); say("  wrote: " + label)
        except PermissionError:
            say("  skipped (open in Excel?): " + label)
        except Exception as e:
            say("  failed: " + label + ": " + str(e))

    say("")
    safe(str(out / "timetable.json"), lambda: write_json(timetable, report, out))
    safe(str(out / "schedule_output.html"), lambda: write_dashboard(timetable, report, universe, out / "schedule_output.html"))
    safe(str(out / "timetable.xlsx"), lambda: write_timetable_xlsx(timetable, universe, out / "timetable.xlsx"))
    safe(str(out / "results.xlsx"),   lambda: write_simple_results(timetable, universe, out / "results.xlsx"))

    # Output.xlsx sync — canonical name, with legacy fallbacks; if nothing
    # exists yet, create ../Output.xlsx fresh.
    template_path = args.template
    if not template_path:
        for cand in (ROOT.parent / "Output.xlsx",
                     ROOT.parent / "output.xlsx",
                     ROOT.parent / "template 2.xlsx",
                     ROOT.parent / "Template 2.xlsx"):
            if cand.exists():
                template_path = str(cand); break
        if not template_path:
            template_path = str(ROOT.parent / "Output.xlsx")
    safe(template_path + "  (synced in place)",
         lambda: write_template2(timetable, universe, template_path, template_path))

    if args.worksheet:
        try:
            if write_back_staff(timetable, args.worksheet):
                say("  filled Staff columns in " + args.worksheet)
        except Exception as e:
            say("  (could not write staff back: " + str(e) + ")")
    return 0 if report.is_feasible else 2


if __name__ == "__main__":
    sys.exit(main())
