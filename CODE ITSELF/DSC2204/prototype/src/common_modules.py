"""
Common Module Registry
======================
Parses `Common Modules(Sheet1).csv` and provides lookups so the scheduler
can identify activities that are shared across multiple programmes.

A common module means:
  - All listed cohorts attend the SAME lecture session simultaneously.
  - The venue must fit the combined enrolment.
  - No other activity for ANY participating cohort may clash with it.

Alias groups handle the case where each programme uses a different course
code for the same content (e.g. ESE1101 / SBE1101 / ASE1011 are one group).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CmGroup:
    """One row (or alias row) from the CSV — a set of codes that share a session."""
    codes: FrozenSet[str]   # upper-cased, e.g. frozenset({"INF1003"})
    year: int
    programmes: List[str]   # e.g. ["DSC", "ICT"]; ["_ALL_"] means every programme


class CommonModuleRegistry:
    def __init__(self, groups: List[CmGroup]):
        self._groups = groups
        # (code_upper, year) -> group
        self._idx: Dict[tuple, CmGroup] = {}
        for g in groups:
            for c in g.codes:
                self._idx[(c, g.year)] = g

    # ------------------------------------------------------------------
    def group_for(self, code: str, year: int) -> Optional[CmGroup]:
        return self._idx.get((code.upper(), year))

    def alias_codes(self, code: str, year: int) -> FrozenSet[str]:
        g = self.group_for(code, year)
        return g.codes if g else frozenset({code.upper()})

    def is_common(self, code: str, year: int) -> bool:
        return (code.upper(), year) in self._idx

    @property
    def groups(self) -> List[CmGroup]:
        return list(self._groups)


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------

_SEP = re.compile(r'\s*[&,+]\s*|\s+and\s+', re.IGNORECASE)


def _parse_programmes(raw: str) -> List[str]:
    if re.search(r'all\s+programme', raw, re.IGNORECASE):
        return ["_ALL_"]
    parts = _SEP.split(raw)
    result = []
    for p in parts:
        p = re.sub(r'\([^)]*\)', '', p).strip().rstrip('.')
        if p:
            result.append(p.upper())
    return result


def load(csv_path: str) -> CommonModuleRegistry:
    """Load common modules from the CSV file and return a registry."""
    groups: List[CmGroup] = []
    path = Path(csv_path)
    if not path.exists():
        return CommonModuleRegistry([])

    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            module_raw = (row.get('Module') or '').strip()
            year_raw   = (row.get('Year') or '').strip()
            progs_raw  = (row.get('Programmes') or '').strip()
            if not module_raw or not year_raw:
                continue
            codes = frozenset(c.strip().upper() for c in module_raw.split('/') if c.strip())
            try:
                year = int(float(year_raw))
            except ValueError:
                continue
            progs = _parse_programmes(progs_raw)
            groups.append(CmGroup(codes=codes, year=year, programmes=progs))

    return CommonModuleRegistry(groups)
