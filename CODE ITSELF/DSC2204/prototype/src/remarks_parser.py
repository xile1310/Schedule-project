"""
src/remarks_parser.py

LLM-assisted remarks parser — interprets Column K free-text remarks and
mutates Activity / Tutor objects so the solver can enforce them as
hard constraints.

Requires: anthropic>=0.40, ANTHROPIC_API_KEY in env or .env file.
Silently skips on import error, missing key, or API failure.
"""
from __future__ import annotations
import json
import os
import re
from typing import List

from .models import Activity, Tutor, Universe

# ---------------------------------------------------------------------------
# Claude system prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """Build the LLM system prompt, injecting the live semester start date
    read from data_loader (which was loaded from the Settings sheet)."""
    from datetime import timedelta
    try:
        from . import data_loader as _dl
        sem = _dl.SEMESTER_START_DATE
    except Exception:
        from datetime import date
        sem = date(2025, 9, 8)

    sem_str = sem.isoformat()                          # e.g. "2025-09-08"
    # Two concrete examples shown to the LLM (week 6 and week 7 Mondays)
    ex1 = sem + timedelta(weeks=5)                     # Monday of week 6
    ex2 = sem + timedelta(weeks=6)                     # Monday of week 7
    ex1_str  = f"{ex1.day} {ex1.strftime('%b')} {ex1.year}"
    ex1_day  = _WEEKDAY_NAMES[ex1.weekday()]
    ex1_days = (ex1 - sem).days
    ex2_str  = f"{ex2.day} {ex2.strftime('%b')} {ex2.year}"
    ex2_day  = _WEEKDAY_NAMES[ex2.weekday()]
    ex2_days = (ex2 - sem).days

    return f"""\
You are a university timetabling assistant.
Read the remark and understand the INTENT — remarks are written by humans
and can be messy, casual, or vague.

Return ONLY a valid JSON ARRAY (no markdown, no explanation).
Each element must match ONE of the constraint types below.
A single remark may contain MULTIPLE constraints — include ALL that apply.
Return [] if no usable constraints can be extracted.

━━ KEY RULE — Pin vs Availability ━━
Times mentioned in remarks almost always describe WHEN THE CLASS TAKES PLACE → use "pin".
Use "availability" ONLY when the remark uses hard language about the TUTOR's personal
schedule such as: "can only teach", "only available", "not available on", "never on".
"prefer", "prefer to", "would like" and similar soft language describe the CLASS TIME
preference — use "pin" for these (they set the class's preferred start, not a hard
tutor block). When in doubt, default to "pin".

━━ Group-specific times ━━
If a group label is provided at the start of the message (e.g. [Group: T1]), and the remark
contains group-specific times (e.g. "2-4pm (T1) and 4-6pm (T2)"), return only the pin that
matches the given group label.

Pinned day + time:
  {{ "type": "pin", "day": "Mon", "start_hour": 10, "start_min": 0 }}
  day must be one of: Mon Tue Wed Thu Fri, or null if no day is mentioned.

Multiple rooms required:
  {{ "type": "multi_room", "count": 2 }}
  Use when the remark says "2 rooms", "needs 2 theatres", etc.
  count = total number of rooms needed (integer >= 2).

Staff availability window:
  {{ "type": "availability", "day": "Fri",
    "windows": [{{"start_hour": 14, "start_min": 0,
                 "end_hour": 18, "end_min": 0}}] }}
  day may be null if not tied to a specific weekday.

Skip specific teaching weeks:
  {{ "type": "skip_week", "weeks": [7] }}

Block a specific day entirely (tutor refuses that day):
  {{ "type": "block_day", "day": "Tue" }}
  Use only when the remark says "do not teach on X", "never on X", with no time given.

Named venue / room (use ONLY when a SPECIFIC room name or identifier is given):
  {{ "type": "room", "name": "<venue name as written>" }}
  Examples: "at W1, Level 3 Connexion" → name = "W1, Level 3 Connexion"
            "in Room SR-220"           → name = "SR-220"
  Do NOT use this for descriptive room requirements — use "room_requirement" instead.

Room type + capacity requirement (use when the remark describes room characteristics, not a specific named room):
  {{ "type": "room_requirement", "room_type": "<type>", "min_capacity": <integer or null> }}
  room_type must be exactly one of: seminar_room, computer_lab, laboratory, lecture_theatre, other
  min_capacity: minimum seat count required (integer), or null if not stated.
  Examples: "50-seat seminar room" → room_type=seminar_room, min_capacity=50
            "computer lab for 30"  → room_type=computer_lab, min_capacity=30
            "needs a seminar room" → room_type=seminar_room, min_capacity=null

