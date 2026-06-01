# DSC2204 Timetabling Prototype

Course: **DSC2204 Integrative Team Project** — Singapore Institute of Technology
Scope: **DSC programme** (Year 1–3 modules), Dover (DV) campus.

## What's in here

| File / dir | Segment | Purpose |
|---|---|---|
| `src/models.py` | 1 | Pure-Python dataclasses (Course, Activity, Room, Tutor, Group, Timetable, ...) |
| `src/data_loader.py` | 1 | Reads the two SIT Excel workbooks → `Universe` |
| `src/constraint_engine.py` | 2 | Audits any timetable; returns a structured `ViolationReport` |
| `src/solver_cpsat.py` | 3 + 4 | Google OR-Tools CP-SAT model (production solver) |
| `src/solver_heuristic.py` | 3 + 4 | Greedy + local-search fallback (no OR-Tools needed) |
| `src/exporter.py` | 5 | Writes `timetable.json`, `violations.json`, `dashboard.html` |
| `run.py` | — | CLI entry point |
| `tests/test_constraint_engine.py` | — | 10 unit tests for the constraint rules |

## Quick start

```bash
pip install -r requirements.txt
python run.py                              # auto: CP-SAT if available, heuristic otherwise
python run.py --solver cp-sat              # force CP-SAT
python run.py --solver heuristic           # force heuristic
python tests/test_constraint_engine.py     # run the unit tests
```

Outputs land in `output/`:

* `timetable.json` — canonical schedule (tooling-friendly)
* `violations.json` — constraint engine report
* `dashboard.html` — open in any browser; filter by programme, tutor, room, group, or week

## Constraints implemented

**Hard (must never be violated)**
* H1 No two classes share the same room at the same time
* H2 A tutor cannot teach two classes simultaneously
* H3 A student group cannot attend two classes simultaneously
* H4 Room capacity ≥ class enrolment
* H5 Online classes only in the virtual room; face-to-face only in physical rooms
* H6 Two classes only conflict if they share a teaching week (odd/even-week aware)
* H7 Only weeks listed in the academic calendar may be used

**Soft (penalised in the objective)**
* S1 Avoid online↔face-to-face switches in adjacent slots (same tutor or group)
* S2 Avoid tutor idle gaps > 2 hours on the same day
* S3 Avoid student group having > 4 consecutive teaching hours
* S4 Avoid group days with only 1–2 hours on campus
* S5 Schedule online classes for each programme on Mon or Tue
