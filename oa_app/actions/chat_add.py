# oa_app/chat_add.py
from __future__ import annotations
from datetime import datetime, timedelta, time as dtime, date as ddate
from typing import List, Optional, Tuple, Dict, Callable
import re
import streamlit as st
import gspread
from ..core.hours import compute_hours_fast, total_hours_from_unh_mc_and_neighbor
from ..core.quotas import bump_ws_version, seed_batch_get_cache

from ..core.locks import get_or_create_locks_sheet, acquire_fcfs_lock, lock_key
from ..core.utils import fmt_time
from ..config import ONCALL_MAX_COLS, ONCALL_MAX_ROWS, ROSTER_SHEET, AUDIT_SHEET, LOCKS_SHEET
from ..ui.schedule_query import (
    get_user_schedule,
    _read_grid,
    _canon_day_from_header,
    _TIME_CELL_RE,
    _parse_time_cell,
    _RANGE_RE,
    _cached_ws_titles,
)

# ───────────────────────── Campus aliases ─────────────────────────
def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower().strip() if ch.isalnum() or ch.isspace())

_MC_ALIASES = {"mc", "main", "main campus", "maincampus"}
_UNH_ALIASES = {"unh", "uhall", "u hall", "university health", "uh"}
_ONCALL_ALIASES = {"oncall", "on-call", "on call", "oc"}

def _alias_in_text(text: Optional[str], aliases: set[str]) -> bool:
    if not text:
        return False
    s = _norm(text)
    s = re.sub(r"\s+", " ", s)
    padded = f" {s} "
    for a in aliases:
        a_norm = re.sub(r"\s+", " ", _norm(a))
        if f" {a_norm} " in padded:
            return True
    return False

def _resolve_campus_title(ss, requested_campus_or_tab: Optional[str], sidebar_tab: Optional[str]) -> Tuple[str, str]:
    titles = _cached_ws_titles(getattr(ss, "id", "")) or []
    if not titles:
        try:
            titles = [w.title for w in ss.worksheets() if not getattr(w, "_properties", {}).get("hidden", False)]
        except Exception:
            titles = []
    if not titles:
        raise ValueError("No visible tabs available right now (could not read worksheet list).")

    # Exclude non-schedule sheets from default selection
    deny = {str(ROSTER_SHEET).strip().lower(), str(AUDIT_SHEET).strip().lower(), str(LOCKS_SHEET).strip().lower()}
    titles = [t for t in titles if (t or '').strip().lower() not in deny]
    if not titles:
        raise ValueError('No schedule tabs available right now (only utility sheets found).')

    s = (requested_campus_or_tab or "").strip()
    if s:
        if _alias_in_text(s, _MC_ALIASES):
            for t in titles:
                if "mc" in t.lower():
                    return t, "MC"
        if _alias_in_text(s, _UNH_ALIASES):
            for t in titles:
                tl = t.lower()
                if "unh" in tl or "hall" in tl:
                    return t, "UNH"
        if _alias_in_text(s, _ONCALL_ALIASES):
            for t in titles:
                if "call" in t.lower():
                    return t, "ONCALL"
        low = s.lower()
        for t in titles:
            tl = t.lower()
            if tl == low or tl.startswith(low) or (low in tl):
                kind = "ONCALL" if ("on" in tl and "call" in tl) else ("MC" if ("mc" in tl or "main" in tl) else "UNH")
                return t, kind

        # If the request explicitly named a campus/tab but we couldn't map it,
        # DO NOT fall back to the approver's sidebar tab — that would apply the
        # request to the wrong campus. Fail loudly so the request can be fixed.
        sample = ", ".join(titles[:8])
        raise ValueError(
            f"Could not map campus/tab '{s}' to a worksheet title. "
            f"Available tabs (sample): {sample}"
        )

    if sidebar_tab:
        low = sidebar_tab.lower()
        if "call" in low: return sidebar_tab, "ONCALL"
        if "mc" in low or "main" in low: return sidebar_tab, "MC"
        if "unh" in low or "hall" in low: return sidebar_tab, "UNH"

    first = titles[0]
    low = first.lower()
    kind = "ONCALL" if ("on" in low and "call" in low) else ("MC" if ("mc" in low or "main" in low) else "UNH")
    return first, kind

# ───────────────────────── Time helpers ─────────────────────────
def _ensure_dt(x, ref_date=None) -> datetime:
    if isinstance(x, datetime):
        return x.replace(second=0, microsecond=0)
    if isinstance(x, dtime):
        d = ref_date or datetime.today().date()
        return datetime.combine(d, x).replace(second=0, microsecond=0)
    raise TypeError(f"Expected datetime or time, got {type(x)}")

def _is_half_hour_boundary_dt(dt: datetime) -> bool:
    return dt.minute in (0, 30) and dt.second == 0 and dt.microsecond == 0