━━ DATE NORMALISATION ━━
Always write dates in your output as "DD Mon YYYY" (e.g. "13 Oct 2025").
Convert non-standard formats before using them:
  "Oct 13"          -> "13 Oct {sem_str[:4]}"
  "October 13th"    -> "13 Oct {sem_str[:4]}"
  "13/10/2025"      -> "13 Oct 2025"
  "13 Oct" (no yr)  -> "13 Oct {sem_str[:4]}"

━━ SEMESTER CALENDAR ━━
Semester start: {sem_str} (Monday of Week 1).
Use this to convert any calendar date in the remark to the correct teaching week and weekday:
  {ex1_str} -> ({ex1_str} - {sem_str}) = {ex1_days} days -> week {ex1_days // 7 + 1}, {ex1_day}
  {ex2_str} -> ({ex2_str} - {sem_str}) = {ex2_days} days -> week {ex2_days // 7 + 1}, {ex2_day}

Set teaching weeks from a calendar date or explicit week numbers.
For EACH date mentioned, emit ONE set_weeks object:
  {{ "type": "set_weeks", "weeks": [7] }}                   <- explicit week numbers
  {{ "type": "set_weeks", "date": "13 Oct {sem_str[:4]}", "day": "{ex1_day}" }}  <- calendar date

  If the remark mentions MULTIPLE dates, emit a SEPARATE set_weeks for EACH date:
    "13 Oct and 27 Oct" -> two set_weeks objects, one per date
    "Mon 13 Oct 2pm and Tue 27 Oct 4pm" -> two set_weeks, each with day + start_hour

  Use set_weeks whenever a specific date or week is mentioned, even if the remark
  says "whenever it is available" — the date is still the scheduling target.

Conversion rules:
- "9am" -> start_hour=9, start_min=0
- "2:30pm" -> start_hour=14, start_min=30
- "2pm" -> start_hour=14, start_min=0
- "Monday, 10am-12pm" -> pin, day=Mon, start_hour=10
- "7 Nov (Friday) 2-4pm" -> pin, day=Fri, start_hour=14
- "9AM-6PM" -> pin, day=null, start_hour=9  (time-only, no day)
- "2 rooms" -> multi_room, count=2
- "AF can only teach on Fridays, 2-4pm" -> availability (explicit "can only teach"), day=Fri, window 14:00-16:00
- "not available week 7" -> skip_week, weeks=[7]
- "prefer afternoons" -> pin, day=null, start_hour=14  (preference, NOT availability)
- "prefer Monday or Tuesday mornings" -> pin, day=Mon, start_hour=9  (first preferred day only)
- "prefer late-morning slots" -> pin, day=null, start_hour=10
- "not available before 10am" -> availability, day=null, window 10:00-18:00
- "not available on Thursdays" -> block_day, day=Thu
- Use 18:00 as end time when none specified for availability windows.
- "Two subgroups; prefer 50-seat seminar rooms" -> room_requirement, room_type=seminar_room, min_capacity=50
- "need a computer lab for 30 students" -> room_requirement, room_type=computer_lab, min_capacity=30
- "in Room SR-220" -> room, name=SR-220
- "Week 7 only" -> set_weeks, weeks=[7]
- "{ex1_str}" -> [set_weeks date="{ex1_str}" day="{ex1_day}"] ({ex1_str} is week {ex1_days // 7 + 1}, {ex1_day})
- "print 'My birthday' on {ex1_str}, whenever it is available" -> [set_weeks date="{ex1_str}" day="{ex1_day}"]
- "Oct 13th" -> [set_weeks date="13 Oct {sem_str[:4]}" day="{ex1_day}"]  (normalise to DD Mon YYYY first)
- "13/{(ex1.month):02d}/{ex1.year}" -> [set_weeks date="{ex1_str}" day="{ex1_day}"]  (numeric date)
- "13 Oct and 27 Oct" -> TWO set_weeks objects, one per date (each with its own day)
- "Mon 13 Oct 2pm and Tue {ex2.day} {ex2.strftime('%b')} 4pm" ->
    [{{"type":"set_weeks","date":"{ex1_str}","day":"Mon","start_hour":14,"start_min":0}},
     {{"type":"set_weeks","date":"{ex2_str}","day":"Tue","start_hour":16,"start_min":0}}]
- Multi-constraint: "Must be in Room SR-220, skip week 7" ->
  [{{"type": "room", "name": "SR-220"}}, {{"type": "skip_week", "weeks": [7]}}]
"""

# ---------------------------------------------------------------------------
# SDK client (lazy singleton)
# ---------------------------------------------------------------------------

_client = None
_HAS_ANTHROPIC: bool | None = None


def _load_dotenv() -> None:
    try:
        from pathlib import Path
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            with open(env_path) as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


def _get_client():
    global _client, _HAS_ANTHROPIC
    if _HAS_ANTHROPIC is None:
        try:
            import anthropic  # noqa: F401
            _HAS_ANTHROPIC = True
        except ImportError:
            _HAS_ANTHROPIC = False
    if not _HAS_ANTHROPIC:
        return None
    if _client is None:
        _load_dotenv()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        import anthropic
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def is_llm_available() -> bool:
    """Return True if the Anthropic client is configured and reachable."""
    return _get_client() is not None


def _parse_json_response(raw: str) -> object:
    """Extract and parse JSON from an LLM response, tolerating trailing text."""
    raw = raw.strip()
    # Strip markdown code fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # If there's trailing text after a JSON array/object, extract just the JSON part
        if raw.startswith("["):
            end = raw.rfind("]")
            if end != -1:
                return json.loads(raw[: end + 1])
        elif raw.startswith("{"):
            end = raw.rfind("}")
            if end != -1:
                return json.loads(raw[: end + 1])
        raise


# ---------------------------------------------------------------------------
# Simple pin extractor + weeks-cell extractor — used by data_loader during
# loading, before the full constraint pass runs.
# ---------------------------------------------------------------------------

_SYSTEM_PIN = """\
You extract a scheduled day and start time from free-text remarks for a university timetable.
Return ONLY a JSON object with exactly two keys:
  "day"   — one of: "Mon", "Tue", "Wed", "Thu", "Fri", or null
  "start" — 24-hour start time as "HH:MM" (e.g. "09:00", "14:30"), or null

Rules:
- If no day is mentioned, set day to null.
- If no start time is mentioned, set start to null.
- Ignore duration, end time, room, or any other info — only extract an EXPLICIT pin.
- A remark like "do not schedule on Tuesdays" does NOT contain a pin — return null for day.
- Only extract a day when the remark explicitly schedules the class on that day with a time.
- "9am" → "09:00", "2:30pm" → "14:30", "2pm" → "14:00"
"""

_SYSTEM_WEEKS = """\
You extract scheduling information from a university timetable "Teaching Weeks" cell.
The cell may be plain week numbers ("1-13") or a specific date and time ("22 Oct (Wed), 1.30pm-5pm").

Return ONLY a JSON object (no markdown, no explanation):
{
  "pin_day":    "Mon"|"Tue"|"Wed"|"Thu"|"Fri"|null,
  "start_hour": <24h integer>|null,
  "start_min":  <integer 0-59>|null,
  "end_hour":   <24h integer>|null,
  "end_min":    <integer 0-59>|null
}

Rules:
- pin_day: extract from an explicit annotation such as "(Wed)" or "Wednesday". Set null if absent.
- Convert all times to 24-hour format:
    "1.30pm" → 13, 30   |  "5pm" → 17, 0   |  "9am" → 9, 0   |  "12pm" → 12, 0
- If the cell contains no recognisable time, set all time fields to null.
- If the cell is only week numbers ("1-13", "1,3,5"), return all null.

Examples:
"22 Oct (Wed), 1.30pm-5pm" → {"pin_day":"Wed","start_hour":13,"start_min":30,"end_hour":17,"end_min":0}
"10 Nov (Mon), 9am-1pm"    → {"pin_day":"Mon","start_hour":9,"start_min":0,"end_hour":13,"end_min":0}
"7 Nov (Fri) 2pm-4pm"      → {"pin_day":"Fri","start_hour":14,"start_min":0,"end_hour":16,"end_min":0}
"1-13"                     → {"pin_day":null,"start_hour":null,"start_min":null,"end_hour":null,"end_min":null}
"""


def parse_remarks_llm(remarks: str) -> tuple[str | None, int | None]:
    """Extract (fixed_day, fixed_start_index) from arbitrary remarks text.

    Used by data_loader during loading to set initial pin values on activities.
    Returns (None, None) on any error, missing API key, or non-pin remarks.
    """
    if not remarks:
        return None, None
    client = _get_client()
    if client is None:
        return None, None
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            temperature=0,
            system=[{"type": "text", "text": _SYSTEM_PIN, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": str(remarks)}],
        )
        data = _parse_json_response(response.content[0].text)
        day = data.get("day")
        start_str = data.get("start")
        if day not in ("Mon", "Tue", "Wed", "Thu", "Fri"):
            day = None
        slot_idx = None
        if start_str:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", start_str.strip())
            if m:
                hour, minute = int(m.group(1)), int(m.group(2))
                dsh = _day_start_hour()
                if hour >= dsh:
                    slot_idx = (hour - dsh) * 2 + (1 if minute >= 30 else 0)
        return day, slot_idx
    except Exception:
        return None, None


def parse_weeks_cell_llm(cell_text: str) -> dict | None:
    """Extract pin_day and time range from a Teaching Weeks cell.

    Used by data_loader._extract_pin_from_weeks_cell() for calendar-date entries
    like "22 Oct (Wed), 1.30pm-5pm".
    Returns dict with keys: pin_day, start_hour, start_min, end_hour, end_min,
    or None on failure.
    """
    if not cell_text:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            temperature=0,
            system=[{"type": "text", "text": _SYSTEM_WEEKS, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": str(cell_text)}],
        )
        return _parse_json_response(response.content[0].text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Primary teaching-weeks parser — LLM-first, raises on failure
# ---------------------------------------------------------------------------

_SYSTEM_TEACHING_WEEKS = """\
You are a university timetable assistant. Parse a "Teaching Weeks" cell from a module planning spreadsheet.

Your job is ONLY to extract dates and times — do NOT calculate week numbers yourself.

Return ONLY a JSON object — no markdown, no explanation:

{
  "date_pins": [
    {
      "iso_date": "YYYY-MM-DD",
      "start_time": "HH:MM" or null,
      "end_time": "HH:MM" or null
    }
  ],
  "explicit_weeks": []
}

Rules:
1. If the cell contains specific calendar dates (e.g. "15 September 2025", "2025-10-22", "22 Oct"):
   - Extract each date as "YYYY-MM-DD". If no year given, use the semester year from context.
   - Extract any time range present (e.g. "9am-11am" → start "09:00", end "11:00")
   - Convert times to 24-hour "HH:MM": "1.30pm" → "13:30", "5pm" → "17:00", "9am" → "09:00"
   - Put results in "date_pins", leave "explicit_weeks" empty
2. If the cell contains only plain week expressions (e.g. "weeks 1 to 6", "odd weeks", "every other week from week 2"):
   - Put the week numbers in "explicit_weeks", leave "date_pins" empty
   - "odd weeks" = [1,3,5,7,9,11,13], "even weeks" = [2,4,6,8,10,12]
   - "weeks 1 to 6" = [1,2,3,4,5,6]
3. For multiple dates, include all of them in "date_pins".
4. Never mix date_pins and explicit_weeks — use whichever applies.

Examples:
"2025-10-22" → {"date_pins": [{"iso_date": "2025-10-22", "start_time": null, "end_time": null}], "explicit_weeks": []}
"15 September 2025 and 19 November 2025" → {"date_pins": [{"iso_date": "2025-09-15", "start_time": null, "end_time": null}, {"iso_date": "2025-11-19", "start_time": null, "end_time": null}], "explicit_weeks": []}
"22 Oct (Wed), 1.30pm-5pm" → {"date_pins": [{"iso_date": "2025-10-22", "start_time": "13:30", "end_time": "17:00"}], "explicit_weeks": []}
"odd weeks" → {"date_pins": [], "explicit_weeks": [1,3,5,7,9,11,13]}
"""


def parse_teaching_weeks_llm(cell_text: str, semester_start_iso: str) -> dict:
    """Parse a Teaching Weeks cell with Claude as the primary parser.

    The LLM extracts ISO dates and times; Python converts dates to week numbers
    and day-of-week (avoids LLM arithmetic errors).

    Args:
        cell_text: raw cell value (e.g. "15 September 2025 and 19 November 2025")
        semester_start_iso: semester start in ISO format (e.g. "2025-09-08")

    Returns:
        {"weeks": [int, ...], "pins": [{"week", "day", "start_time", "end_time"}, ...]}

    Raises:
        RuntimeError if the API key is missing or the LLM call fails.
    """
    from datetime import date as _date

    client = _get_client()
    if client is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set or the 'anthropic' package is not installed. "
            "It is required to parse free-text Teaching Weeks cells. "
            "Set ANTHROPIC_API_KEY in your environment and restart the dashboard."
        )

    try:
        sem_year = int(semester_start_iso[:4])
    except (ValueError, TypeError):
        sem_year = 2025

    user_msg = f"Semester year: {sem_year}\nTeaching Weeks cell: {cell_text}"
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            temperature=0,
            system=[{"type": "text", "text": _SYSTEM_TEACHING_WEEKS,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = _parse_json_response(response.content[0].text)
        if not isinstance(raw, dict):
            raise ValueError(f"unexpected response shape: {response.content[0].text!r}")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"LLM failed to parse Teaching Weeks cell {cell_text!r}: {exc}"
        ) from exc

    # Convert date_pins → week numbers + day pins using Python arithmetic.
    _DOW = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    try:
        sem_start = _date.fromisoformat(semester_start_iso)
    except ValueError:
        sem_start = None

    weeks: list[int] = list(raw.get("explicit_weeks") or [])
    pins: list[dict] = []

    for dp in raw.get("date_pins") or []:
        iso = dp.get("iso_date", "")
        try:
            d = _date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if sem_start:
            wk = (d - sem_start).days // 7 + 1
        else:
            wk = None
        dow = _DOW[d.weekday()] if d.weekday() < 5 else None
        if wk and wk >= 1:
            weeks.append(wk)
            pins.append({
                "week": wk,
                "day": dow,
                "start_time": dp.get("start_time"),
                "end_time": dp.get("end_time"),
            })

    return {"weeks": sorted(set(weeks)), "pins": pins}


# ---------------------------------------------------------------------------
# Slot helpers  (mirror data_loader constants; import live value to stay in sync)
# ---------------------------------------------------------------------------

def _day_start_hour() -> int:
    try:
        from . import data_loader
        return data_loader.DAY_START_HOUR
    except Exception:
        return 8


def _to_slot(hour: int, minute: int) -> int:
    return (hour - _day_start_hour()) * 2 + (1 if minute >= 30 else 0)


def _slots_in_window(
    start_hour: int, start_min: int,
    end_hour: int,   end_min: int,
) -> List[int]:
    """All 30-min slot indices fully contained in [start, end)."""
    dsh = _day_start_hour()
    start_t = start_hour * 60 + start_min
    end_t   = end_hour   * 60 + end_min
    slots: List[int] = []
    t = start_t
    while t + 30 <= end_t:
        h, m = divmod(t, 60)
        if h >= dsh:
            slots.append(_to_slot(h, m))
        t += 30
    return slots


_VALID_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri"}
_ALL_DAYS   = list(_VALID_DAYS)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _remarks_date_to_week(date_str: str) -> int | None:
    """Convert a calendar date string like '13 Oct 2025' or '13 Oct' to a
    teaching week number, counting from SEMESTER_START_DATE (week 1 Monday).
    Returns None if conversion fails or result is out of range 1-13."""
    result = _remarks_date_to_week_and_day(date_str)
    return result[0] if result else None


def _remarks_date_to_week_and_day(date_str: str) -> tuple[int, str] | None:
    """Convert a calendar date string to (teaching_week, day_name).

    Uses SEMESTER_START_DATE from data_loader to calculate the week number.
    day_name is one of 'Mon' 'Tue' 'Wed' 'Thu' 'Fri'; returns None for weekends
    or if conversion fails.

    Example: '13 Oct 2025', semester starts 8 Sep 2025
      → week 6 (35 days // 7 + 1), Monday
    """
    if not date_str:
        return None
    try:
        from datetime import date as _date
        from . import data_loader as _dl
        sem = _dl.SEMESTER_START_DATE
        text = date_str.strip()
        m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?(?:\s+(\d{4}))?", text)
        if not m:
            return None
        day = int(m.group(1))
        mon = m.group(2)[:3].lower()
        if mon not in _MONTHS:
            return None
        year = int(m.group(3)) if m.group(3) else sem.year
        d = _date(year, _MONTHS[mon], day)
        delta = (d - sem).days
        if delta < 0:
            return None
        week = delta // 7 + 1
        if not (1 <= week <= 13):
            return None
        wd = d.weekday()          # 0=Mon … 4=Fri, 5=Sat, 6=Sun
        if wd >= 5:
            return None           # weekend — no teaching day
        return week, _WEEKDAY_NAMES[wd]
    except Exception:
        return None


def _extract_all_dates(text: str) -> list[str]:
    """Extract every calendar date mentioned in free text.

    Handles all common formats:
      '13 Oct'          '13 October'        '13 Oct 2025'
      'Oct 13'          'October 13th'      'Oct 13th 2025'
      '13/10/2025'      '13-10-2025'

    Returns a deduplicated list of normalised 'DD Mon YYYY' strings.
    """
    from . import data_loader as _dl
    sem_year = str(_dl.SEMESTER_START_DATE.year)
    _mon_re = (
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    )
    found: list[str] = []

    # Pattern 1: day-first  "13 Oct [2025]"
    for m in re.finditer(
        rf"\b(\d{{1,2}})\s+{_mon_re}\.?(?:st|nd|rd|th)?(?:\s+(\d{{4}}))?\b",
        text, re.IGNORECASE,
    ):
        day, mon, yr = m.group(1), m.group(2)[:3].lower(), m.group(3) or sem_year
        candidate = f"{day} {mon} {yr}"
        if candidate not in found:
            found.append(candidate)

    # Pattern 2: month-first  "Oct 13th [2025]"
    for m in re.finditer(
        rf"\b{_mon_re}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(\d{{4}}))?\b",
        text, re.IGNORECASE,
    ):
        mon, day, yr = m.group(1)[:3].lower(), m.group(2), m.group(3) or sem_year
        candidate = f"{day} {mon} {yr}"
        if candidate not in found:
            found.append(candidate)

    # Pattern 3: numeric  "13/10/2025"  or  "13-10-2025"  (day/month, European)
    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text):
        try:
            d, mo, yr_raw = int(m.group(1)), int(m.group(2)), m.group(3)
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                continue
            if len(yr_raw) == 2:
                yr_raw = "20" + yr_raw
            mon_abbr = list(_MONTHS.keys())[mo - 1]
            candidate = f"{d} {mon_abbr} {yr_raw}"
            if candidate not in found:
                found.append(candidate)
        except (ValueError, IndexError):
            pass

    return found


_ROOM_PATTERNS = [
    re.compile(r"\b(?:at|in|room|venue|held at)\s+([A-Za-z0-9][A-Za-z0-9,./()&'\- ]{1,80})", re.IGNORECASE),
]

# Words that, if they start the extracted text, indicate a false-positive match
# (e.g. "in weeks 7 and 13" → "weeks..." is NOT a room name).
_ROOM_FALSE_STARTS = re.compile(
    r"^(week|day|class|student|group|subgroup|slot|period|time|the|a|an|"
    r"zoom|teams|online|any|all|both|each|no|not|this)\b",
    re.IGNORECASE,
)


def _extract_room_name(remark: str) -> str | None:
    """Best-effort room/venue extractor for free-text remarks.

    This catches common patterns such as:
      - "Engagement event with Industry at W1, Level 3 Connexion"
      - "Must be in Room SR-220"

    Rejects false positives (e.g. "in weeks 7 and 13") by requiring the
    extracted text to start with an uppercase letter or digit and not begin
    with a common non-venue word.
    """
    text = (remark or "").strip()
    if not text:
        return None

    for pattern in _ROOM_PATTERNS:
        match = pattern.search(text)
        if match:
            room = match.group(1).strip()
            room = re.sub(r"\s+", " ", room)
            room = room.rstrip(".,;:")
            room = re.sub(r"^(?:room|venue|hall|space)\s+", "", room, flags=re.IGNORECASE)
            if not room:
                continue
            # Reject if it starts with a common non-venue word.
            if _ROOM_FALSE_STARTS.match(room):
                continue
            # Require the first character to be uppercase or a digit — real
            # venue codes and names always start this way (SR-220, W1, LT-1,
            # "Connexion", etc.).  Lowercase starts ("weeks", "class") are
            # not real room identifiers.
            if room[0].islower():
                continue
            return room
    return None


# ---------------------------------------------------------------------------
# Core parse function
# ---------------------------------------------------------------------------

def parse_remarks(
    remark: str,
    activity: Activity,
    tutor: Tutor,
    all_tutors: List[Tutor],   # kept for future cross-tutor logic
) -> None:
    """
    Call Claude Sonnet to interpret *remark* and directly mutate
    *activity* and/or *tutor* in place.

    Always writes activity.notes = remark.
    Leaves scheduling fields unchanged on API error or unresolvable remark.
    """
    activity.notes = remark
    room_name = _extract_room_name(remark)

    # Always run the Python date-pin fallback first, regardless of LLM outcome.
    # Handles ALL dates found in the remark (not just the first one), and supports
    # multiple formats: "13 Oct", "Oct 13", "October 13th", "13/10/2025".
    # The LLM path below can refine or override these if it returns set_weeks items.
    if getattr(activity, "weeks_from_default", False):
        _all_dates = _extract_all_dates(remark)
        _pinned: dict[int, tuple] = {}
        for ds in _all_dates:
            res = _remarks_date_to_week_and_day(ds)
            if res:
                wk, day = res
                _pinned[wk] = (day, None)
        if _pinned:
            activity.week_pins = _pinned
            activity.weeks = sorted(_pinned)
            activity.weeks_from_default = False
            # If all pinned weeks share the same day, set fixed_day once
            unique_days = {d for (d, _) in _pinned.values() if d}
            if len(unique_days) == 1 and not activity.fixed_day:
                activity.fixed_day = unique_days.pop()
            print(f"[remarks_parser] date pin (pre-LLM): found {list(_pinned.items())} for {activity.id}")

    # Extract the group label (e.g. "T1", "T2", "All") so the LLM can resolve
    # group-specific time variants like "2-4pm (T1) and 4-6pm (T2)".
    group_label = activity.group_id.split("/")[-1] if "/" in activity.group_id else activity.group_id
    llm_content = f"[Group: {group_label}]\n{remark}" if group_label and group_label.lower() != "all" else remark

    _weeks_replaced = False  # True once we start accumulating LLM-sourced week pins

    client = _get_client()
    if client is None:
        if room_name:
            activity.fixed_room_id = room_name
        return

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            temperature=0,
            system=[
                {
                    "type": "text",
                    "text": _build_system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": llm_content}],
        )
        parsed = _parse_json_response(response.content[0].text)
        # Accept both array (current) and bare dict (legacy fallback)
        items: list = parsed if isinstance(parsed, list) else [parsed]

    except Exception as exc:
        print(f"[remarks_parser] WARNING — API error for remark {remark!r}: {exc}")
        items = []   # treat as empty; the pre-LLM date pin already ran above

    for data in items:
        rtype = data.get("type")

        # ── pin ──────────────────────────────────────────────────────────────
        if rtype == "pin":
            day = data.get("day")
            sh  = data.get("start_hour")
            sm  = int(data.get("start_min") or 0)
            if isinstance(sh, int) and sh >= _day_start_hour():
                if day in _VALID_DAYS:
                    activity.fixed_day = day
                activity.fixed_start_index = _to_slot(sh, sm)

        # ── multi_room ───────────────────────────────────────────────────────
        elif rtype == "multi_room":
            count = data.get("count")
            if isinstance(count, int) and count >= 2:
                activity.room_count = max(getattr(activity, "room_count", 1), count)

        # ── availability ─────────────────────────────────────────────────────
        elif rtype == "availability":
            day     = data.get("day")
            windows = data.get("windows", [])
            slots: List[int] = []
            for w in windows:
                slots.extend(_slots_in_window(
                    int(w.get("start_hour") or 8),  int(w.get("start_min") or 0),
                    int(w.get("end_hour")   or 18), int(w.get("end_min")   or 0),
                ))
            slots = sorted(set(slots))
            if not slots:
                continue
            targets = [day] if day in _VALID_DAYS else _ALL_DAYS
            for d in targets:
                existing = tutor.availability.get(d, [])
                tutor.availability[d] = sorted(set(existing) | set(slots))

        # ── block_day ────────────────────────────────────────────────────────
        elif rtype == "block_day":
            day = data.get("day")
            if day in _VALID_DAYS:
                tutor.availability[day] = []

        # ── skip_week ────────────────────────────────────────────────────────
        elif rtype == "skip_week":
            skip = set(data.get("weeks", []))
            activity.weeks = [w for w in activity.weeks if w not in skip]

        # ── set_weeks ────────────────────────────────────────────────────────
        elif rtype == "set_weeks":
            # Eligible when weeks were defaulted OR when we've already started
            # accumulating LLM-sourced date pins (multiple set_weeks in one remark).
            eligible = getattr(activity, "weeks_from_default", False) or _weeks_replaced
            if eligible:
                weeks_list = list(data.get("weeks") or [])
                date_str = str(data.get("date") or "").strip()
                day_hint = data.get("day")
                sh = data.get("start_hour")
                sm = data.get("start_min", 0)

                week_day: str | None = None
                if not weeks_list and date_str:
                    res = _remarks_date_to_week_and_day(date_str)
                    if res:
                        wk_d, week_day = res
                        weeks_list = [wk_d]
                elif date_str:
                    res = _remarks_date_to_week_and_day(date_str)
                    if res:
                        _, week_day = res

                if day_hint in _VALID_DAYS and not week_day:
                    week_day = day_hint

                slot_idx: int | None = None
                if isinstance(sh, int) and sh >= _day_start_hour():
                    slot_idx = _to_slot(sh, int(sm or 0))

                valid = sorted(
                    set(int(w) for w in weeks_list
                        if isinstance(w, (int, float)) and 1 <= int(w) <= 13)
                )
                if valid:
                    if not _weeks_replaced:
                        # First LLM pin — discard the fallback week list and start fresh
                        activity.weeks = []
                        activity.week_pins = {}
                        activity.weeks_from_default = False
                        _weeks_replaced = True
                    for wk in valid:
                        if wk not in activity.weeks:
                            activity.weeks.append(wk)
                        if week_day or slot_idx is not None:
                            activity.week_pins[wk] = (week_day, slot_idx)
                    activity.weeks.sort()
                    # Update fixed_day if all pinned weeks share one day
                    pinned_days = {d for (d, _) in activity.week_pins.values() if d}
                    if len(pinned_days) == 1:
                        activity.fixed_day = pinned_days.pop()
                    else:
                        activity.fixed_day = None  # different days per week — handled by split

        # ── room_requirement ─────────────────────────────────────────────────
        elif rtype == "room_requirement":
            rtype_str = str(data.get("room_type", "")).strip().lower()
            rcap = data.get("min_capacity")
            valid_types = {"seminar_room", "computer_lab", "laboratory",
                           "lecture_theatre", "other"}
            if rtype_str in valid_types:
                activity.room_type_req = rtype_str
            if isinstance(rcap, (int, float)) and rcap > 0:
                activity.room_cap_req = int(rcap)

        # ── room ─────────────────────────────────────────────────────────────
        elif rtype == "room":
            room_name = str(data.get("name", "")).strip()
            if room_name:
                activity.fixed_room_id = room_name

        elif rtype not in (None, "unresolved"):
            print(f"[remarks_parser] WARNING — unknown type {rtype!r} for remark {remark!r}")

    # Post-LLM safety net: if weeks_from_default is STILL True (LLM returned nothing
    # useful for set_weeks), run the full multi-date extractor one more time.
    # This is normally already handled pre-LLM, but the flag may have been reset
    # by earlier handling in this call, so this is a no-op in the common path.
    if getattr(activity, "weeks_from_default", False):
        _remaining = _extract_all_dates(remark)
        _pinned2: dict[int, tuple] = {}
        for ds in _remaining:
            res = _remarks_date_to_week_and_day(ds)
            if res:
                wk2, day2 = res
                _pinned2[wk2] = (day2, None)
        if _pinned2:
            activity.week_pins = _pinned2
            activity.weeks = sorted(_pinned2)
            activity.weeks_from_default = False
            _ud2 = {d for (d, _) in _pinned2.values() if d}
            if len(_ud2) == 1 and not activity.fixed_day:
                activity.fixed_day = _ud2.pop()

    # Deterministic fallback: if the LLM did not produce a room pin AND no
    # room_type_req was set, try to extract a named room directly from the text
    # so event-space rooms still work even when the model response is malformed.
    if (not getattr(activity, "fixed_room_id", None)
            and not getattr(activity, "room_type_req", None)
            and room_name):
        activity.fixed_room_id = room_name


# ---------------------------------------------------------------------------
# Bulk runner
# ---------------------------------------------------------------------------

def parse_all_remarks(universe: Universe) -> tuple:
    """
    Walk every Activity in *universe* that has a non-empty notes field
    and call parse_remarks for it.

    Returns (parsed_count, warnings) where:
      parsed_count — number of remarks dispatched
      warnings     — list of human-readable validation warning strings
    """
    tutor_map = {t.id: t for t in universe.tutors}
    parsed = 0
    warnings: list[str] = []

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
            warnings.append(f"{activity.id}: {exc}")
            print(f"[remarks_parser] WARNING — failed to parse remark for {activity.id}: {exc}")

    # Register any venue-pinned rooms that are not in the formal room list.
    from .models import Room, RoomType
    existing_ids = {r.id for r in universe.rooms}
    _event_space_ids: set[str] = set()
    for activity in universe.all_activities():
        rid = getattr(activity, "fixed_room_id", None)
        if rid and rid not in existing_ids:
            universe.rooms.append(Room(
                id=rid, name=rid, capacity=10_000,
                room_type=RoomType.OTHER, zone="Event Space",
            ))
            existing_ids.add(rid)
            _event_space_ids.add(rid)
            print(f"[remarks_parser] added event-space room: {rid!r}")

    # Validate venue pins against room rules (capacity, mode).
    # Event spaces created above are exempt — they're special venues.
    # Formal rooms (from the Rooms tab) are checked and warnings raised.
    room_by_id = {r.id: r for r in universe.rooms}
    for activity in universe.all_activities():
        rid = getattr(activity, "fixed_room_id", None)
        if not rid:
            continue
        if rid in _event_space_ids:
            continue  # auto-created event space — no validation needed
        room = room_by_id.get(rid)
        if room is None:
            msg = (f"Pinned room {rid!r} not found in room list "
                   f"for activity {activity.id} — will cause solver error")
            warnings.append(msg)
            print(f"[remarks_parser] WARNING: {msg}")
            continue
        mode = activity.delivery_mode.value
        if mode in ("online_sync", "online_async") and not room.is_virtual:
            msg = (f"{activity.course_code} {activity.activity_type.value}: "
                   f"online activity pinned to physical room {rid!r} — "
                   f"pin honoured but delivery mode mismatch")
            warnings.append(msg)
            print(f"[remarks_parser] WARNING: {msg}")
        elif mode == "f2f" and room.is_virtual:
            msg = (f"{activity.course_code} {activity.activity_type.value}: "
                   f"f2f activity pinned to virtual room {rid!r} — "
                   f"pin honoured but delivery mode mismatch")
            warnings.append(msg)
            print(f"[remarks_parser] WARNING: {msg}")
        if not room.is_virtual and room.capacity < activity.size:
            msg = (f"{activity.course_code} {activity.activity_type.value}: "
                   f"pinned room {rid!r} capacity {room.capacity} < "
                   f"activity size {activity.size} — "
                   f"room will be overcrowded")
            warnings.append(msg)
            print(f"[remarks_parser] WARNING: {msg}")

    return parsed, warnings
