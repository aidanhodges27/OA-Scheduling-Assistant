# oa_app/schedule_query.py
import time
import re
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional, Set, Any
from zoneinfo import ZoneInfo
import streamlit as st
import gspread
import gspread.utils as a1
import pandas as pd
import plotly.express as px

from .. import config
from ..core import summer_schedule

from ..core import week_range as week_range_mod

from ..config import (
    OA_SCHEDULE_SHEETS,   # e.g. ["UNH (OA and GOAs)", "MC (OA and GOAs)"]
    ROSTER_SHEET,
    AUDIT_SHEET,
    LOCKS_SHEET,
    ONCALL_MAX_COLS,
    ONCALL_MAX_ROWS,
)
# Optional override; if missing, treat as None
try:
    from ..config import ONCALL_SHEET_OVERRIDE as _ONCALL_OVERRIDE
except Exception:
    _ONCALL_OVERRIDE = None

from ..core.quotas import _safe_batch_get

# ──────────────────────────────────────────────────────────────────────────────
# 2) Normalize the OA name (case-insensitive substring matching)
# ──────────────────────────────────────────────────────────────────────────────

_PREFIX_RE = re.compile(r"^\s*(?:OA|GOA|On[-\s]*Call)\s*:\s*", re.I)
_ROLE_NAME_RE = re.compile(r"^\s*(OA|GOA|On[-\s]*Call)\s*:\s*(.*?)\s*$", re.I)
_WEEKDAY_SET = {"monday", "tuesday", "wednesday", "thursday", "friday"}
_WEEKEND_SET = {"saturday", "sunday"}
_LA_TZ = ZoneInfo("America/Los_Angeles")


def _norm_name(s: str) -> str:
    s = _PREFIX_RE.sub("", s or "")
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.lower().split())


def _cell_has_name(cell: str, name_norm: str) -> bool:
    if not cell or not name_norm:
        return False
    cell_norm = _norm_name(str(cell))
    if not cell_norm:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(name_norm)}(?!\w)", cell_norm))


def _people_from_cell(cell: str) -> List[Dict[str, str]]:
    raw = str(cell or "").strip()
    if not raw:
        return []

    out: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for part in re.split(r"[\n;/]+", raw):
        txt = " ".join(str(part or "").split())
        if not txt:
            continue

        role = ""
        name = txt
        m = _ROLE_NAME_RE.match(txt)
        if m:
            role = m.group(1).upper().replace(" ", "")
            name = " ".join(str(m.group(2) or "").split())

        key = _norm_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "name": name, "role": role})

    return out


def _format_people_ranges(
    per_day_people: Dict[str, Dict[str, Dict[str, Any]]]
) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for day, people in (per_day_people or {}).items():
        rows: List[Dict[str, str]] = []
        for info in (people or {}).values():
            merged = _merge_contiguous(list(info.get("intervals") or []))
            for start_dt, end_dt in merged:
                rows.append(
                    {
                        "name": str(info.get("name") or "").strip(),
                        "role": str(info.get("role") or "").strip(),
                        "start": _fmt(start_dt),
                        "end": _fmt(end_dt),
                    }
                )
        rows.sort(key=lambda row: (_parse_time_cell(row.get("start", "")) or datetime.min, row.get("name", "")))
        if rows:
            out[day] = rows
    return out


def _coerce_la_datetime(when: Optional[datetime] = None) -> datetime:
    if when is None:
        return datetime.now(_LA_TZ)
    if when.tzinfo is None:
        return when.replace(tzinfo=_LA_TZ)
    return when.astimezone(_LA_TZ)


def should_show_oncall_now(when: Optional[datetime] = None) -> bool:
    local = _coerce_la_datetime(when)
    day_canon = local.strftime("%A").lower()
    if day_canon in _WEEKEND_SET:
        return True
    if day_canon in _WEEKDAY_SET and local.hour >= 19:
        return True
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Day helpers
# ──────────────────────────────────────────────────────────────────────────────

_DAY_WORDS = {
    "monday": "monday", "mon": "monday",
    "tuesday": "tuesday", "tue": "tuesday", "tues": "tuesday",
    "wednesday": "wednesday", "wed": "wednesday",
    "thursday": "thursday", "thu": "thursday", "thur": "thursday", "thurs": "thursday",
    "friday": "friday", "fri": "friday",
    "saturday": "saturday", "sat": "saturday",
    "sunday": "sunday", "sun": "sunday",
}
_WEEK_ORDER_7 = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
_MMDD_TOKEN_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?!\d)")

def _canon_day_from_header(value: str) -> Optional[str]:
    """
    Row0 may contain "Monday".."Friday" (UNH/MC) or "Monday, 9/8/25" (On-Call).
    Return canonical day string or None.
    """
    s = (value or "").strip().lower()
    # keep letters, commas, spaces
    s = "".join(ch for ch in s if ch.isalpha() or ch.isspace() or ch == ",")
    head = s.split(",")[0].strip()
    return _DAY_WORDS.get(head)


def _week_bounds_for_grid(title: str) -> Optional[Tuple[date, date]]:
    try:
        return week_range_mod.week_range_from_title(str(title or ""), today=week_range_mod.la_today())
    except Exception:
        return None


def _day_from_date_token(value: str, week_bounds: Optional[Tuple[date, date]]) -> Optional[str]:
    if not value or not week_bounds:
        return None
    ws, we = week_bounds
    cur = ws
    day_lookup: Dict[Tuple[int, int], str] = {}
    while cur <= we:
        day_lookup[(cur.month, cur.day)] = cur.strftime("%A").lower()
        cur = cur + timedelta(days=1)

    for m in _MMDD_TOKEN_RE.finditer(str(value)):
        key = (int(m.group(1)), int(m.group(2)))
        if key in day_lookup:
            return day_lookup[key]
    return None


def _day_from_cell(value: str, week_bounds: Optional[Tuple[date, date]] = None) -> Optional[str]:
    return _canon_day_from_header(value) or _day_from_date_token(value, week_bounds)


def _scan_day_columns(
    grid: List[List[str]],
    *,
    week_bounds: Optional[Tuple[date, date]] = None,
    max_rows: int = 25,
    max_cols: int = 80,
) -> Dict[str, int]:
    best: Dict[str, int] = {}
    if not grid:
        return best

    row_limit = min(max_rows, len(grid))
    for r in range(row_limit):
        row = grid[r] or []
        row_map: Dict[str, int] = {}
        for c, value in enumerate(row[:max_cols]):
            day = _day_from_cell(value, week_bounds)
            if day and day not in row_map:
                row_map[day] = c
        if len(row_map) > len(best):
            best = row_map
            if len(best) >= 5:
                break
    return best


