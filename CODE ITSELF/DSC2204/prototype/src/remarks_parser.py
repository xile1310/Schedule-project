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

_SYSTEM = """\
You are a university timetabling assistant.
Read the remark and understand the INTENT — remarks are written by humans
and can be messy, casual, or vague.

Return ONLY a valid JSON ARRAY (no markdown, no explanation).
Each element must match ONE of the constraint types below.
A single remark may contain MULTIPLE constraints — include ALL that apply.
Return [] if no usable constraints can be extracted.

Pinned day + time:
  { "type": "pin", "day": "Mon", "start_hour": 10, "start_min": 0 }
  day must be one of: Mon Tue Wed Thu Fri

Staff availability window:
  { "type": "availability", "day": "Fri",
    "windows": [{"start_hour": 14, "start_min": 0,
                 "end_hour": 18, "end_min": 0}] }
  day may be null if not tied to a specific weekday.

Skip specific teaching weeks:
  { "type": "skip_week", "weeks": [7] }

Block a specific day entirely (tutor refuses that day):
  { "type": "block_day", "day": "Tue" }
  Use this when the remark says "do not want to teach on X", "not available on X",
  "never on X", etc. for a whole day with no time window given.

Named venue / room (activity must be held here):
  { "type": "room", "name": "<venue name as written>" }
  Use when the remark names a specific room, hall, or event space where the activity
  MUST take place. Extract the venue name verbatim.
  Examples: "at W1, Level 3 Connexion" → name = "W1, Level 3 Connexion"
            "in Room SR-220"           → name = "SR-220"

Conversion rules:
- "9am" → start_hour=9, start_min=0
- "2:30pm" → start_hour=14, start_min=30
- "2pm" → start_hour=14, start_min=0
- "7 Nov (Friday) 2-4pm" → pin to Fri at 14:00
- "not available week 7" → skip_week, weeks=[7]
- "prefer afternoons" → availability, day=null, window 12:00-18:00
- "not available before 10am" → availability, day=null, window 10:00-18:00
- Use 18:00 as end time when none is specified.
- Multi-constraint example: "Must be in Room SR-220, skip week 7" →
  [{"type": "room", "name": "SR-220"}, {"type": "skip_week", "weeks": [7]}]
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
            system=[{"type": "text", "text": _SYSTEM_PIN, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": str(remarks)}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
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
            system=[{"type": "text", "text": _SYSTEM_WEEKS, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": str(cell_text)}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception:
        return None


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


_ROOM_PATTERNS = [
    re.compile(r"\b(?:at|in|room|venue|held at)\s+([A-Za-z0-9][A-Za-z0-9,./()&'\- ]{1,80})", re.IGNORECASE),
]


def _extract_room_name(remark: str) -> str | None:
    """Best-effort room/venue extractor for free-text remarks.

    This catches common patterns such as:
      - "Engagement event with Industry at W1, Level 3 Connexion"
      - "Must be in Room SR-220"
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
            if room:
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

    client = _get_client()
    if client is None:
        if room_name:
            activity.fixed_room_id = room_name
        return

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": remark}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        parsed = json.loads(raw)
        # Accept both array (current) and bare dict (legacy fallback)
        items: list = parsed if isinstance(parsed, list) else [parsed]

    except Exception as exc:
        print(f"[remarks_parser] WARNING — API error for remark {remark!r}: {exc}")
        return

    for data in items:
        rtype = data.get("type")

        # ── pin ──────────────────────────────────────────────────────────────
        if rtype == "pin":
            day = data.get("day")
            sh  = data.get("start_hour")
            sm  = int(data.get("start_min", 0))
            if day in _VALID_DAYS and isinstance(sh, int) and sh >= _day_start_hour():
                activity.fixed_day         = day
                activity.fixed_start_index = _to_slot(sh, sm)

        # ── availability ─────────────────────────────────────────────────────
        elif rtype == "availability":
            day     = data.get("day")
            windows = data.get("windows", [])
            slots: List[int] = []
            for w in windows:
                slots.extend(_slots_in_window(
                    int(w.get("start_hour", 8)),  int(w.get("start_min", 0)),
                    int(w.get("end_hour",   18)), int(w.get("end_min",   0)),
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

        # ── room ─────────────────────────────────────────────────────────────
        elif rtype == "room":
            room_name = str(data.get("name", "")).strip()
            if room_name:
                activity.fixed_room_id = room_name

        elif rtype not in (None, "unresolved"):
            print(f"[remarks_parser] WARNING — unknown type {rtype!r} for remark {remark!r}")

    # Deterministic fallback: if the LLM did not produce a room pin, try to
    # extract one directly from the remark text so event-space rooms still work
    # even when the model response is missing or malformed.
    if not getattr(activity, "fixed_room_id", None) and room_name:
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