def _range_to_slots(start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
    out: List[Tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = cur + timedelta(minutes=30)
        out.append((cur, nxt))
        cur = nxt
    return out

def _is_blankish(v) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s in {"-", "–", "—", "·", ".", "n/a", "N/A"}


def _cell_has_name(v: object, canon_target_name: str) -> bool:
    """True if a cell already contains the target name (case-insensitive).

    Cells typically look like "OA: Name" but may include extra text.
    We treat an exact word-boundary match on the normalized name as present.
    """
    if v is None:
        return False
    s = str(v).strip()
    if not s:
        return False
    # Fast path
    if canon_target_name.lower() in s.lower():
        return True
    # Word-boundary tolerant match for names with punctuation/extra spaces.
    tgt = re.sub(r"\s+", " ", (canon_target_name or "").strip().lower())
    hay = re.sub(r"\s+", " ", s.lower())
    if not tgt:
        return False
    return re.search(rf"\b{re.escape(tgt)}\b", hay) is not None

def _fmt_hm(mins: int) -> str:
    h, m = divmod(max(0, int(mins)), 60)
    return f"{h}h" if m == 0 else f"{h}h {m}m"

# ───────────────────────── Day detection ─────────────────────────
_WEEKDAYS = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"]
_ABBR = {"sun":"sunday","mon":"monday","tue":"tuesday","tues":"tuesday","wed":"wednesday","thu":"thursday","thur":"thursday","thurs":"thursday","fri":"friday","sat":"saturday"}
_DAY_TOKEN_RE = re.compile(r"^\s*([A-Za-z]{3,9})(?:\s*,|\s+|$)", re.I)

_USER_DAY_ALIASES = {
    "mon": "monday", "monday": "monday",
    "tue": "tuesday", "tues": "tuesday", "tuesday": "tuesday",
    "wed": "wednesday", "weds": "wednesday", "wednesday": "wednesday",
    "thu": "thursday", "thur": "thursday", "thurs": "thursday", "thursday": "thursday",
    "fri": "friday", "friday": "friday",
    "sat": "saturday", "saturday": "saturday",
    "sun": "sunday", "sunday": "sunday",
}

def _canon_input_day(user_day: Optional[str]) -> Optional[str]:
    if not user_day:
        return None
    token = re.sub(r"[^a-z]", "", (user_day or "").strip().lower())
    if not token:
        return None
    if token in _USER_DAY_ALIASES:
        return _USER_DAY_ALIASES[token]
    if len(token) >= 2:
        for full in _WEEKDAYS:
            if full.startswith(token):
                return full
    return None

def _canon_day_from_header(value: str) -> Optional[str]:
    s = (value or "").strip().lower()
    s = "".join(ch for ch in s if ch.isalpha() or ch.isspace() or ch == ",")
    head = s.split(",")[0].strip()
    mapping = {
        "monday":"monday","tuesday":"tuesday","wednesday":"wednesday",
        "thursday":"thursday","friday":"friday","saturday":"saturday","sunday":"sunday",
    }
    return mapping.get(head)

def _canon_day_loose(cell: str) -> Optional[str]:
    if not cell:
        return None
    s = str(cell).replace("\n", " ").strip()
    d = _canon_day_from_header(s)
    if d:
        return d
    m = _DAY_TOKEN_RE.match(s)
    if not m:
        return None
    token = m.group(1).lower()
    if token in _WEEKDAYS: return token
    if token in _ABBR: return _ABBR[token]
    return None

def _weekday_from_dateish(cell: str) -> Optional[str]:
    s = (cell or "").strip()
    if not s:
        return None
    if _canon_day_loose(s):
        return None
    s = s.replace("\xa0", " ")
    candidates = [s]
    if re.search(r"\d{2}:\d{2}:\d{2}$", s):
        from re import sub
        candidates.append(sub(r"\s*\d{2}:\d{2}:\d{2}$", "", s).strip())
    for t in candidates:
        try:
            from dateutil import parser as dateparser
            dt = dateparser.parse(t, fuzzy=True, default=datetime(2000,1,1))
            if dt:
                return dt.strftime("%A").lower()
        except Exception:
            pass
    return None

def _first_k_nonempty_rows(grid: List[List[str]], k: int = 3) -> List[Tuple[int, List[str]]]:
    out = []
    for r, row in enumerate(grid):
        if any(((row[c] if c < len(row) else "") or "").strip() for c in range(len(row))):
            out.append((r, [((row[c] if c < len(row) else "") or "").strip() for c in range(len(row))]))
        if len(out) >= k:
            break
    return out

# ───────────────────────── Always scan top row ─────────────────────────
def _day_cols_from_first_row(grid: List[List[str]], dbg: Optional[Callable[[str], None]] = None) -> Dict[str, int]:
    cols: Dict[str, int] = {}
    if not grid:
        return cols
    row0 = grid[0]
    for c, raw in enumerate(row0):
        cell = (raw or "").replace("\xa0", " ").strip()
        if not cell:
            continue
        low = cell.lower()
        if re.fullmatch(r"\d+(\.\d+)?", low) or re.search(r"\b(am|pm)\b", low):
            continue
        d = _canon_day_loose(cell) or _weekday_from_dateish(cell)
        if d and d not in cols:
            cols[d] = c
    if dbg:
        dbg(f"🧩 [row0 header] _day_cols_from_first_row → {cols} | head={row0[:10]}")
    return cols

def _find_day_col_anywhere(grid: List[List[str]], target_day: str, scan_rows: int = 60, scan_cols: int = 60) -> Optional[int]:
    if not grid:
        return None
    R = min(scan_rows, len(grid))
    C = min(scan_cols, max((len(r) for r in grid[:scan_rows]), default=0))
    first_hits: Dict[str, int] = {}
    for r in range(R):
        row = grid[r]
        for c in range(C):
            val = (row[c] if c < len(row) else "") or ""
            d = _canon_day_loose(val) or _weekday_from_dateish(val)
            if d and d not in first_hits:
                first_hits[d] = c
    if target_day in first_hits:
        return first_hits[target_day]
    if len(first_hits) == 1:
        return list(first_hits.values())[0]
    return None

def _find_day_col_fuzzy(grid: List[List[str]], target_day: str, scan_rows: int = 40, scan_cols: int = 60, dbg: Optional[Callable[[str], None]] = None) -> Optional[int]:
    if not grid:
        return None
    target_tokens = {target_day}
    for k, v in _ABBR.items():
        if v == target_day:
            target_tokens.add(k)
    R = min(scan_rows, len(grid))
    C = max((len(r) for r in grid[:R]), default=0)
    C = min(C, scan_cols)
    col_counts: Dict[int, int] = {}
    for r in range(R):
        row = grid[r]
        for c in range(C):
            cell = (row[c] if c < len(row) else "") or ""
            low = str(cell).replace("\n", " ").lower()
            for tok in target_tokens:
                if re.search(rf"\b{re.escape(tok)}\b", low):
                    col_counts[c] = col_counts.get(c, 0) + 1
                    break
    if not col_counts:
        return None
    best = max(col_counts.items(), key=lambda kv: kv[1])[0]
    if dbg:
        dbg(f"🧪 _find_day_col_fuzzy hits={col_counts} → choose col {best}")
    return best

# ───────────────────────── Grid helpers ─────────────────────────
def _header_day_cols(grid: List[List[str]], dbg: Optional[Callable[[str], None]] = None) -> Dict[str, int]:
    if not grid:
        return {}
    best: Dict[str, int] = {}
    limit = min(25, len(grid))
    for r in range(limit):
        row = grid[r]
        day_cols: Dict[str, int] = {}
        for c, val in enumerate(row):
            d = _canon_day_loose(val)
            if d and d not in day_cols:
                day_cols[d] = c
        if day_cols and dbg:
            dbg(f"🔎 _header_day_cols row {r+1}: {day_cols}")
        if len(day_cols) >= 2:
            return day_cols
        if day_cols and not best:
            best = day_cols
    return best

def _time_row_indices(grid: List[List[str]]) -> List[int]:
    out: List[int] = []
    for r, row in enumerate(grid):
        c0 = (row[0] if len(row) >= 1 else "") or ""
        if _TIME_CELL_RE.match(c0) and _parse_time_cell(c0):
            out.append(r)
    return out

def _slot_bands_by_time(grid: List[List[str]]) -> Dict[str, Tuple[int, int]]:
    bands: Dict[str, Tuple[int, int]] = {}
    rows = _time_row_indices(grid)
    if not rows:
        return bands
    rows = rows + [len(grid)]
    for i in range(len(rows) - 1):
        r0, r1 = rows[i], rows[i + 1]
        start_label = (grid[r0][0] if len(grid[r0]) >= 1 else "") or ""
        dt = _parse_time_cell(start_label)
        if dt:
            label = dt.strftime("%I:%M %p").lstrip("0")
            bands[label] = (r0, r1)
    return bands

# ───────────────────────── On-Call inference from blocks ─────────────────────────
def _infer_day_cols_by_blocks(grid: List[List[str]], dbg: Optional[Callable[[str], None]] = None) -> Dict[str, int]:
    day_cols: Dict[str, int] = {}
    if not grid:
        return day_cols
    R = len(grid)
    C = max((len(r) for r in grid), default=0)
    first_range_row_for_col: Dict[int, int] = {}
    for r in range(R):
        row = grid[r]
        for c in range(C):
            cell = (row[c] if c < len(row) else "") or ""
            if _RANGE_RE.match(cell or ""):
                if c not in first_range_row_for_col:
                    first_range_row_for_col[c] = r
    for c, r0 in first_range_row_for_col.items():
        scan_top = max(0, r0 - 8)
        header_day: Optional[str] = None
        for r in range(r0, scan_top - 1, -1):
            val = (grid[r][c] if c < len(grid[r]) else "") or ""
            d = _canon_day_loose(val) or _weekday_from_dateish(val)
            if d:
                header_day = d
                break
        if header_day and header_day not in day_cols:
            day_cols[header_day] = c
    if dbg:
        dbg(f"🧭 _infer_day_cols_by_blocks → {day_cols}")
    return day_cols

# ───────────────────────── Availability checks ─────────────────────────
def _check_unh_mc_capacity_via_grid(
    ws: gspread.Worksheet,
    day_canon: str,
    start_dt: datetime,
    end_dt: datetime,
    *,
    per_slot_cap: Optional[int],  # None=MC, else UNH cap=2
    debug: bool = True,
    dbg: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, List[str], Dict[str, Dict[str, object]]]:
    grid = _read_grid(ws)
    if not grid:
        return False, ["sheet empty"], {}

    day_cols = _header_day_cols(grid, dbg=dbg)
    if day_canon not in day_cols:
        c_guess = _find_day_col_anywhere(grid, day_canon)
        if c_guess is not None:
            day_cols = dict(day_cols)
            day_cols[day_canon] = c_guess

    if day_canon not in day_cols:
        if debug and dbg:
            dbg("⚠️ Could not map weekday header; header scan (first ~12 rows):")
            _debug_dump_header_scan(grid, dbg)
        return False, [f"Could not read weekday header (day '{day_canon}' missing)."], {}
    c0 = day_cols[day_canon]

    bands = _slot_bands_by_time(grid)
    slots = _range_to_slots(start_dt, end_dt)
    reasons: List[str] = []
    detail: Dict[str, Dict[str, object]] = {}

    if debug and dbg:
        dbg(f"🧭 Day '{day_canon.title()}' → col {c0} (0-based)")
        dbg(f"⌚ Bands discovered: {sorted(list(bands.keys()))[:8]}{' …' if len(bands)>8 else ''}")

    for (sdt, _edt) in slots:
        label = sdt.strftime("%I:%M %p").lstrip("0")
        band = bands.get(label)
        if not band:
            reasons.append(f"{label} — no time-row band in sheet")
            continue

        r0, r1 = band
        lane_rows = list(range(r0 + 1, r1))
        vals = [(grid[rr][c0] if rr < len(grid) and c0 < len(grid[rr]) else "") for rr in lane_rows]

        filled_rows = [i for i, v in enumerate(vals) if not _is_blankish(v)]
        empty_rows  = [i for i, v in enumerate(vals) if _is_blankish(v)]
        detail[label] = {
            "lane_rows": lane_rows,
            "vals": vals,
            "filled_rows": filled_rows,
            "empty_rows": empty_rows,
            "col0": c0
        }

        if debug and dbg:
            dbg(f"⏱️ {label}: rows {[rr+1 for rr in lane_rows]} → {vals} (filled={len(filled_rows)}, empty_lanes={empty_rows})")

        if per_slot_cap is None:
            if not empty_rows:
                reasons.append(f"{label} — no empty cells")
        else:
            if len(filled_rows) >= per_slot_cap:
                reasons.append(f"{label} — at capacity ({len(filled_rows)}/{per_slot_cap})")

    ok = (len(reasons) == 0)
    return ok, reasons, detail

def _find_oncall_block_row_bounds(
    grid: List[List[str]],
    c0: int,
    want_s: str,
    want_e: str,
) -> Optional[Tuple[int, int]]:
    label_rows: List[int] = []
    for r in range(0, len(grid)):
        cell = (grid[r][c0] if len(grid[r]) > c0 else "") or ""
        if _RANGE_RE.match(cell or ""):
            label_rows.append(r)
    if not label_rows:
        return None

    r_label = None
    for r in label_rows:
        cell = (grid[r][c0] if len(grid[r]) > c0 else "") or ""
        m = _RANGE_RE.match(cell or "")
        if not m:
            continue
        s_raw, e_raw = m.group(1), m.group(2)
        s_dt, e_dt = _parse_time_cell(s_raw), _parse_time_cell(e_raw)
        if not (s_dt and e_dt):
            continue
        if s_dt.strftime("%I:%M %p") == want_s and e_dt.strftime("%I:%M %p") == want_e:
            r_label = r
            break
    if r_label is None:
        return None

    r_next = len(grid)
    for r in label_rows:
        if r > r_label:
            r_next = r
            break
    return (r_label, r_next)

def _check_oncall_capacity_via_grid(
    ws: gspread.Worksheet,
    day_canon: str,
    start_dt: datetime,
    end_dt: datetime,
    *,
    debug: bool = True,
    dbg: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, List[str], Dict[str, object]]:
    grid = _read_grid(ws)
    if not grid:
        return False, ["sheet empty"], {}

    if debug and dbg:
        _debug_dump_grid_head(ws.title, grid, dbg)

    first_rows = _first_k_nonempty_rows(grid, 3)
    if dbg:
        for r_idx, row in first_rows:
            dbg(f"🧰 Row {r_idx+1} preview: {row}")

    day_cols = _day_cols_from_first_row(grid, dbg=dbg)

    if day_canon not in day_cols:
        hdr = _header_day_cols(grid, dbg=dbg)
        for k, v in hdr.items():
            day_cols.setdefault(k, v)
        if dbg:
            dbg(f"🧰 merged _header_day_cols → {day_cols}")

    if day_canon not in day_cols:
        inferred = _infer_day_cols_by_blocks(grid, dbg=dbg)
        for k, v in inferred.items():
            day_cols.setdefault(k, v)
        if dbg:
            dbg(f"🧰 merged _infer_day_cols_by_blocks → {day_cols}")

    if day_canon not in day_cols:
        c_guess = _find_day_col_anywhere(grid, day_canon)
        if c_guess is not None:
            day_cols[day_canon] = c_guess
            if dbg: dbg(f"🧰 _find_day_col_anywhere → {day_cols}")
    if day_canon not in day_cols:
        c_guess2 = _find_day_col_fuzzy(grid, day_canon, dbg=dbg)
        if c_guess2 is not None:
            day_cols[day_canon] = c_guess2
            if dbg: dbg(f"🧰 _find_day_col_fuzzy → {day_cols}")

    if day_canon not in day_cols:
        if debug and dbg:
            dbg("⚠️ Could not map weekday header after all strategies; header scan (first ~12 rows):")
            _debug_dump_header_scan(grid, dbg)
            dbg("🔎 Grid head preview (12×14):")
            _debug_dump_grid_head(ws.title, grid, dbg, rows=12, cols=14)
        return False, [f"Could not read weekday header from '{ws.title}' for day '{day_canon.title()}'. Map={day_cols}"], {}

    c0 = day_cols[day_canon]
    if dbg:
        dbg(f"✅ Resolved day '{day_canon.title()}' to column {c0} (0-based)")

    want_s = start_dt.strftime("%I:%M %p")
    want_e = end_dt.strftime("%I:%M %p")
    bounds = _find_oncall_block_row_bounds(grid, c0, want_s, want_e)
    if not bounds:
        if debug and dbg:
            _debug_dump_oncall_blocks_for_col(grid, c0, dbg)
        return False, [f"On-Call supports fixed blocks; '{want_s} – {want_e}' not found in col {c0}."], {}
    r_label, r_next = bounds

    lane_rows = list(range(r_label + 1, r_next))
    vals = [(grid[rr][c0] if rr < len(grid) and c0 < len(grid[rr]) else "") for rr in lane_rows]
    empty_any = any(_is_blankish(v) for v in vals)

    if debug and dbg:
        dbg(f"📚 On-Call '{want_s} – {want_e}' rows {[rr+1 for rr in lane_rows]} → {vals}")

    reasons = [] if empty_any else [f"On-Call block '{want_s} – {want_e}' has no empty cells"]
    return empty_any, reasons, {"lane_rows": lane_rows, "vals": vals, "col0": c0}

# ─────────── Debug printers ───────────
def _debug_dump_grid_head(title: str, grid: List[List[str]], dbg: Callable[[str], None], rows: int = 8, cols: int = 8):
    R = min(rows, len(grid))
    C = min(cols, max((len(r) for r in grid[:rows]), default=0))
    dbg(f"📄 Grid preview ({title}) {R}×{C}:")
    for r in range(R):
        row = [ (grid[r][c] if c < len(grid[r]) else "") for c in range(C) ]
        dbg(f"  r{r+1:02}: {row}")

def _debug_dump_header_scan(grid: List[List[str]], dbg: Callable[[str], None], rows: int = 12, cols: int = 10):
    R = min(rows, len(grid))
    C = min(cols, max((len(r) for r in grid[:rows]), default=0))
    for r in range(R):
        hits = []
        for c in range(C):
            v = (grid[r][c] if c < len(grid[r]) else "") or ""
            d = _canon_day_loose(v) or _weekday_from_dateish(v)
            if d:
                hits.append((c, v))
        if hits:
            dbg(f"  r{r+1:02} day-like cells: {hits}")

def _debug_dump_oncall_blocks_for_col(grid: List[List[str]], c0: int, dbg: Callable[[str], None]):
    blocks = []
    for r in range(len(grid)):
        v = (grid[r][c0] if c0 < len(grid[r]) else "") or ""
        if _RANGE_RE.match(v or ""):
            blocks.append((r+1, v))
    if blocks:
        dbg(f"📚 Blocks found in col {c0}: {blocks}")
    else:
        dbg(f"📚 No block labels found in col {c0}.")

# ───────────────────────── On-Call tab resolver ─────────────────────────
_ONCALL_TITLE_RE = re.compile(r"\bon\s*[- ]?\s*call\b", re.I)

def _iter_oncall_titles_cached(ss) -> List[str]:
    titles = _cached_ws_titles(getattr(ss, "id", "")) or []
    return [t for t in titles if _ONCALL_TITLE_RE.search(t or "")]

def _predict_oncall_title_for_date(d: ddate) -> str:
    weekday = d.weekday()  # Monday=0..Sunday=6
    days_since_sunday = (weekday + 1) % 7
    sunday = d - timedelta(days=days_since_sunday)
    saturday = sunday + timedelta(days=6)
    return f"On Call {sunday.month}/{sunday.day}-{saturday.month}/{saturday.day}"

def _find_working_oncall_ws(
    ss,
    preferred_title: str,
    day_canon: str,
    start_dt: datetime,
    end_dt: datetime,
    dbg: Optional[Callable[[str], None]] = None,
) -> Optional[gspread.Worksheet]:
    want_title = _predict_oncall_title_for_date(start_dt.date())
    ordered_titles: List[str] = []

    ordered_titles.append(want_title)
    if preferred_title and preferred_title not in ordered_titles:
        ordered_titles.append(preferred_title)

    cached = _iter_oncall_titles_cached(ss)
    for t in cached:
        if t not in ordered_titles:
            ordered_titles.append(t)

    if not cached:
        try:
            all_titles = [w.title for w in ss.worksheets() if _ONCALL_TITLE_RE.search(w.title)]
            for t in all_titles:
                if t not in ordered_titles:
                    ordered_titles.append(t)
        except Exception:
            pass

    if dbg:
        dbg(f"🗂️ On-Call tabs to try (in order): {ordered_titles}")

    for title in ordered_titles:
        try:
            ws = ss.worksheet(title)
        except Exception:
            continue
        if dbg: dbg(f"🔎 Checking On-Call tab: {title}")
        ok, reasons, _ = _check_oncall_capacity_via_grid(ws, day_canon, start_dt, end_dt, debug=False)
        if ok:
            if dbg: dbg(f"✅ Using On-Call tab: {title}")
            return ws
        else:
            if dbg: dbg(f"↪️ Not suitable: {title} — {' | '.join(reasons)}")

    return None

# ───────────────────────── Main entry ─────────────────────────
def handle_add(
    st, ss, schedule, *,
    actor_name: str, canon_target_name: str,
    campus_title: Optional[str],
    day: str,
    start, end,
) -> str:
    """
    Booking rules:
      • MC: each 30-min slot must have ≥1 empty lane.
      • UNH: per-slot capacity = 2.
      • On-Call: exact block match (e.g., 7–11) with ≥1 empty lane.
    """
    debug_log: List[str] = []

    def dbg(msg: str):
        debug_log.append(str(msg))

    def fail(msg: str):
        # Include debug log only when enabled.
        log = "\n".join(debug_log[-800:])
        show_debug = bool(st.session_state.get("debug_add", False))
        if show_debug and log:
            raise ValueError(
                f"{msg}\n\n--- DEBUG ---------------------------------\n{log}\n-------------------------------------------"
            )
        raise ValueError(msg)

    def fail_user(msg: str):
        raise ValueError(msg)

    # Resolve campus/tab
    sidebar_tab = st.session_state.get("active_sheet")
    sheet_title, campus_kind = _resolve_campus_title(ss, campus_title, sidebar_tab)
    dbg(f"📌 Sheet resolved: '{sheet_title}'  kind={campus_kind}")

    # Times + caps
    start_dt = _ensure_dt(start)
    end_dt   = _ensure_dt(end, ref_date=start_dt.date())

    # Allow overnight windows like "7pm-12am" or "10pm-2am" by rolling end into next day
    if not (_is_half_hour_boundary_dt(start_dt) and _is_half_hour_boundary_dt(end_dt)):
        fail("Times must be on 30-minute boundaries (:00 or :30).")

    if end_dt <= start_dt:
        # If the end looks like after-midnight (00:00–05:59), treat it as same-night continuation
        if 0 <= end_dt.time().hour <= 5:
            end_dt = end_dt.replace(day=end_dt.day) + timedelta(days=1)
            dbg("⏩ Interpreting an after-midnight end time (e.g., 12:00 AM) as same-night; rolled end to next day.")
        else:
            fail("End time must be after start time.")

    req_slots = _range_to_slots(start_dt, end_dt)
    req_minutes = 30 * len(req_slots)

    dbg(f"🕒 Request window {fmt_time(start_dt)}–{fmt_time(end_dt)}  ({req_minutes/60:.1f}h)")

    # Canonical day
    day_canon = _canon_input_day(day)
    if not day_canon:
        fail(f"Couldn't understand the day '{day}'.")
    dbg(f"🧭 Day raw={repr(day)} canon={day_canon!r}")

    # ───────────────────────── Summer block schedule ─────────────────────────
    if getattr(config, "SUMMER_MODE", False):
        start_label = summer_schedule._clean_time_label(fmt_time(start_dt))
        end_label = summer_schedule._clean_time_label(fmt_time(end_dt))

        valid_windows = {
            (
                summer_schedule._clean_time_label(a),
                summer_schedule._clean_time_label(b),
            )
            for a, b in getattr(config, "SUMMER_SHIFT_WINDOWS", [])
        }
        if (start_label, end_label) not in valid_windows:
            fail(
                "Summer shifts must be either "
                "7:00 AM–3:30 PM or 3:30 PM–12:00 AM."
            )

        target_date = start_dt.date()
        if target_date.year < 2000:
            try:
                target_date = _date_for_weekday_in_sheet(ss, sheet_title, day_canon)
            except Exception:
                target_date = None

        if target_date is None:
            try:
                target_date = _date_for_weekday_in_current_la_week(day_canon)
            except Exception:
                target_date = None

        if target_date is None:
            fail(
                "Could not figure out the actual date for this summer shift. "
                "The summer sheet needs date-based scheduling, not just weekday names."
            )

        try:
            existing = summer_schedule.list_person_shifts(ss, canon_target_name)
            shifts = existing.get(canon_target_name, [])
            week_start = target_date - timedelta(days=(target_date.weekday() + 1) % 7)
            week_end = week_start + timedelta(days=6)
            shifts_this_week = [
                item for item in shifts
                if week_start <= item[0] <= week_end
            ]
        except Exception:
            shifts_this_week = []

        max_shifts = getattr(config, "SUMMER_MAX_SHIFTS_PER_WEEK", 5)
        if len(shifts_this_week) >= max_shifts:
            fail(
                f"{canon_target_name} already has "
                f"{len(shifts_this_week)}/{max_shifts} shifts that week."
            )

        paid_mins = int(getattr(config, "SUMMER_PAID_SHIFT_MINS", 8 * 60))
        weekly_cap_mins = int(getattr(config, "SUMMER_WEEKLY_CAP_MINS", 40 * 60))
        week_mins = len(shifts_this_week) * paid_mins
        if week_mins + paid_mins > weekly_cap_mins:
            fail(
                f"Would exceed {weekly_cap_mins // 60}h weekly cap: "
                f"currently {week_mins / 60:.1f}h plus one more shift."
            )

        capacity = getattr(config, "SUMMER_SHIFT_CAPACITY", 9)

        result = summer_schedule.add_person_to_shift(
            ss=ss,
            target_date=target_date,
            start_label=start_label,
            end_label=end_label,
            person_name=canon_target_name,
            role_prefix="OA",
            capacity=capacity,
        )

        return result

    # 20h weekly cap (pre)
    # Fast path: use cached hour counter. This avoids a full-grid strict scan on
    # every add (which was the main source of UI lag). If you're extremely close
    # to the 20h cap, we fall back to the strict scan.
    # Cache key for hours should change when the underlying sheets change.
    # The UI supplies a version-keyed epoch via _LAST_HOURS.
    epoch = st.session_state.get("_LAST_HOURS", {}).get("epoch") or st.session_state.get(
        "UI_EPOCH", st.session_state.get("HOURS_EPOCH", 0)
    )
    try:
        week_hours_now = float(compute_hours_fast(ss, schedule, canon_target_name, epoch=epoch))
    except Exception:
        week_hours_now = total_hours_from_unh_mc_and_neighbor(ss, schedule, canon_target_name)
    if week_hours_now >= 18.0:
        # Near the limit: do a strict refresh to reduce risk from out-of-band edits.
        week_hours_now = total_hours_from_unh_mc_and_neighbor(ss, schedule, canon_target_name)
    dbg(f"📈 Weekly hours before: {week_hours_now:.1f}h")
    if week_hours_now + (req_minutes / 60.0) > 20.0:
        fail(f"More than 20 hours: have {week_hours_now:.1f}h; request {req_minutes/60:.1f}h.")

    # ---- IMPORTANT: Only call schedule._get_sheet for UNH/MC. For ON-CALL we must not. ----
    ws0: gspread.Worksheet
    if campus_kind == "ONCALL":
        # Directly open the tab by title; do NOT build SheetInfo here (it expects weekday headers).
        try:
            ws0 = ss.worksheet(sheet_title)
        except Exception as e:
            fail(f"Could not open worksheet '{sheet_title}': {e}")
    else:
        # UNH/MC path (uses ladder + day headers)
        info = schedule._get_sheet(sheet_title)  # safe for UNH/MC
        ws0 = getattr(info, 'ws', info)

    # If ONCALL but request is small, optionally re-route to UNH (kept from your original behavior)
    if campus_kind == "ONCALL" and (end_dt - start_dt) < timedelta(hours=3):
        titles = _cached_ws_titles(getattr(ss, "id", "")) or []
        for t in titles:
            tl = t.lower()
            if ("unh" in tl) or ("hall" in tl):
                sheet_title, campus_kind = t, "UNH"
                # refresh ws0 correctly for UNH
                info = schedule._get_sheet(sheet_title)
                ws0 = getattr(info, 'ws', info)
                dbg(f"🔁 Auto-route to {sheet_title} (UNH) for sub-3h request.")
                break

    # Per-day minutes cap (8h)
    sched = get_user_schedule(ss, schedule, canon_target_name) or {}

    # Prevent double-booking across UNH/MC (and duplicate entries within the same campus).
    if campus_kind in ("UNH", "MC"):
        base_date = start_dt.date()
        req_s, req_e = start_dt, end_dt

        def _iter_se(seq):
            for block in (seq or []):
                s = e = None
                if isinstance(block, (list, tuple)) and len(block) >= 2:
                    s, e = block[0], block[1]
                elif isinstance(block, str):
                    mm = _RANGE_RE.match(block.strip())
                    if mm:
                        s, e = mm.group(1), mm.group(2)
                if not (s and e):
                    continue

                # Schedule data is 12h strings; accept a couple of common variants.
                def _p(x):
                    for fmt in ("%I:%M %p", "%I %p"):
                        try:
                            return datetime.strptime(str(x).strip(), fmt)
                        except Exception:
                            pass
                    return None

                sd0 = _p(s)
                ed0 = _p(e)
                if not (sd0 and ed0):
                    continue

                sd = datetime.combine(base_date, sd0.time())
                ed = datetime.combine(base_date, ed0.time())
                if ed <= sd:
                    ed += timedelta(days=1)
                yield sd, ed

        buckets_today = (sched.get(day_canon, {}) or {})
        for src in ("UNH", "MC"):
            for sd, ed in _iter_se(buckets_today.get(src, []) or []):
                if max(req_s, sd) < min(req_e, ed):
                    if src == campus_kind:
                        fail_user(
                            f"Duplicate entry: {canon_target_name} is already scheduled in this time window "
                            f"({day_canon.title()} {fmt_time(sd)}–{fmt_time(ed)})."
                        )
                    else:
                        fail_user(
                            f"Schedule conflict: {canon_target_name} is already scheduled in {src} "
                            f"({day_canon.title()} {fmt_time(sd)}–{fmt_time(ed)}). "
                            f"They can’t be scheduled in {campus_kind} at the same time."
                        )

    minutes_today = 0

    # Be defensive: schedule parsers sometimes return ranges as
    #   - (start,end)
    #   - (start,end,...)  (extra metadata)
    #   - a single string like "7:00 AM – 9:00 AM"
    # We normalize everything into (start,end) to avoid unpack errors.
    for _, ranges in (sched.get(day_canon, {}) or {}).items():
        for block in (ranges or []):
            s = e = None
            if isinstance(block, (list, tuple)):
                if len(block) >= 2:
                    s, e = block[0], block[1]
            elif isinstance(block, str):
                mm = _RANGE_RE.match(block.strip())
                if mm:
                    s, e = mm.group(1), mm.group(2)

            if not (s and e):
                continue

            try:
                sd = datetime.strptime(str(s).strip(), "%I:%M %p")
                ed = datetime.strptime(str(e).strip(), "%I:%M %p")
            except Exception:
                continue

            if ed <= sd:
                ed += timedelta(days=1)
            minutes_today += int((ed - sd).total_seconds() // 60)
    dbg(f"🧮 Minutes on {day_canon.title()} before: {minutes_today} min")
    if (minutes_today + req_minutes) > 8 * 60:
        fail(f"Daily cap exceeded on {day_canon.title()}: have {_fmt_hm(minutes_today)}, request {_fmt_hm(req_minutes)}.")

    # ───────────────────────── On-Call ─────────────────────────
    if campus_kind == "ONCALL":
        # Find a working On-Call sheet (same week) and deeply debug header mapping
        ws = _find_working_oncall_ws(ss, ws0.title, day_canon, start_dt, end_dt, dbg=dbg)
        if not ws:
            fail("Could not find an On-Call tab that contains this weekday/time block (or no empty lanes).")

        ok, reasons, _ = _check_oncall_capacity_via_grid(ws, day_canon, start_dt, end_dt, debug=True, dbg=dbg)
        if not ok:
            fail(f"Using '{ws.title}': {' | '.join(reasons)}")

        # Lock
        locks_ws = get_or_create_locks_sheet(ss)
        k = lock_key(ws.title, day_canon, fmt_time(start_dt), fmt_time(end_dt))
        won, _ = acquire_fcfs_lock(locks_ws, k, actor_name, ttl_sec=90)
        if not won:
            fail("Another request just claimed this window. Try again.")

        # Resolve the column for this weekday (with rich debug)
        grid = _read_grid(ws)
        dbg(f"🧩 Top row (first 12 cols): { (grid[0][:12] if grid and grid[0] else []) }")
        day_cols = _day_cols_from_first_row(grid, dbg=dbg)
        if day_canon not in day_cols:
            hdr = _header_day_cols(grid, dbg=dbg)
            for k2, v2 in hdr.items():
                day_cols.setdefault(k2, v2)
            dbg(f"🧰 merged _header_day_cols → {day_cols}")

        if day_canon not in day_cols:
            inferred = _infer_day_cols_by_blocks(grid, dbg=dbg)
            for k2, v2 in inferred.items():
                day_cols.setdefault(k2, v2)
            dbg(f"🧰 merged _infer_day_cols_by_blocks → {day_cols}")

        if day_canon not in day_cols:
            c_guess = _find_day_col_anywhere(grid, day_canon)
            if c_guess is not None:
                day_cols[day_canon] = c_guess
                dbg(f"🧰 _find_day_col_anywhere → {day_cols}")
        if day_canon not in day_cols:
            c_guess2 = _find_day_col_fuzzy(grid, day_canon, dbg=dbg)
            if c_guess2 is not None:
                day_cols[day_canon] = c_guess2
                dbg(f"🧰 _find_day_col_fuzzy → {day_cols}")

        if day_canon not in day_cols:
            # Dump a small grid preview into the error so we can see what's actually in that header row
            _debug_dump_header_scan(grid, dbg)
            _debug_dump_grid_head(ws.title, grid, dbg, rows=12, cols=14)
            fail(f"Could not read weekday header from '{ws.title}' for day '{day_canon.title()}'. Map={day_cols}")

        c0 = day_cols[day_canon]
        c1 = c0   # in your sheet, names live under the SAME column as the time-range label
        dbg(f"✅ Day '{day_canon.title()}' resolved to column {c0} (0-based)")

        # Find the exact On-Call block (e.g., "7:00 AM – 11:00 AM") and write into first empty lane
        want_s = start_dt.strftime("%I:%M %p"); want_e = end_dt.strftime("%I:%M %p")
        bounds = _find_oncall_block_row_bounds(grid, c0, want_s, want_e)
        if not bounds:
            _debug_dump_oncall_blocks_for_col(grid, c0, dbg)
            fail(f"On-Call supports fixed blocks; '{want_s} – {want_e}' not found in col {c0}.")
        r_label, r_next = bounds
        lane_rows = list(range(r_label + 1, r_next))
        if not lane_rows:
            fail("On-Call block has no lane rows defined.")

        # Prevent duplicate entries within the same block.
        for rr in lane_rows:
            v = grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else ""
            if _cell_has_name(v, canon_target_name):
                fail(
                    f"Duplicate entry: **{canon_target_name}** is already scheduled "
                    f"for this On-Call block ({fmt_time(start_dt)}–{fmt_time(end_dt)})."
                )

        wrote = False
        for rr in lane_rows:
            v = grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else ""
            if _is_blankish(v):
                # Single-cell write, but use batch_update so it shares the same invalidation path
                # as multi-cell writes (and is friendlier to quotas).
                import gspread.utils as a1
                ref = a1.rowcol_to_a1(rr + 1, c1 + 1)
                ws.batch_update([{ "range": f"{ref}:{ref}", "values": [[f"OA: {canon_target_name}"]] }])
                bump_ws_version(ws)
                # Keep our in-memory grid consistent so we can seed caches.
                if rr < len(grid):
                    if c0 >= len(grid[rr]):
                        grid[rr] = list(grid[rr]) + [""] * (c0 + 1 - len(grid[rr]))
                    grid[rr][c0] = f"OA: {canon_target_name}"

                # Seed the read cache for the new worksheet version so the
                # immediate rerun doesn't refetch a huge grid.
                try:
                    from ..config import ONCALL_MAX_COLS, ONCALL_MAX_ROWS, ROSTER_SHEET, AUDIT_SHEET, LOCKS_SHEET
                    end_col_letter = a1.rowcol_to_a1(1, ONCALL_MAX_COLS).split("1")[0]
                    full_range = f"A1:{end_col_letter}{ONCALL_MAX_ROWS}"
                    seed_batch_get_cache(ws, [full_range], [grid])
                except Exception:
                    pass
                wrote = True

                dbg(f"📝 Wrote at r{rr+1}, c{c1+1} → 'OA: {canon_target_name}'")
                break
        if not wrote:
            fail("On-Call block filled before write; please retry.")

        target_title = ws.title

    # ───────────────────────── UNH / MC ─────────────────────────
    else:
        per_slot_cap = 2 if campus_kind == "UNH" else None
        ok, reasons, _ = _check_unh_mc_capacity_via_grid(ws0, day_canon, start_dt, end_dt, per_slot_cap=per_slot_cap, debug=True, dbg=dbg)
        if not ok:
            fail(f"Using '{ws0.title}': {' | '.join(reasons)}")

        # Lock
        locks_ws = get_or_create_locks_sheet(ss)
        k = lock_key(ws0.title, day_canon, fmt_time(start_dt), fmt_time(end_dt))
        won, _ = acquire_fcfs_lock(locks_ws, k, actor_name, ttl_sec=90)
        if not won:
            fail("Another request just claimed this window. Try again.")

        # Write per 30-min band
        grid = _read_grid(ws0)
        day_cols = _header_day_cols(grid, dbg=dbg)
        if day_canon not in day_cols:
            c_guess = _find_day_col_anywhere(grid, day_canon)
            if c_guess is not None:
                day_cols = dict(day_cols); day_cols[day_canon] = c_guess
        if day_canon not in day_cols:
            _debug_dump_header_scan(grid, dbg)
            _debug_dump_grid_head(ws0.title, grid, dbg)
            fail(f"Could not read weekday header (day '{day_canon}' missing).")

        # Resolve target weekday column (0-based in our grid)
        c0 = day_cols[day_canon]
        # Convert to 1-based just for gspread calls
        col_1based = c0 + 1

        bands = _slot_bands_by_time(grid)

        # Batch writes across all 30-min bands to minimize API calls.
        pending_writes: list[tuple[int,int,str]] = []  # (row1, col1, val)

        for (sdt, _edt) in req_slots:
            label = sdt.strftime("%I:%M %p").lstrip("0")
            band = bands.get(label)
            if not band:
                fail(f"Slot {label} missing unexpectedly; please retry.")
            r0, r1 = band
            lane_rows = list(range(r0 + 1, r1))
            if not lane_rows:
                fail(f"Slot {label} has no lane rows defined.")

            # Prevent duplicate entries in the same 30-min band.
            for rr in lane_rows:
                v = grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else ""
                if _cell_has_name(v, canon_target_name):
                    fail(
                        f"Duplicate entry: **{canon_target_name}** is already scheduled "
                        f"in this slot ({day_canon.title()} {label})."
                    )

            # Capacity check for UNH
            if per_slot_cap is not None:
                filled = 0
                for rr in lane_rows:
                    v = grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else ""
                    if not _is_blankish(v):
                        filled += 1
                if filled >= per_slot_cap:
                    fail(f"{label} just reached capacity ({filled}/{per_slot_cap}); retry another time.")

            # Write into first empty lane
            wrote = False
            for rr in lane_rows:
                v = grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else ""
                if _is_blankish(v):
                    pending_writes.append((rr + 1, col_1based, f"OA: {canon_target_name}"))
                    wrote = True
                    dbg(f"📝 Wrote at r{rr+1}, c{col_1based} → 'OA: {canon_target_name}'")
                    # Keep local grid in sync for subsequent slots
                    if rr < len(grid):
                        if c0 >= len(grid[rr]):
                            grid[rr] = list(grid[rr]) + [""] * (c0 + 1 - len(grid[rr]))
                        grid[rr][c0] = f"OA: {canon_target_name}"
                    break
            if not wrote:
                fail(f"Slot {label} filled before write; please retry.")

        if pending_writes:
            import gspread.utils as a1
            data = []
            for r1, c1, v in pending_writes:
                ref = a1.rowcol_to_a1(r1, c1)
                data.append({"range": f"{ref}:{ref}", "values": [[v]]})
            ws0.batch_update(data)
            bump_ws_version(ws0)

            # Seed the new-version read cache with our already-updated grid.
            try:
                from ..config import ONCALL_MAX_COLS, ONCALL_MAX_ROWS, ROSTER_SHEET, AUDIT_SHEET, LOCKS_SHEET
                end_col_letter = a1.rowcol_to_a1(1, ONCALL_MAX_COLS).split("1")[0]
                full_range = f"A1:{end_col_letter}{ONCALL_MAX_ROWS}"
                seed_batch_get_cache(ws0, [full_range], [grid])
            except Exception:
                pass


        target_title = ws0.title

    # Success (avoid a full strict rescan here; it made the UI feel "frozen")
    fresh_total = week_hours_now + (req_minutes / 60.0)
    return (
        f"Added **{canon_target_name}** on **{target_title}** "
        f"({day_canon.title()} {fmt_time(start_dt)}–{fmt_time(end_dt)}). "
        f"Now at **{fresh_total:.1f}h / 20h** this week."
    )