def _scan_day_hits_anywhere(
    grid: List[List[str]],
    *,
    week_bounds: Optional[Tuple[date, date]] = None,
    max_rows: int = 60,
    max_cols: int = 80,
) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not grid:
        return out

    row_limit = min(max_rows, len(grid))
    for r in range(row_limit):
        row = grid[r] or []
        for c, value in enumerate(row[:max_cols]):
            day = _day_from_cell(value, week_bounds)
            if day and day not in out:
                out[day] = c
    return out


def _infer_oncall_day_columns(
    grid: List[List[str]],
    *,
    week_bounds: Optional[Tuple[date, date]] = None,
) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not grid:
        return out

    first_range_row_for_col: Dict[int, int] = {}
    for r, row in enumerate(grid):
        for c, value in enumerate(row):
            if _RANGE_RE.match(str(value or "").strip()) and c not in first_range_row_for_col:
                first_range_row_for_col[c] = r

    for c, r0 in first_range_row_for_col.items():
        for r in range(r0, max(-1, r0 - 8), -1):
            value = (grid[r][c] if c < len(grid[r]) else "") or ""
            day = _day_from_cell(value, week_bounds)
            if day and day not in out:
                out[day] = c
                break
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Time parsing + formatting
# ──────────────────────────────────────────────────────────────────────────────

"""Time parsing

Google Sheets sometimes formats whole-hour times without minutes (e.g., "7 PM")
and/or omits the space before AM/PM (e.g., "7:00PM"). On-Call headers also use
these formats. We accept both H and H:MM forms.
"""

# Accept: 7 PM, 7PM, 7:00 PM, 7:00PM
_TIME_CELL_RE = re.compile(r"^\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM)\s*$", re.I)

# Accept: 7 PM - 12 AM, 7:00 PM–12:00 AM, etc.
_RANGE_RE = re.compile(
    r"^\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s*[-–]\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s*$",
    re.I,
)

