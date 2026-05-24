"""Availability computation + UI rendering.

This module:
- Computes open ranges for UNH/MC (30m bands) and On-Call (block labels)
- Caches availability for fast UI
- Renders the availability expander widgets

It reuses parsing helpers from `oa_app.ui.schedule_query`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import gspread
import streamlit as st

from ..config import AUDIT_SHEET, LOCKS_SHEET, ROSTER_SHEET, APPROVAL_SHEET
from ..core.utils import fmt_time
from ..integrations.gspread_io import with_backoff
from . import schedule_query


# Pull from config (fall back if not present)
try:
    from .. import config as _config

    UNH_MC_CAPACITY_DEFAULT = getattr(_config, "UNH_MC_CAPACITY", 2)
    ONCALL_WEEKDAY_CAPACITY = getattr(_config, "ONCALL_WEEKDAY_CAPACITY", 9)
except Exception:
    UNH_MC_CAPACITY_DEFAULT = 2
    ONCALL_WEEKDAY_CAPACITY = 9

_read_grid = schedule_query._read_grid
_RANGE_RE = schedule_query._RANGE_RE
_TIME_CELL_RE = schedule_query._TIME_CELL_RE
_parse_time_cell = schedule_query._parse_time_cell
_sq_time_rows = getattr(schedule_query, "_time_row_indices", None)
_sq_daycanon = getattr(schedule_query, "_canon_day_from_header", None)


def campus_kind(title: str) -> str:
    tl = (title or "").lower()
    if re.search(r"call", tl):
        return "ONCALL"
    if re.search(r"\bmc\b|main", tl):
        return "MC"
    return "UNH"


def weekday_filter(days_list: List[str], tab_title: str) -> List[str]:
    try:
        from .. import config as _config
        if getattr(_config,"SUMMER_MODE", False):
            return getattr(_config, "SUMMER_DAYS", days_list)
    except Exception:
        pass
        
    return (
        ["monday", "tuesday", "wednesday", "thursday", "friday"]
        if campus_kind(tab_title) in ("UNH", "MC")
        else days_list
    )


DAY_ALIASES = {
    "mon": "monday",
    "monday": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "tuesday": "tuesday",
    "wed": "wednesday",
    "weds": "wednesday",
    "wednsday": "wednesday",
    "wednesday": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "thursday": "thursday",
    "fri": "friday",
    "friday": "friday",
    "sat": "saturday",
    "saturday": "saturday",
    "sun": "sunday",
    "sunday": "sunday",
}


def norm_day(token: str) -> Optional[str]:
    if not token:
        return None
    t = re.sub(r"[^a-z]", "", str(token).lower())
    return DAY_ALIASES.get(t)


def _is_blankish(v) -> bool:
    if v is None:
        return True
    s = str(v)
    s = s.replace("\u00A0", " ").replace("\u200B", "").strip()
    s = re.sub(r"[‐-‒–—―]+", "-", s)
    if s == "" or s in {"-", "--", "---", ".", "…"}:
        return True
    if re.fullmatch(r"[\s\.\-_/\\|]*", s or ""):
        return True
    # common placeholders
    if s.lower() in {"n/a", "na"}:
        return True
    return False


def _find_day_col_anywhere(grid: List[List], day_canon: str) -> Optional[int]:
    # scan first 10 rows to find a day header
    max_rows = min(10, len(grid))
    for r in range(max_rows):
        row = grid[r] if r < len(grid) else []
        for c, val in enumerate(row):
            d = None
            if callable(_sq_daycanon):
                try:
                    d = _sq_daycanon(val)
                except Exception:
                    d = None
            if d == day_canon:
                return c
            # fallback: raw string search
            s = ("" if val is None else str(val)).lower()
            if day_canon in s:
                return c
    return None


def _find_day_col_fuzzy(grid: List[List], day_canon: str) -> Optional[int]:
    # fuzzy: match first 3 letters
    token = (day_canon or "")[:3].lower()
    if not token:
        return None
    max_rows = min(15, len(grid))
    for r in range(max_rows):
        row = grid[r] if r < len(grid) else []
        for c, val in enumerate(row):
            s = ("" if val is None else str(val)).strip().lower()
            if s.startswith(token):
                return c
    return None


def _resolve_day_col_via_sq(grid: List[List], day_canon: str) -> Optional[int]:
    """Use schedule_query._canon_day_from_header; scan deeper to handle tall headers."""
    if not callable(_sq_daycanon):
        return None
    max_rows = min(10, len(grid))
    for r in range(max_rows):
        row = grid[r] if r < len(grid) else []
        for c, val in enumerate(row):
            if _sq_daycanon(val) == day_canon:
                return c
    return None


def _time_rows_via_sq(grid: List[List]) -> List[int]:
    """Use schedule_query._time_row_indices if available; else derive from col A."""
    if callable(_sq_time_rows):
        trs = _sq_time_rows(grid)
        if trs:
            return trs
    trs: List[int] = []
    for r, row in enumerate(grid):
        col0 = (row[0] if row else "") or ""
        if _TIME_CELL_RE.match(col0) and _parse_time_cell(col0):
            trs.append(r)
    return trs


def _merge_half_hours_to_ranges(labels_30m: List[Tuple[datetime, datetime]]):
    """Merge contiguous 30-minute labels into wider ranges.

    Be defensive: callers may accidentally pass items that are not 2-tuples
    (e.g., (start,end,meta) or dicts). We normalize to (datetime, datetime)
    pairs and ignore anything else.
    """
    pairs: List[Tuple[datetime, datetime]] = []
    for item in (labels_30m or []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            s, e = item[0], item[1]
            if isinstance(s, datetime) and isinstance(e, datetime):
                pairs.append((s, e))

    if not pairs:
        return []

    pairs.sort(key=lambda ab: ab[0])
    merged: List[Tuple[datetime, datetime]] = []
    cs, ce = pairs[0]
    for s, e in pairs[1:]:
        if s == ce:
            ce = e
        elif e <= ce:
            continue
        else:
            merged.append((cs, ce))
            cs, ce = s, e
    merged.append((cs, ce))
    return merged



def _available_ranges_unh_mc(ws: gspread.Worksheet, day_canon: str):
    """Compute available ranges for UNH/MC tabs."""
    grid = _read_grid(ws)
    if not grid:
        return []

    # Day column
    c_day = _resolve_day_col_via_sq(grid, day_canon)
    if c_day is None:
        c_day = _find_day_col_anywhere(grid, day_canon) or _find_day_col_fuzzy(grid, day_canon)
        if c_day is None:
            return []

    # Time rows
    time_rows = _time_rows_via_sq(grid)
    if not time_rows:
        return []
    time_rows.append(len(grid))  # sentinel

    is_mc = bool(re.search(r"\bmc\b|main", (ws.title or "").lower()))
    empties: List[Tuple[datetime, datetime]] = []

    # For MC: dynamically detect real lane rows based on any weekday occupancy
    weekday_cols: List[int] = []
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        cd = _resolve_day_col_via_sq(grid, d)
        if cd is not None:
            weekday_cols.append(cd)

    used_rows = set()
    if is_mc and weekday_cols:
        scan_rows = range(0, min(len(grid), 800))
        for rr in scan_rows:
            row = grid[rr] if rr < len(grid) else []
            for cd in weekday_cols:
                if cd < len(row) and not _is_blankish(row[cd]):
                    used_rows.add(rr)
                    break

    cap = int(UNH_MC_CAPACITY_DEFAULT)

    for i in range(len(time_rows) - 1):
        r0, r1 = time_rows[i], time_rows[i + 1]
        start_label = (grid[r0][0] if len(grid[r0]) >= 1 else "") or ""
        sdt = _parse_time_cell(start_label)
        if not sdt:
            continue
        edt = sdt + timedelta(minutes=30)

        band_rows = list(range(r0 + 1, r1))
        if not band_rows:
            continue

        if is_mc:
            lane_rows = [rr for rr in band_rows if (rr in used_rows)] or band_rows
        else:
            lane_rows = band_rows[: max(1, cap)]

        vals = []
        for rr in lane_rows:
            if 0 <= rr < len(grid) and 0 <= c_day < len(grid[rr]):
                vals.append(grid[rr][c_day])
            else:
                vals.append("")

        if any(_is_blankish(v) for v in vals):
            empties.append((sdt, edt))

    return _merge_half_hours_to_ranges(empties)


def _available_blocks_oncall(ws: gspread.Worksheet, day_canon: str):
    """Compute available blocks for On-Call tabs."""
    grid = _read_grid(ws)
    if not grid:
        return []

    c0 = _resolve_day_col_via_sq(grid, day_canon)
    if c0 is None:
        c0 = _find_day_col_anywhere(grid, day_canon) or _find_day_col_fuzzy(grid, day_canon)
        if c0 is None:
            return []

    is_weekday = day_canon in {"monday", "tuesday", "wednesday", "thursday", "friday"}
    cap = int(ONCALL_WEEKDAY_CAPACITY) if is_weekday else None

    blocks = []
    label_rows = [
        r
        for r in range(len(grid))
        if _RANGE_RE.match(((grid[r][c0] if c0 < len(grid[r]) else "") or ""))
    ]
    for i, rlab in enumerate(label_rows):
        m = _RANGE_RE.match((grid[rlab][c0] if c0 < len(grid[rlab]) else "") or "")
        if not m:
            continue
        s_raw, e_raw = m.group(1), m.group(2)
        sdt, edt = _parse_time_cell(s_raw), _parse_time_cell(e_raw)
        if not (sdt and edt):
            continue
        rnext = (label_rows[i + 1] if i + 1 < len(label_rows) else len(grid))
        lane_rows = list(range(rlab + 1, rnext))
        if cap is not None:
            lane_rows = lane_rows[:cap]
        vals = [
            (grid[rr][c0] if rr < len(grid) and c0 < len(grid[rr]) else "")
            for rr in lane_rows
        ]
        if any(_is_blankish(v) for v in vals):
            blocks.append((sdt, edt))
    return blocks


@st.cache_data(ttl=30, show_spinner=False)
def cached_available_ranges_for_day(ss_id: str, tab_title: str, day_canon: str, epoch: int):
    """Return availability for a specific day as a list of (HH:MM, HH:MM).

    Be defensive: upstream helpers should return iterable of (start_dt, end_dt),
    but older caches / alternate implementations sometimes return:
      - (start,end,meta)
      - dicts like {"start":..., "end":...}
      - already-formatted strings
    We normalize everything and ignore unknown shapes.
    """
    try:
        ss = st.session_state.get("_SS_HANDLE_BY_ID", {}).get(ss_id)
        if not ss:
            return []
        ws = with_backoff(ss.worksheet, tab_title)
        kind = campus_kind(tab_title)
        ranges = (
            _available_blocks_oncall(ws, day_canon)
            if kind == "ONCALL"
            else _available_ranges_unh_mc(ws, day_canon)
        )

        out = []
        for item in (ranges or []):
            s = e = None
            if isinstance(item, dict):
                s = item.get("start") or item.get("s")
                e = item.get("end") or item.get("e")
            elif isinstance(item, (list, tuple)):
                if len(item) >= 2:
                    s, e = item[0], item[1]
            elif isinstance(item, str):
                # If upstream already returned 24h strings, accept them.
                mm = re.match(r"^\s*([0-2]?\d:\d\d)\s*[-–]\s*([0-2]?\d:\d\d)\s*$", item.strip())
                if mm:
                    out.append((mm.group(1), mm.group(2)))
                    continue

            if s is None or e is None:
                continue

            # datetime/time objects
            if hasattr(s, "strftime") and hasattr(e, "strftime"):
                out.append((s.strftime("%H:%M"), e.strftime("%H:%M")))

        return out
    except Exception:
        return []



@st.cache_data(ttl=30, show_spinner=False)
def enumerate_exact_length_windows(merged_ranges_24h, need_minutes: int):
    """Enumerate contiguous windows exactly equal to requested duration (30m step).

    Defensive against input items that are not 2-tuples.
    """
    results = []
    step = 30

    # Normalize to (HH:MM, HH:MM) pairs
    pairs = []
    for item in (merged_ranges_24h or []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            pairs.append((str(item[0]).strip(), str(item[1]).strip()))
        elif isinstance(item, str):
            mm = re.match(r"^\s*([0-2]?\d:\d\d)\s*[-–]\s*([0-2]?\d:\d\d)\s*$", item.strip())
            if mm:
                pairs.append((mm.group(1), mm.group(2)))

    for s24, e24 in pairs:
        try:
            s_base = datetime.strptime(s24, "%H:%M")
            e_base = datetime.strptime(e24, "%H:%M")
        except Exception:
            continue
        span = int((e_base - s_base).total_seconds() // 60)
        if span < need_minutes:
            continue
        start = s_base
        while start + timedelta(minutes=need_minutes) <= e_base:
            end = start + timedelta(minutes=need_minutes)
            results.append((start.strftime("%H:%M"), end.strftime("%H:%M")))
            start += timedelta(minutes=step)

    seen = set()
    uniq = []
    for s, e in sorted(results):
        if (s, e) in seen:
            continue
        seen.add((s, e))
        uniq.append((s, e))
    return uniq



@st.cache_data(ttl=30, show_spinner=False)
def cached_all_day_availability(ss_id: str, tab_title: str, epoch: int):
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    return {d: cached_available_ranges_for_day(ss_id, tab_title, d, epoch) for d in days}


def render_availability_expander(st_mod, ss_id: str, tab_title: str, epoch: int):
    kind = campus_kind(tab_title)
    badge = {"UNH": "unh", "MC": "mc", "ONCALL": "oncall"}[kind]
    pretty = {"UNH": "UNH", "MC": "MC", "ONCALL": "On-Call"}[kind]

    days_order = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    days_pretty = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    avail_map = cached_all_day_availability(ss_id, tab_title, epoch)

    st_mod.markdown(
        """
    <style>
      .avail-wrap{border:1px solid #e8e8e8;border-radius:16px;padding:14px 14px 6px 14px;background:#fafafa}
      .head-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
      .badge{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;color:white;background:#888}
      .badge.unh{background:#2563eb}
      .badge.mc{background:#059669}
      .badge.oncall{background:#f59e0b}
      .grid{display:grid;grid-template-columns:90px 1fr;gap:8px}
      .day{font-weight:600;color:#444;padding-top:6px}
      .chips{display:flex;flex-wrap:wrap;gap:6px}
      .chip{display:inline-block;padding:4px 8px;border-radius:10px;border:1px solid #ddd;background:white;font-size:12px}
      .muted{color:#777}
    </style>
    """,
        unsafe_allow_html=True,
    )

    with st_mod.container():
        st_mod.markdown(
            f"""<div class=\"avail-wrap\">
                    <div class=\"head-row\"><span class=\"badge {badge}\">{pretty}</span></div>
                    <div class=\"grid\">""",
            unsafe_allow_html=True,
        )
        for dcanon, dpretty in zip(days_order, days_pretty):
            slots = avail_map.get(dcanon) or []
            try:
                summer_mode = bool(getattr(_config, "SUMMER_MODE", False))
            except Exception:
                summer_mode = False
                
            if kind in ("UNH", "MC") and dcanon in ("saturday", "sunday"):
                chips_html = '<span class="muted">N/A</span>'
            elif not slots:
                chips_html = '<span class="muted">No slots</span>'
            else:
                def _chip(s24, e24):
                    sdt = datetime.strptime(s24, "%H:%M")
                    edt = datetime.strptime(e24, "%H:%M")
                    return f'<span class="chip">{fmt_time(sdt)}–{fmt_time(edt)}</span>'

                pairs = []
                for item in slots:
                    if item is None:
                        continue
                    if isinstance(item, dict):
                        s = item.get("start") or item.get("s")
                        e = item.get("end") or item.get("e")
                        if s and e:
                            pairs.append((str(s).strip(), str(e).strip()))
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        pairs.append((str(item[0]).strip(), str(item[1]).strip()))
                    elif isinstance(item, str):
                        mm = re.match(r"^\s*([^–-]+?)\s*[-–]\s*([^–-]+?)\s*$", item.strip())
                        if mm:
                            pairs.append((mm.group(1).strip(), mm.group(2).strip()))
                chips_html = "".join(_chip(s, e) for (s, e) in pairs)

            st_mod.markdown(
                f"""<div class=\"day\">{dpretty}</div>
                    <div class=\"chips\">{chips_html}</div>""",
                unsafe_allow_html=True,
            )
        st_mod.markdown("</div></div>", unsafe_allow_html=True)


def _visible_tabs(ss) -> List[str]:
    """Fast, cached visible tab listing.

    We rely on schedule_query._cached_ws_titles (already cached for ~60s) to
    avoid repeated ss.worksheets() calls which can be slow and occasionally
    fail transiently (leading to an empty sidebar).
    """
    deny = {
        AUDIT_SHEET.strip().lower(),
        LOCKS_SHEET.strip().lower(),
        str(ROSTER_SHEET).strip().lower(),
        str(APPROVAL_SHEET).strip().lower(),
    }

    # Optional, user-facing front-matter tabs (e.g., Policies) should not
    # appear in dropdowns where users pick a schedule/job tab.
    try:
        extra = {
            str(t).strip().lower()
            for t in (getattr(_config, "HIDE_SIDEBAR_TABS", None) or [])
            if str(t).strip()
        }
        deny |= extra
    except Exception:
        pass
    titles = schedule_query._cached_ws_titles(getattr(ss, "id", "")) or []
    return [t for t in titles if t.strip().lower() not in deny]


def _latest_tab_of_kind(titles: List[str], kind: str) -> Optional[str]:
    for t in reversed(titles):
        if campus_kind(t) == kind:
            return t
    return None


def render_global_availability(st_mod, ss, epoch: int):
    titles = _visible_tabs(ss)
    tab_unh = _latest_tab_of_kind(titles, "UNH")
    tab_mc = _latest_tab_of_kind(titles, "MC")
    tab_oc = _latest_tab_of_kind(titles, "ONCALL")

    with st_mod.expander("🧭 Available Slots — (UNH • MC • On-Call)", expanded=False):
        c1, c2, c3 = st_mod.columns(3)
        with c1:
            if tab_unh:
                render_availability_expander(st_mod, ss.id, tab_unh, epoch)
            else:
                st_mod.info("No UNH tab visible.")
        with c2:
            if tab_mc:
                render_availability_expander(st_mod, ss.id, tab_mc, epoch)
            else:
                st_mod.info("No MC tab visible.")
        with c3:
            if tab_oc:
                render_availability_expander(st_mod, ss.id, tab_oc, epoch)
            else:
                st_mod.info("No On-Call tab visible.")


@st.cache_data(ttl=300, show_spinner=False)
def list_tabs_for_sidebar(ss_id: str) -> List[str]:
    """Tabs list for the left sidebar.

    This should almost never depend on write invalidations; removing `epoch`
    prevents expensive re-listing after each add/remove.
    """
    ss = st.session_state.get("_SS_HANDLE_BY_ID", {}).get(ss_id)
    if not ss:
        return []
    return _visible_tabs(ss)


def clear_caches() -> None:
    """Clear this module's caches."""
    try:
        cached_available_ranges_for_day.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        cached_all_day_availability.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        enumerate_exact_length_windows.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        list_tabs_for_sidebar.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
