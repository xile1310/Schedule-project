# DSC2204 Timetabling Prototype

A Python tool that reads a planner-friendly Excel workbook and automatically
generates a feasible class timetable — every class placed into a day, time,
and room without breaking any hard scheduling rules.

---

## What it does

You fill in **`Timetable.xlsx`** with your modules, weeks, staff, subgroups
and rooms. You run **one command**. The solver figures out where every
class goes, then writes the schedule back into **`Output.xlsx`** (the SIT
planning template) and produces an interactive HTML dashboard.

The solver respects:

- Tutor double-booking (a prof can't be in two rooms at once)
- Room capacity (no 100 students in a 30-seat seminar)
- Room type (lectures → lecture theatres, labs → laboratories)
- Online vs face-to-face (online classes get a synthetic VIRTUAL room)
- Subgroups (110 students with `Subgroups=2` → two parallel sessions of 55)
- Teaching weeks (week 7 is reading week — schedule respects it)

…and optimises soft preferences like minimising tutor idle gaps and
avoiding online/f2f mode switching during the same day.

---

## How to run

### 1. Install dependencies

You need Python 3.10+. From the `prototype/` folder:

```
pip install -r requirements.txt
```

(Optional: `pip install ortools` enables the faster CP-SAT solver. Without
it, the pure-Python heuristic solver runs — still produces valid timetables.)

### 2. Run it

```
cd prototype
python run.py
```

That's it — no flags needed. The script auto-detects `Timetable.xlsx`
and `Output.xlsx` sitting in the parent folder.

### 3. Look at the results

After the run finishes, you'll find:

| File                                  | What it is                                          |
|---------------------------------------|-----------------------------------------------------|
| `prototype/output/dashboard.html`     | Interactive calendar view — open in any browser     |
| `prototype/output/results.xlsx`       | Simple one-sheet timetable (Course / Tutor / Room / Day / Time) |
| `prototype/output/timetable.xlsx`     | Detailed multi-sheet output                         |
| `prototype/output/timetable.json`     | Machine-readable schedule                           |
| `prototype/output/violations.json`    | Any constraint issues found                         |
| `Output.xlsx` (project root)          | The SIT template, populated with the schedule       |

The HTML dashboard is the best way to eyeball results. Each module gets its
own row, with classes laid out across Monday–Friday.

---

## Tweaking the inputs

Everything the planner controls lives inside `Timetable.xlsx`:

| Tab           | What you change                                                    |
|---------------|--------------------------------------------------------------------|
| `Module`      | The classes to schedule (module, activity, weeks, staff, subgroups)|
| `Eligibility` | Which staff are allowed to teach each module/activity              |
| `Tutors`      | Staff roster (ID → Name)                                           |
| `Rooms`       | Available rooms with capacity and type                             |
| `Calendar`    | Teaching weeks and semester start date                             |
| `Settings`    | Day start/end times, slot length, subgroup defaults, soft weights |
| `ActivityModes`| Which delivery modes each activity type allows                    |

Re-run `python run.py` after any change. Excel must be closed during the
run (it locks open files on Windows) — if you see *"skipped (open in
Excel?)"* in the log, close `Output.xlsx` / `results.xlsx` and re-run.

---

## Folder layout

```
DSC2204/
├── Timetable.xlsx                            ← your input
├── Output.xlsx                               ← solver writes back into the Timetable tab
├── README.md                                 ← this file
├── CLAUDE.md                                 ← briefing for the next Claude AI agent
├── ITP Project Proposal.pdf                  ← project context
├── ITP Project Requirements.pdf
├── Worksheet in ITP Project Requirements.xlsx
├── TTConstraints_timetline(Constraints).xlsx
├── Venue Information(Campus Court).xlsx
└── prototype/
    ├── run.py                                ← entry point
    ├── requirements.txt
    └── src/                                  ← Python source
```

---