def _parse_time_cell(s: str) -> Optional[datetime]:
    txt = (s or "").strip()
    if not txt:
        return None
    # Normalize missing space before AM/PM
    txt = re.sub(r"\s*(am|pm)\s*$", lambda m: f" {m.group(1).upper()}", txt, flags=re.I)
    # Try common formats
    for fmt in ("%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(txt, fmt)
        except Exception:
            continue
    return None

def _fmt(dt: datetime) -> str:
    try:
        return dt.strftime("%-I:%M %p")  # POSIX
    except Exception:
        return dt.strftime("%I:%M %p")   # Windows

# ──────────────────────────────────────────────────────────────────────────────
# Robust worksheet resolution (handles disconnects)
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_title(actuals: List[gspread.Worksheet], wanted: str) -> Optional[str]:
    want = (wanted or "").strip().lower()
    by_low = {w.title.strip().lower(): w.title for w in actuals}
    if want in by_low:
        return by_low[want]
    first = want.split()[0] if want else ""
    for w in actuals:
        t = w.title.strip()
        tl = t.lower()
        if tl == want or (first and tl.startswith(first)):
            return t
    return None

def _list_worksheets_with_retry(ss: gspread.Spreadsheet, attempts: int = 4, base_sleep: float = 0.4) -> Optional[List[gspread.Worksheet]]:
    """Retry listing worksheets to survive transient RemoteDisconnected."""
    for i in range(attempts):
        try:
            return ss.worksheets()
        except Exception:
            if i == attempts - 1:
                return None
            time.sleep(base_sleep * (2 ** i))
    return None
@st.cache_data(ttl=60, show_spinner=False)
def _cached_ws_titles(ss_id: str) -> list[str]:
    """
    Cached list of *visible* worksheet titles for this Spreadsheet id.
    Uses the Spreadsheet handle we stashed in session_state (set in app.py).
    """
    ss = st.session_state.get("_SS_HANDLE_BY_ID", {}).get(ss_id)
    if ss is None:
        return []
    try:
        lst = ss.worksheets()
    except Exception:
        return []
    titles = []
    for w in lst:
        hidden = bool(getattr(w, "_properties", {}).get("hidden", False))
        if not hidden:
            titles.append(w.title)
    return titles

def _open_three(ss: gspread.Spreadsheet) -> List[str]:
    """
    Return [UNH, MC, On-Call?] using a cached list of titles for 60s
    to avoid hammering ss.worksheets(). Falls back to a retry listing if cache empty.
    """
    unh_cfg, mc_cfg = OA_SCHEDULE_SHEETS[0], OA_SCHEDULE_SHEETS[1]
    out: List[str] = []

    # 1) Try cached titles first
    titles = _cached_ws_titles(getattr(ss, "id", ""))
    def _resolve_from_titles(all_titles: list[str], wanted: str) -> Optional[str]:
        want = (wanted or "").strip().lower()
        by_low = {t.strip().lower(): t for t in all_titles}
        if want in by_low:
            return by_low[want]
        first = want.split()[0] if want else ""
        for t in all_titles:
            tl = t.strip().lower()
            if tl == want or (first and tl.startswith(first)):
                return t
        return None

    if titles:
        unh = _resolve_from_titles(titles, unh_cfg)
        mc  = _resolve_from_titles(titles, mc_cfg)
        if unh: out.append(unh)
        if mc:  out.append(mc)
        # On-Call (prefer the tab for the current week; never use On Call General)
        # IMPORTANT: do NOT accidentally treat the roster sheet as On-Call.
        oncall = None
        deny = {str(AUDIT_SHEET).strip().lower(), str(LOCKS_SHEET).strip().lower(), str(ROSTER_SHEET).strip().lower()}

        def _looks_oncall(title: str) -> bool:
            tl = (title or '').lower()
            return bool(re.search(r'\bon\s*[- ]?\s*call\b', tl)) or ('oncall' in tl) or ('call' in tl and 'on' in tl)

        if _ONCALL_OVERRIDE and str(_ONCALL_OVERRIDE).strip():
            cand = _resolve_from_titles(titles, str(_ONCALL_OVERRIDE))
            if cand and cand.strip().lower() not in deny:
                oncall = cand

        # 1) Try exact week-range match for *this week* in Los Angeles.
        if not oncall:
            try:
                today = week_range_mod.la_today()
                # Our schedules use a Sunday–Saturday week.
                sunday_offset = (today.weekday() + 1) % 7  # Mon=0..Sun=6
                ws = today - timedelta(days=sunday_offset)
                we = ws + timedelta(days=6)
                for cand in titles:
                    tl = cand.strip().lower()
                    if tl in deny:
                        continue
                    if "general" in tl:
                        continue
                    if not _looks_oncall(cand):
                        continue
                    wr = week_range_mod.week_range_from_title(cand, today=today)
                    if wr and wr == (ws, we):
                        oncall = cand
                        break
            except Exception:
                pass

        # 2) Fallback: prefer a matching-looking title after MC, then any on-call.
        if not oncall:
            start = -1
            try:
                start = titles.index(mc) + 1 if mc in titles else -1
            except Exception:
                start = -1
            if start >= 0:
                for cand in titles[start:]:
                    tl = cand.strip().lower()
                    if tl in deny or "general" in tl:
                        continue
                    if _looks_oncall(cand):
                        oncall = cand
                        break
        if not oncall:
            for cand in titles:
                tl = cand.strip().lower()
                if tl in deny or "general" in tl:
                    continue
                if _looks_oncall(cand):
                    oncall = cand
                    break

        if oncall:
            out.append(oncall)

        # de-dup preserving order
        seen, final = set(), []
        for t in out:
            if t and t not in seen:
                seen.add(t); final.append(t)
        return final

    # 2) Fallback (rare): cached titles unavailable → old retry path
    ws_list = _list_worksheets_with_retry(ss)
    out = []
    if ws_list is not None:
        unh = _resolve_title(ws_list, unh_cfg)
        mc  = _resolve_title(ws_list, mc_cfg)
        if unh: out.append(unh)
        if mc:  out.append(mc)
        oncall = None
        deny = {str(AUDIT_SHEET).strip().lower(), str(LOCKS_SHEET).strip().lower(), str(ROSTER_SHEET).strip().lower()}

        def _looks_oncall_ws(title: str) -> bool:
            tl = (title or '').lower()
            return bool(re.search(r'\bon\s*[- ]?\s*call\b', tl)) or ('oncall' in tl) or ('call' in tl and 'on' in tl)

        if _ONCALL_OVERRIDE and str(_ONCALL_OVERRIDE).strip():
            cand = _resolve_title(ws_list, str(_ONCALL_OVERRIDE))
            if cand and cand.strip().lower() not in deny:
                oncall = cand

        # 1) Try exact week-range match for *this week* in Los Angeles.
        if not oncall:
            try:
                today = week_range_mod.la_today()
                sunday_offset = (today.weekday() + 1) % 7
                ws = today - timedelta(days=sunday_offset)
                we = ws + timedelta(days=6)
                for w in ws_list:
                    cand = w.title
                    tl = cand.strip().lower()
                    if tl in deny or "general" in tl:
                        continue
                    if not _looks_oncall_ws(cand):
                        continue
                    wr = week_range_mod.week_range_from_title(cand, today=today)
                    if wr and wr == (ws, we):
                        oncall = cand
                        break
            except Exception:
                pass

        # 2) Fallback: prefer neighbor after MC, then any on-call.
        if not oncall and mc:
            try:
                idx = next(i for i, w in enumerate(ws_list) if w.title == mc)
            except StopIteration:
                idx = -1
            if idx >= 0:
                for w in ws_list[idx+1:]:
                    cand = w.title
                    tl = cand.strip().lower()
                    if tl in deny or "general" in tl:
                        continue
                    if _looks_oncall_ws(cand):
                        oncall = cand
                        break

        if not oncall:
            for w in ws_list:
                cand = w.title
                tl = cand.strip().lower()
                if tl in deny or "general" in tl:
                    continue
                if _looks_oncall_ws(cand):
                    oncall = cand
                    break

        if oncall:
            out.append(oncall)

    # de-dup
    seen, final = set(), []
    for t in out:
        if t and t not in seen:
            seen.add(t); final.append(t)
    return final

# ──────────────────────────────────────────────────────────────────────────────
# 3) UNH/MC: 30-minute slots → merged ranges (exact algorithm requested)
# ──────────────────────────────────────────────────────────────────────────────

def _read_grid(ws: gspread.Worksheet) -> List[List[str]]:
    end_col_letter = a1.rowcol_to_a1(1, ONCALL_MAX_COLS).split("1")[0]
    return _safe_batch_get(ws, [f"A1:{end_col_letter}{ONCALL_MAX_ROWS}"])[0] or []

def _unh_mc_intervals(ws: gspread.Worksheet, name_norm: str) -> Dict[str, List[Tuple[datetime, datetime]]]:
    grid = _read_grid(ws)
    if not grid:
        return {}

    # 3.1 Identify structure
    day_cols = _scan_day_columns(grid)
    if len(day_cols) < 2:
        day_cols = _scan_day_hits_anywhere(grid)
    if not day_cols:
        return {}

    # Col 0 contains time row labels; find their row indices
    time_rows: List[int] = []
    for r, row in enumerate(grid):
        col0 = (row[0] if len(row) >= 1 else "") or ""
        if _TIME_CELL_RE.match(col0) and _parse_time_cell(col0):
            time_rows.append(r)

    if not time_rows:
        return {}

    # Sentinel at bottom
    time_rows.append(len(grid))

    # 3.2 Build “hit” intervals
    hits: Dict[str, List[Tuple[datetime, datetime]]] = {d: [] for d in day_cols}
    for i in range(len(time_rows) - 1):
        r0 = time_rows[i]
        r1 = time_rows[i + 1]

        start_label = (grid[r0][0] if len(grid[r0]) >= 1 else "") or ""
        start_dt = _parse_time_cell(start_label)
        if not start_dt:
            continue
        end_dt = start_dt + timedelta(minutes=30)

        for day, c in day_cols.items():
            if c == 0:
                continue  # skip time column

            # Count the slot if any lane cell in this band mentions the target OA.
            found = False
            for rr in range(r0 + 1, r1):
                val = grid[rr][c] if len(grid[rr]) > c else ""
                if val and _cell_has_name(str(val), name_norm):
                    found = True
                    break
            if found:
                hits[day].append((start_dt, end_dt))

    return hits

def _merge_contiguous(intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals.sort(key=lambda x: x[0])
    merged: List[Tuple[datetime, datetime]] = []
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if cur_e == s:        # contiguous half-hours
            cur_e = e
        elif e <= cur_e:      # overlaps/dups collapse
            continue
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return merged

def _unh_mc_ranges(ws: gspread.Worksheet, name_norm: str) -> Dict[str, List[Tuple[str, str]]]:
    intervals_by_day = _unh_mc_intervals(ws, name_norm)
    out: Dict[str, List[Tuple[str, str]]] = {}
    for day, ivals in intervals_by_day.items():
        merged = _merge_contiguous(ivals)
        if merged:
            out[day] = [(_fmt(s), _fmt(e)) for s, e in merged]
    return out


def _unh_mc_people_ranges(ws: gspread.Worksheet) -> Dict[str, List[Dict[str, str]]]:
    grid = _read_grid(ws)
    if not grid:
        return {}

    day_cols = _scan_day_columns(grid)
    if len(day_cols) < 2:
        day_cols = _scan_day_hits_anywhere(grid)
    if not day_cols:
        return {}

    time_rows: List[int] = []
    for r, row in enumerate(grid):
        col0 = (row[0] if len(row) >= 1 else "") or ""
        if _TIME_CELL_RE.match(col0) and _parse_time_cell(col0):
            time_rows.append(r)

    if not time_rows:
        return {}

    time_rows.append(len(grid))
    per_day_people: Dict[str, Dict[str, Dict[str, Any]]] = {d: {} for d in day_cols}

    for i in range(len(time_rows) - 1):
        r0 = time_rows[i]
        r1 = time_rows[i + 1]

        start_label = (grid[r0][0] if len(grid[r0]) >= 1 else "") or ""
        start_dt = _parse_time_cell(start_label)
        if not start_dt:
            continue
        end_dt = start_dt + timedelta(minutes=30)

        for day, c in day_cols.items():
            if c == 0:
                continue

            band_people: Dict[str, Dict[str, str]] = {}
            for rr in range(r0 + 1, r1):
                val = grid[rr][c] if len(grid[rr]) > c else ""
                for person in _people_from_cell(val):
                    band_people.setdefault(person["key"], person)

            for key, person in band_people.items():
                entry = per_day_people[day].setdefault(
                    key,
                    {
                        "name": person["name"],
                        "role": person["role"],
                        "intervals": [],
                    },
                )
                if not entry.get("role") and person.get("role"):
                    entry["role"] = person["role"]
                entry["intervals"].append((start_dt, end_dt))

    return _format_people_ranges(per_day_people)

# ──────────────────────────────────────────────────────────────────────────────
# 4) On-Call: 4/5-hour blocks (time range label followed by names)
# ──────────────────────────────────────────────────────────────────────────────

def _oncall_blocks(ws: gspread.Worksheet, name_norm: str) -> Dict[str, List[Tuple[str, str]]]:
    grid = _read_grid(ws)
    if not grid:
        return {}

    # 4.1 Identify structure from weekday headers and/or date-only headers.
    week_bounds = _week_bounds_for_grid(getattr(ws, "title", ""))
    day_cols = _scan_day_columns(grid, week_bounds=week_bounds, max_rows=15)
    if len(day_cols) < 2:
        for day, col in _scan_day_hits_anywhere(grid, week_bounds=week_bounds, max_rows=30).items():
            day_cols.setdefault(day, col)
    if len(day_cols) < 2:
        for day, col in _infer_oncall_day_columns(grid, week_bounds=week_bounds).items():
            day_cols.setdefault(day, col)
    if not day_cols:
        return {}

    per_day: Dict[str, Set[Tuple[str, str]]] = {d: set() for d in day_cols}

    # 4.2 Extract blocks per day
    for day, c in day_cols.items():
        label_rows: List[int] = []
        for r in range(len(grid)):
            col0 = (grid[r][0] if len(grid[r]) > 0 else "") or ""
            cell = (grid[r][c] if len(grid[r]) > c else "") or ""
            if _RANGE_RE.match(str(col0).strip()) or _RANGE_RE.match(str(cell).strip()):
                label_rows.append(r)

        if not label_rows:
            continue
        label_rows.append(len(grid))

        for i in range(len(label_rows) - 1):
            r_label = label_rows[i]
            r_next = label_rows[i + 1]
            col0 = (grid[r_label][0] if len(grid[r_label]) > 0 else "") or ""
            cell = (grid[r_label][c] if len(grid[r_label]) > c else "") or ""
            range_txt = cell if _RANGE_RE.match(str(cell).strip()) else col0
            m = _RANGE_RE.match(range_txt) if range_txt else None
            if m:
                s_raw, e_raw = m.group(1), m.group(2)
                sdt, edt = _parse_time_cell(s_raw), _parse_time_cell(e_raw)
                if not (sdt and edt):
                    continue
                current_range = (_fmt(sdt), _fmt(edt))
            else:
                continue

            for rr in range(r_label + 1, r_next):
                lane_cell = (grid[rr][c] if len(grid[rr]) > c else "") or ""
                if _cell_has_name(lane_cell, name_norm):
                    per_day[day].add(current_range)
                    break

    # Dedup + sort by start time
    out: Dict[str, List[Tuple[str, str]]] = {}
    for d, blocks in per_day.items():
        sorted_blocks = sorted(blocks, key=lambda ab: _parse_time_cell(ab[0]) or datetime.min)
        if sorted_blocks:
            out[d] = list(sorted_blocks)
    return out


def _oncall_people_ranges(ws: gspread.Worksheet) -> Dict[str, List[Dict[str, str]]]:
    grid = _read_grid(ws)
    if not grid:
        return {}

    week_bounds = _week_bounds_for_grid(getattr(ws, "title", ""))
    day_cols = _scan_day_columns(grid, week_bounds=week_bounds, max_rows=15)
    if len(day_cols) < 2:
        for day, col in _scan_day_hits_anywhere(grid, week_bounds=week_bounds, max_rows=30).items():
            day_cols.setdefault(day, col)
    if len(day_cols) < 2:
        for day, col in _infer_oncall_day_columns(grid, week_bounds=week_bounds).items():
            day_cols.setdefault(day, col)
    if not day_cols:
        return {}

    per_day_people: Dict[str, Dict[str, Dict[str, Any]]] = {d: {} for d in day_cols}

    for day, c in day_cols.items():
        label_rows: List[int] = []
        for r in range(len(grid)):
            col0 = (grid[r][0] if len(grid[r]) > 0 else "") or ""
            cell = (grid[r][c] if len(grid[r]) > c else "") or ""
            if _RANGE_RE.match(str(col0).strip()) or _RANGE_RE.match(str(cell).strip()):
                label_rows.append(r)

        if not label_rows:
            continue
        label_rows.append(len(grid))

        for i in range(len(label_rows) - 1):
            r_label = label_rows[i]
            r_next = label_rows[i + 1]
            col0 = (grid[r_label][0] if len(grid[r_label]) > 0 else "") or ""
            cell = (grid[r_label][c] if len(grid[r_label]) > c else "") or ""
            range_txt = cell if _RANGE_RE.match(str(cell).strip()) else col0
            m = _RANGE_RE.match(range_txt) if range_txt else None
            if not m:
                continue

            start_dt = _parse_time_cell(m.group(1))
            end_dt = _parse_time_cell(m.group(2))
            if not (start_dt and end_dt):
                continue

            band_people: Dict[str, Dict[str, str]] = {}
            for rr in range(r_label + 1, r_next):
                lane_cell = (grid[rr][c] if len(grid[rr]) > c else "") or ""
                for person in _people_from_cell(lane_cell):
                    band_people.setdefault(person["key"], person)

            for key, person in band_people.items():
                entry = per_day_people[day].setdefault(
                    key,
                    {
                        "name": person["name"],
                        "role": person["role"],
                        "intervals": [],
                    },
                )
                if not entry.get("role") and person.get("role"):
                    entry["role"] = person["role"]
                entry["intervals"].append((start_dt, end_dt))

    return _format_people_ranges(per_day_people)

# ──────────────────────────────────────────────────────────────────────────────
# Public API used by app.py
# ──────────────────────────────────────────────────────────────────────────────

def get_user_schedule(ss: gspread.Spreadsheet, _schedule_unused, oa_name: str) -> Dict[str, Dict[str, List[Tuple[str, str]]]]:
    """
    Returns:
      {
        'monday': {'UNH': [(start,end),...], 'MC': [...], 'On-Call': [('start','end'), ...]},
        ...
      }
    """
    if getattr(config, "SUMMER_MODE", False):
        result: Dict[str, Dict[str, List]] = {
            d: {"UNH": [], "MC": [], "On-Call": []} for d in _WEEK_ORDER_7
        }

        try:
            found = summer_schedule.list_person_shifts(ss, oa_name).get(oa_name, [])
        except Exception:
            return result

        seen = set()

        for actual_date, tab_title, start, end in found:
            day = actual_date.strftime("%A").lower()

            key = (actual_date.isoformat(), start, end, tab_title)
            if key in seen:
                continue
            seen.add(key)

            result[day]["On-Call"].append(
                {
                    "start": start,
                    "end": end,
                    "date": actual_date.isoformat(),
                    "tab": tab_title,
                    "source": "Summer"
                }
            )

        return result   
    titles = _open_three(ss)
    result: Dict[str, Dict[str, List[Tuple[str, str]]]] = {
        d: {"UNH": [], "MC": [], "On-Call": []} for d in _WEEK_ORDER_7
    }

    if not titles:
        return result

    name_norm = _norm_name(oa_name)

    # UNH
    if len(titles) >= 1:
        try:
            ws_unh = ss.worksheet(titles[0])
            unh_ranges = _unh_mc_ranges(ws_unh, name_norm)  # Mon–Fri
            for d, blocks in unh_ranges.items():
                result[d]["UNH"] = blocks
        except Exception:
            pass

    # MC
    if len(titles) >= 2:
        try:
            ws_mc = ss.worksheet(titles[1])
            mc_ranges = _unh_mc_ranges(ws_mc, name_norm)  # Mon–Fri
            for d, blocks in mc_ranges.items():
                result[d]["MC"] = blocks
        except Exception:
            pass

    # On-Call (neighbor / override)
    if len(titles) >= 3:
        try:
            ws_on = ss.worksheet(titles[2])
            oc = _oncall_blocks(ws_on, name_norm)  # Sun–Sat
            for d, blocks in oc.items():
                result[d]["On-Call"].extend(blocks)
        except Exception:
            pass

    return result


def get_user_schedule_for_titles(
    ss: gspread.Spreadsheet,
    _schedule_unused,
    oa_name: str,
    *,
    unh_title: str | None = None,
    mc_title: str | None = None,
    oncall_title: str | None = None,
) -> Dict[str, Dict[str, List[Tuple[str, str]]]]:
    """Return a user's schedule, but for explicit worksheet titles.

    This is used for pickup validation so we can evaluate caps / conflicts against
    the *same week* the callout came from (UNH/MC + the matching On-Call tab),
    instead of relying on the neighbor-tab heuristic.
    """

    result: Dict[str, Dict[str, List[Tuple[str, str]]]] = {
        d: {"UNH": [], "MC": [], "On-Call": []} for d in _WEEK_ORDER_7
    }

    name_norm = _norm_name(oa_name)

    # UNH
    if unh_title:
        try:
            ws_unh = ss.worksheet(unh_title)
            unh_ranges = _unh_mc_ranges(ws_unh, name_norm)  # Mon–Fri
            for d, blocks in unh_ranges.items():
                result[d]["UNH"] = blocks
        except Exception:
            pass

    # MC
    if mc_title:
        try:
            ws_mc = ss.worksheet(mc_title)
            mc_ranges = _unh_mc_ranges(ws_mc, name_norm)  # Mon–Fri
            for d, blocks in mc_ranges.items():
                result[d]["MC"] = blocks
        except Exception:
            pass

    # On-Call
    if oncall_title:
        try:
            ws_on = ss.worksheet(oncall_title)
            oc = _oncall_blocks(ws_on, name_norm)  # Sun–Sat
            for d, blocks in oc.items():
                result[d]["On-Call"].extend(blocks)
        except Exception:
            pass

    return result

# ──────────────────────────────────────────────────────────────────────────────
# Minimal, polished tabular rendering (weekly + per-day tables) — for chat
# ──────────────────────────────────────────────────────────────────────────────

def _time_in_named_window(start_txt: str, end_txt: str, when: datetime) -> bool:
    start_dt = _parse_time_cell(start_txt)
    end_dt = _parse_time_cell(end_txt)
    if not (start_dt and end_dt):
        return False

    now_min = (when.hour * 60) + when.minute
    start_min = (start_dt.hour * 60) + start_dt.minute
    end_min = (end_dt.hour * 60) + end_dt.minute

    if end_min <= start_min:
        end_min += 24 * 60
        if now_min < start_min:
            now_min += 24 * 60

    return start_min <= now_min < end_min


def _working_entries_for_day(
    rows: List[Dict[str, str]],
    *,
    source: str,
    when: datetime,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in rows or []:
        start_txt = str(row.get("start") or "").strip()
        end_txt = str(row.get("end") or "").strip()
        if not start_txt or not end_txt:
            continue
        if not _time_in_named_window(start_txt, end_txt, when):
            continue
        out.append(
            {
                "source": source,
                "name": str(row.get("name") or "").strip(),
                "role": str(row.get("role") or "").strip(),
                "start": start_txt,
                "end": end_txt,
            }
        )
    out.sort(key=lambda row: (_parse_time_cell(row.get("start", "")) or datetime.min, row.get("name", "")))
    return out


def get_people_working_now(
    ss: gspread.Spreadsheet,
    *,
    when: Optional[datetime] = None,
) -> Dict[str, Any]:
    local_now = _coerce_la_datetime(when)
    day_canon = local_now.strftime("%A").lower()
    titles = _open_three(ss)
    show_oncall = should_show_oncall_now(local_now)

    result: Dict[str, Any] = {
        "day": day_canon,
        "display_mode": "oncall" if show_oncall else "campus",
        "when_local": local_now.isoformat(timespec="minutes"),
        "when_label": local_now.strftime("%A, %b %d at %I:%M %p").replace(" 0", " "),
        "entries": [],
        "sources": [],
        "sheet_titles": {},
    }

    if show_oncall:
        result["sources"] = ["On-Call"]
        if len(titles) < 3:
            return result
        result["sheet_titles"]["On-Call"] = titles[2]
        try:
            ws_on = ss.worksheet(titles[2])
            per_day = _oncall_people_ranges(ws_on)
            result["entries"] = _working_entries_for_day(
                per_day.get(day_canon, []),
                source="On-Call",
                when=local_now,
            )
        except Exception:
            result["entries"] = []
        return result

    source_specs = []
    if len(titles) >= 1:
        source_specs.append(("UNH", titles[0]))
    if len(titles) >= 2:
        source_specs.append(("MC", titles[1]))

    result["sources"] = [src for src, _ in source_specs]
    for source, title in source_specs:
        result["sheet_titles"][source] = title
        try:
            ws = ss.worksheet(title)
            per_day = _unh_mc_people_ranges(ws)
            result["entries"].extend(
                _working_entries_for_day(
                    per_day.get(day_canon, []),
                    source=source,
                    when=local_now,
                )
            )
        except Exception:
            continue

    result["entries"].sort(
        key=lambda row: (
            result["sources"].index(row.get("source", "")) if row.get("source", "") in result["sources"] else 99,
            _parse_time_cell(row.get("start", "")) or datetime.min,
            row.get("name", ""),
        )
    )
    return result


def _mins_between(s: str, e: str) -> int:
    try:
        sd = datetime.strptime(s, "%I:%M %p")
        ed = datetime.strptime(e, "%I:%M %p")
    except Exception:
        return 0
    if ed <= sd:  # allow on-call to roll past midnight
        ed += timedelta(days=1)
    return int((ed - sd).total_seconds() // 60)

def _sum_ranges_minutes(ranges: List[Tuple[str, str]]) -> int:
    return sum(_mins_between(s, e) for (s, e) in _iter_time_pairs(ranges))

def _fmt_hours(mins: int) -> str:
    h, m = mins // 60, mins % 60
    return f"{h}h" if m == 0 else f"{h}h {m}m"

def _join_chips(ranges: List[Tuple[str, str]]) -> str:
    # compact “chips” for time blocks
    return ", ".join(f"`{s} – {e}`" for (s, e) in _iter_time_pairs(ranges))

def _weekly_totals(user_sched: Dict[str, Dict[str, List[Tuple[str, str]]]]) -> Dict[str, int]:
    totals = {"UNH": 0, "MC": 0, "On-Call": 0}
    for buckets in user_sched.values():
        totals["UNH"]    += _sum_ranges_minutes(buckets.get("UNH", []))
        totals["MC"]     += _sum_ranges_minutes(buckets.get("MC", []))
        totals["On-Call"]+= _sum_ranges_minutes(buckets.get("On-Call", []))
    return totals

def _render_weekly_summary_table(user_sched: Dict[str, Dict[str, List[Tuple[str, str]]]]) -> List[str]:
    totals = _weekly_totals(user_sched)
    lines = []
    lines.append("## Weekly Summary")
    lines.append("| Source | Hours |")
    lines.append("|:------:|:-----:|")
    lines.append(f"| UNH | **{_fmt_hours(totals['UNH'])}** |")
    lines.append(f"| MC | **{_fmt_hours(totals['MC'])}** |")
    lines.append(f"| On-Call | **{_fmt_hours(totals['On-Call'])}** |")
    return lines

def _render_day_table(day: str, buckets: Dict[str, List[Tuple[str, str]]]) -> List[str]:
    # Only include rows for sources that have blocks
    rows = []
    if buckets.get("UNH"):
        mins = _sum_ranges_minutes(buckets["UNH"])
        rows.append(("UNH", _join_chips(buckets["UNH"]), _fmt_hours(mins)))
    if buckets.get("MC"):
        mins = _sum_ranges_minutes(buckets["MC"])
        rows.append(("MC", _join_chips(buckets["MC"]), _fmt_hours(mins)))
    if buckets.get("On-Call"):
        mins = _sum_ranges_minutes(buckets["On-Call"])
        rows.append(("On-Call", _join_chips(buckets["On-Call"]), _fmt_hours(mins)))

    if not rows:
        return []

    lines = []
    lines.append(f"### {day.title()}")
    lines.append("| Source | Time Blocks | Total |")
    lines.append("|:------:|:-----------|:-----:|")
    for src, chips, total in rows:
        lines.append(f"| {src} | {chips} | **{total}** |")
    return lines

def render_user_schedule_markdown(
    user_sched: Dict[str, Dict[str, List[Tuple[str, str]]]],
    *,
    include_weekly_summary: bool = True
) -> str:
    """
    Clean tabular layout for chat:
      • Weekly Summary table (optional)
      • One compact table per day that has shifts
    """
    day_order = list(_WEEK_ORDER_7)
    blocks: List[str] = []

    if include_weekly_summary:
        blocks.extend(_render_weekly_summary_table(user_sched))
        blocks.append("")  # spacer

    any_day = False
    for d in day_order:
        section = _render_day_table(d, user_sched.get(d, {}))
        if section:
            any_day = True
            blocks.extend(section)
            blocks.append("")  # spacer between days

    if not any_day:
        return "_No shifts found for your name._"

    # trim last blank line
    if blocks and blocks[-1] == "":
        blocks.pop()

    return "\n".join(blocks)

def chat_schedule_response(ss, schedule_unused, oa_name: str) -> str:
    data = get_user_schedule(ss, schedule_unused, oa_name)
    # set include_weekly_summary=False if you prefer only per-day tables
    return render_user_schedule_markdown(data, include_weekly_summary=True)

# ──────────────────────────────────────────────────────────────────────────────
# PICTORIAL / TABULAR RENDERING (MAIN APP PANE)
# ──────────────────────────────────────────────────────────────────────────────

# Brand-ish colors for sources (tweak to taste)
_SOURCE_COLOR = {
    "UNH": "#4F46E5",      # indigo-600
    "MC": "#16A34A",       # green-600
    "On-Call": "#F59E0B",  # amber-500
    "Summer": "#4F46E5",  # indigo-600 (same as UNH, but labeled "Summer" in chat)
}

_DAY_ORDER = list(_WEEK_ORDER_7)
_DAY_TITLE = {d: d.title() for d in _DAY_ORDER}

def _parse_time_for_dt(t: str) -> datetime:
    return datetime.strptime(t.strip(), "%I:%M %p")

def _anchor_dt(t: str, anchor: date) -> datetime:
    base = _parse_time_for_dt(t)
    return datetime(anchor.year, anchor.month, anchor.day, base.hour, base.minute, base.second)


def _extract_time_pair(block: Any) -> Optional[Tuple[str, str]]:
    """Best-effort normalize a schedule 'block' into (start_12h, end_12h).

    We sometimes see blocks come through as:
      - (start, end)
      - (start, end, ...)  # extra metadata
      - "7:00 AM - 9:00 AM"
      - {"start": "...", "end": "..."}  (rare)
    """
    if block is None:
        return None

    s = e = None

    if isinstance(block, dict):
        s = block.get("start") or block.get("s")
        e = block.get("end") or block.get("e")
    elif isinstance(block, (list, tuple)):
        if len(block) >= 2:
            s, e = block[0], block[1]
    elif isinstance(block, str):
        mm = _RANGE_RE.match(block.strip())
        if mm:
            s, e = mm.group(1), mm.group(2)

    if not (s and e):
        return None

    # Coerce datetimes into formatted strings
    try:
        if isinstance(s, datetime):
            s = _fmt(s)
        if isinstance(e, datetime):
            e = _fmt(e)
    except Exception:
        pass

    def _coerce_12h(t: str) -> str:
        raw = (t or "").strip()
        if not raw:
            return raw
        # "7:00AM" -> "7:00 AM"
        m1 = re.match(r"^(\d{1,2}:\d{2})\s*(AM|PM)$", raw, re.I)
        if m1:
            try:
                dt = datetime.strptime(f"{m1.group(1)} {m1.group(2).upper()}", "%I:%M %p")
                return _fmt(dt)
            except Exception:
                return f"{m1.group(1)} {m1.group(2).upper()}"
        # 24h "13:30" -> "1:30 PM"
        if re.fullmatch(r"\d{1,2}:\d{2}", raw):
            try:
                dt = datetime.strptime(raw, "%H:%M")
                return _fmt(dt)
            except Exception:
                return raw
        return raw

    s = _coerce_12h(str(s).strip())
    e = _coerce_12h(str(e).strip())
    if not s or not e:
        return None
    return (s, e)


def _iter_time_pairs(seq) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for blk in (seq or []):
        pair = _extract_time_pair(blk)
        if pair:
            out.append(pair)
    return out

def build_schedule_dataframe(user_sched: Dict[str, Dict[str, List[Tuple[str, str]]]]) -> pd.DataFrame:
    """
    Flatten parsed schedule into a DataFrame:
    Columns: Day, Source, Start, End, Date, DurationMin, Duration
    Plot-only: PlotStartDT, PlotEndDT
    """
    rows = []
    today = week_range_mod.la_today()
    anchor_sun = today - timedelta(days=((today.weekday() + 1) % 7))
    anchors = {
        "sunday":    anchor_sun,
        "monday":    anchor_sun + timedelta(days=1),
        "tuesday":   anchor_sun + timedelta(days=2),
        "wednesday": anchor_sun + timedelta(days=3),
        "thursday":  anchor_sun + timedelta(days=4),
        "friday":    anchor_sun + timedelta(days=5),
        "saturday":  anchor_sun + timedelta(days=6),
    }

    for day in _DAY_ORDER:
        buckets = user_sched.get(day, {})
        for src in ("UNH", "MC", "On-Call"):
            for block in (buckets.get(src, []) or []):
                pair = _extract_time_pair(block)
                if not pair:
                    continue

                s, e = pair

                # Normal old schedule behavior: anchor to current week.
                anchor_date = anchors[day]

                # Summer schedule behavior: preserve the real date from the parser.
                if isinstance(block, dict) and block.get("date"):
                    try:
                        anchor_date = date.fromisoformat(str(block["date"]))
                    except Exception:
                        anchor_date = anchors[day]

                plot_start = _anchor_dt(s, anchor_date)
                plot_end = _anchor_dt(e, anchor_date)

                if plot_end <= plot_start:
                    plot_end += timedelta(days=1)

                dur_min = int((plot_end - plot_start).total_seconds() // 60)

                display_source = "Summer" if isinstance(block, dict) and block.get("source") == "Summer" else src

                rows.append({
                    "Day": _DAY_TITLE[day],
                    "Source": display_source,
                    "Start": s,
                    "End": e,
                    "Date": plot_start.date(),
                    "DurationMin": dur_min,
                    "Duration": (
                        f"{dur_min//60}h"
                        if dur_min % 60 == 0
                        else f"{dur_min//60}h {dur_min%60}m"
                    ),
                    "PlotStartDT": plot_start,
                    "PlotEndDT": plot_end,
                })

    if not rows:
        return pd.DataFrame(columns=[
            "Day","Source","Start","End","Date","DurationMin","Duration",
            "PlotStartDT","PlotEndDT"
        ])

    df = pd.DataFrame(rows)

    df.drop_duplicates(
        subset=["Date", "Day", "Source", "Start", "End"],
        inplace=True,
    )

    df["DayOrder"] = df["Day"].map({
        v: i for i, v in enumerate([_DAY_TITLE[d] for d in _DAY_ORDER])
    })

    df.sort_values(["Date", "DayOrder", "PlotStartDT", "Source"], inplace=True, kind="stable")
    df.drop(columns=["DayOrder"], inplace=True)

    return df

def render_schedule_viz(st, df: pd.DataFrame, *, title: str = "This Week's Schedule"):
    """
    Calendar view:
      • X-axis: Days (Sun → Sat) with day+date labels shown at the top
      • Y-axis: Time (7:00 AM → 12:00 AM) in 30-min increments
      • Narrow colored blocks with centered labels: time range + source
      • Vertical separator lines between days
    """
    if df.empty:
        st.info("No shifts found for your name.")
        return
    
    # Summer mode: the full schedule table can contain the whole summer,
    # but this chart is labeled "This Week", so only show one Sunday-Saturday week.
    try:
        from .. import config

        if getattr(config, "SUMMER_MODE", False) and "Date" in df.columns:
            today = week_range_mod.la_today()
            week_start = today - timedelta(days=((today.weekday() + 1) % 7))
            week_end = week_start + timedelta(days=6)

            df = df[
                (pd.to_datetime(df["Date"]).dt.date >= week_start)
                & (pd.to_datetime(df["Date"]).dt.date <= week_end)
            ].copy()

            if df.empty:
                st.info("No shifts found for your name this week.")
                return
    except Exception:
        pass

    try:
        import plotly.graph_objects as go
    except ImportError:
        st.info("📈 Install Plotly to enable the pictorial timeline: `pip install plotly`")
        return

    # ---- Days present (Sun→Sat order) ----
    day_titles = [_DAY_TITLE[d] for d in _DAY_ORDER]
    days_present = [d for d in day_titles if d in df["Day"].unique().tolist()]
    if not days_present:
        st.info("No shifts found for your name.")
        return

    # Map each day to a representative date
    day_to_date = (
        df.sort_values(["Day", "PlotStartDT"])
          .groupby("Day", as_index=False)
          .first()[["Day", "PlotStartDT"]]
    )
    day_to_date = {r["Day"]: r["PlotStartDT"].date() for _, r in day_to_date.iterrows()}

    def _fmt_day_with_date(day_name: str) -> str:
        d = day_to_date.get(day_name)
        return f"{day_name}<br>{d.strftime('%b %d')}" if d else day_name

    x_ticktext = [_fmt_day_with_date(d) for d in days_present]

    # ---- Helpers ----
    def _to_minutes(dt: datetime) -> int:
        m = dt.hour * 60 + dt.minute
        return 24 * 60 if m == 0 else m   # midnight → 1440

    def _label_time_range(start_dt: datetime, end_dt: datetime) -> str:
        """Return a clean time range label, e.g., '7–12 AM' or '11 AM–1 PM'."""
        def fmt(H: int, M: int):
            H12 = 12 if (H % 12) == 0 else (H % 12)
            return f"{H12}" if M == 0 else f"{H12}:{M:02d}"

        sH, sM = start_dt.hour, start_dt.minute
        eH, eM = end_dt.hour, end_dt.minute
        if eH == 0 and eM == 0:
            eH, eM = 24, 0  # treat midnight as 24:00

        s_ampm = "AM" if sH < 12 else "PM"
        e_ampm = "AM" if (eH % 24) < 12 else "PM"

        if s_ampm == e_ampm:
            return f"{fmt(sH, sM)}–{fmt(eH, eM)} {s_ampm}"
        else:
            return f"{fmt(sH, sM)} {s_ampm}–{fmt(eH, eM)} {e_ampm}"

    # ---- Build bar traces ----
    bars = []
    seen_source = set()
    for _, r in df.iterrows():
        day = r["Day"]
        if day not in days_present:
            continue

        start_min = max(_to_minutes(r["PlotStartDT"]), 7 * 60)
        end_min   = min(_to_minutes(r["PlotEndDT"]), 24 * 60)
        if end_min <= start_min:
            continue

        start_hr = start_min / 60.0
        dur_hr   = (end_min - start_min) / 60.0

        color = _SOURCE_COLOR.get(r["Source"], "#6b7280")
        showlegend = r["Source"] not in seen_source
        seen_source.add(r["Source"])

        label = f"{_label_time_range(r['PlotStartDT'], r['PlotEndDT'])}<br>{r['Source']}"

        bars.append(go.Bar(
            x=[day],
            y=[dur_hr],
            base=[start_hr],
            marker=dict(color=color, line=dict(width=0)),
            width=0.38,
            name=r["Source"],
            showlegend=showlegend,
            text=[label],
            texttemplate="%{text}",
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(color="white", size=11),
            hovertemplate=(f"{day} ({day_to_date.get(day,'')})<br>"
                           f"{r['Source']}<br>"
                           f"{r['Start']} – {r['End']}<extra></extra>"),
            opacity=0.98,
        ))

    fig = go.Figure(bars)

    # ---- Y axis ticks (30-min increments) ----
    y_ticks = [7 + i * 0.5 for i in range(int((24 - 7) / 0.5) + 1)]
    def _fmt_h(h):
        total_min = int(round(h * 60))
        H, M = divmod(total_min, 60)
        if H == 24: H = 0
        ampm = "AM" if H < 12 else "PM"
        H12 = 12 if H % 12 == 0 else H % 12
        return f"{H12}:{M:02d} {ampm}"

    y_text = [_fmt_h(h) for h in y_ticks]

    fig.update_xaxes(
        title="",
        type="category",
        tickmode="array",
        tickvals=days_present,
        ticktext=x_ticktext,
        showline=True,
        linecolor="#e5e7eb",
        ticks="outside",
        tickfont=dict(size=12),
        side="top",  # move labels to top
    )

    fig.update_yaxes(
        title="Time",
        tickmode="array",
        tickvals=y_ticks,
        ticktext=y_text,
        range=[24, 7],
        showgrid=True,
        gridcolor="#eef2f7",
        zeroline=False,
    )

    # ---- Vertical separator lines between days ----
    for i in range(1, len(days_present)):
        fig.add_vline(
            x=i - 0.5, line_width=1, line_dash="dot", line_color="#cccccc"
        )

    fig.update_layout(
        title=title,
        barmode="overlay",
        bargap=0.18,
        bargroupgap=0.05,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend_title_text="",
        margin=dict(l=10, r=10, t=60, b=10),
        height=max(820, int(26 * len(y_ticks))),
    )

    st.plotly_chart(fig, use_container_width=True, theme="streamlit")
def render_schedule_dataframe(st, df: pd.DataFrame):
    """
    Show a simple dataframe below the chart.
    Columns: Date, Day, Source, Start, End, Duration
    """
    if df.empty:
        st.info("No shifts found for your name.")
        return

    cols = ["Date", "Day", "Source", "Start", "End", "Duration"]
    show = df[cols].copy()

    # Sort nicely by date, then source, then start time
    show.sort_values(["Date", "Day", "Source"], inplace=True)

    st.markdown("### Full Schedule Table")
    st.dataframe(show, use_container_width=True)
