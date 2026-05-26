# oa_app/chat_callout.py
from __future__ import annotations

from datetime import datetime, timedelta, date
from typing import Optional
import re
import gspread
import streamlit as st
from .. import config
from ..core import summer_schedule

from ..core.utils import fmt_time
from .chat_add import (
    _ensure_dt,
    _is_half_hour_boundary_dt,
    _range_to_slots,
    _is_blankish,
    _day_cols_from_first_row,
    _header_day_cols,
    _find_day_col_anywhere,
    _find_day_col_fuzzy,
)
from ..ui.schedule_query import _read_grid, _RANGE_RE, _parse_time_cell, _TIME_CELL_RE
from ..core.quotas import bump_ws_version, seed_batch_get_cache
from ..config import ONCALL_MAX_COLS, ONCALL_MAX_ROWS
from ..core import week_range
from .. import config
from ..core import summer_schedule

# Simple color helpers
_ORANGE = {"red": 1.0, "green": 0.65, "blue": 0.0}
_RED = {"red": 0.95, "green": 0.25, "blue": 0.25}


def _campus_kind(title: str) -> str:
    tl = (title or "").lower()
    if "call" in tl:
        return "ONCALL"
    if "mc" in tl or "main" in tl:
        return "MC"
    return "UNH"


def _find_day_col(grid: list[list[str]], day_canon: str) -> int | None:
    day_cols = _day_cols_from_first_row(grid)
    if day_canon not in day_cols:
        hdr = _header_day_cols(grid)
        for k, v in hdr.items():
            day_cols.setdefault(k, v)
    if day_canon not in day_cols:
        g = _find_day_col_anywhere(grid, day_canon)
        if g is not None:
            day_cols[day_canon] = g
    if day_canon not in day_cols:
        g2 = _find_day_col_fuzzy(grid, day_canon)
        if g2 is not None:
            day_cols[day_canon] = g2
    return day_cols.get(day_canon)


def _cell_has_name_loose(cell: str, canon_name: str) -> bool:
    """Loose match for names inside schedule cells.

    Sheets often store lanes as "OA: Name" / "GOA: Name" and can contain
    non‑breaking spaces or extra whitespace. We treat the OA name as a
    case‑insensitive substring after light normalization.
    """
    if not cell or not canon_name:
        return False
    c = str(cell).replace("\xa0", " ").strip().lower()
    c = re.sub(r"\b(oa|goa)\s*:\s*", "", c, flags=re.I)
    c = re.sub(r"\s+", " ", c)
    t = str(canon_name).replace("\xa0", " ").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t in c


def _find_oncall_day_col(grid: list[list[str]], day_canon: str, ws_title: str) -> int | None:
    """Find the day column for On-Call tabs.

    Some On-Call templates only show dates (e.g., "2/8") rather than weekday
    names. Inferring weekdays from a date *without a year* can be wrong.

    We therefore prefer matching the column by its month/day within the
    worksheet's week range (derived from the tab title).
    """
    try:
        rng = week_range.week_range_from_title(ws_title)
    except Exception:
        rng = None

    # First try the generic day-column logic.
    c_guess = _find_day_col(grid, day_canon)

    if not rng or not grid:
        return c_guess

    ws0, ws1 = rng
    order = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
    if day_canon not in order:
        return c_guess
    try:
        target_d = ws0 + timedelta(days=order.index(day_canon))
    except Exception:
        return c_guess

    mm, dd = target_d.month, target_d.day

    def _mmdd_match(txt: str) -> bool:
        s = (txt or "").replace("\xa0", " ")
        # direct M/D or MM/DD tokens
        m = re.search(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)", s)
        if m:
            try:
                return int(m.group(1)) == mm and int(m.group(2)) == dd
            except Exception:
                return False
        # month name + day
        try:
            from dateutil import parser as dateparser

            dt = dateparser.parse(s, fuzzy=True, default=datetime(target_d.year, 1, 1))
            if dt:
                return dt.month == mm and dt.day == dd
        except Exception:
            pass
        return False

    # Scan the visible header area (first ~6 rows) for the date token.
    R = min(len(grid), 6)
    C = max((len(r) for r in grid[:R]), default=0)
    for c in range(C):
        for r in range(R):
            v = (grid[r][c] if c < len(grid[r]) else "") or ""
            if v and _mmdd_match(str(v)):
                return c

    return c_guess


def _oncall_ref_date(ws_title: str, day_canon: str) -> Optional[date]:
    """Return the calendar date for an On-Call weekday within the worksheet week.

    On-Call block labels only encode *times* (e.g., "7:00 PM - 12:00 AM").
    When we compare ranges we must anchor both the requested window and the
    block label window to the *same* calendar date, otherwise overlap checks
    will fail (e.g., 1900 vs 2026).
    """
    try:
        rng = week_range.week_range_from_title(ws_title)
    except Exception:
        rng = None
    if not rng:
        return None
    ws0, _ws1 = rng
    order = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
    dc = (day_canon or "").strip().lower()
    if dc not in order:
        return None
    try:
        return ws0 + timedelta(days=order.index(dc))
    except Exception:
        return None


def _format_cells(ws: gspread.Worksheet, coords: list[tuple[int, int]], rgb: dict) -> None:
    """Apply background color to multiple cells in one batch_update. coords are 0-based."""
    if not coords:
        return
    requests = []
    for (r0, c0) in coords:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": r0,
                        "endRowIndex": r0 + 1,
                        "startColumnIndex": c0,
                        "endColumnIndex": c0 + 1,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": rgb}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    ws.spreadsheet.batch_update({"requests": requests})


_SHIFT_SWAPS_RE = re.compile(r"^\s*shift\s*swaps?(?:\s*for\s*the\s*week)?\s*$", re.I)


def _find_shift_swaps_col(grid: list[list[str]]) -> tuple[int, int] | None:
    """Return (header_row, col) for the 'Shift Swaps for the week' column."""
    if not grid:
        return None
    R = min(len(grid), 80)
    C = min(max((len(r) for r in grid[:R]), default=0), 80)
    for r in range(R):
        row = grid[r]
        for c in range(C):
            v = (row[c] if c < len(row) else "") or ""
            if v and _SHIFT_SWAPS_RE.match(str(v).strip()):
                return (r, c)
    return None


def _parse_12h_time(s: str) -> datetime:
    """Parse 12h time strings like '7:00 AM', '7 AM', '7:00AM' into a datetime (dummy date)."""
    t = (s or "").strip().upper().replace(".", "")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(\d)(AM|PM)$", r"\1 \2", t)
    t = re.sub(r"(\d:\d\d)(AM|PM)$", r"\1 \2", t)
    if re.fullmatch(r"\d{1,2} (AM|PM)", t):
        t = t.replace(" ", ":00 ")
    return datetime.strptime(t, "%I:%M %p")


def _fmt_swap_timerange(sdt: datetime, edt: datetime) -> str:
    """Format like '1-3:30 pm' (or '11:30 am-12:30 pm')."""
    def _parts(dt: datetime) -> tuple[str, str]:
        hour = dt.strftime("%I").lstrip("0") or "0"
        minute = dt.strftime("%M")
        mm = "" if minute == "00" else f":{minute}"
        mer = dt.strftime("%p").lower()
        return f"{hour}{mm}", mer

    s_txt, s_mer = _parts(sdt)
    e_txt, e_mer = _parts(edt)
    if s_mer == e_mer:
        return f"{s_txt}-{e_txt} {e_mer}"
    return f"{s_txt} {s_mer}-{e_txt} {e_mer}"


def _extract_mmdd_for_day_col(grid: list[list[str]], col: int) -> str | None:
    """Try to extract an mm/dd date from cells near the weekday header column."""
    if not grid:
        return None
    mmdd_re = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")

    # Look near the top of that column for something like "Tue 10/21" or "10/21"
    for r in range(min(25, len(grid))):
        v = (grid[r][col] if col < len(grid[r]) else "") or ""
        m = mmdd_re.search(str(v))
        if m:
            return f"{int(m.group(1))}/{int(m.group(2))}"

    # Fallback: try dateutil parse if installed
    try:
        from dateutil import parser as dateparser
    except Exception:
        return None

    for r in range(min(25, len(grid))):
        v = (grid[r][col] if col < len(grid[r]) else "") or ""
        s = str(v).strip()
        if not s:
            continue
        try:
            dt = dateparser.parse(s, fuzzy=True, default=datetime(2020, 1, 1))
            if dt:
                return f"{dt.month}/{dt.day}"
        except Exception:
            continue

    return None


def handle_callout(
    st, ss, schedule, *,
    canon_target_name: str,
    campus_title: str,
    day: str,
    start, end,
    covered_by: Optional[str] = None,
) -> str:
    """
    Mark a Call-Out:
      • If covered_by is None/empty → color cell RED.
      • If covered_by is provided → color cell ORANGE.

    Note: Swap section logging ("Shift Swaps for the week" / "Future Swaps/Call outs")
    is now rendered idempotently from Supabase via oa_app.jobs.sync_swaps_to_sheets.
    This function only marks the schedule grid cells.
    Works for UNH/MC (half-hour lanes) and On-Call (fixed blocks).
    """
    dbg_log: list[str] = []

    def dbg(x):  # keep for internal use if needed
        dbg_log.append(str(x))

    def fail(msg: str):
        # Keep callout errors clean (no debug spam unless you want to add a flag later)
        raise ValueError(msg)

    kind = _campus_kind(campus_title)
    covered_by = (covered_by or "").strip() or None

    # ───────────────────────── Summer block schedule ─────────────────────────
    if getattr(config, "SUMMER_MODE", False):
        covered_by = (covered_by or "").strip() or None

        sdt = _ensure_dt(start)
        edt = _ensure_dt(end, ref_date=sdt.date())

        if edt <= sdt:
            if 0 <= edt.time().hour <= 5:
                edt = edt + timedelta(days=1)
            else:
                fail("End time must be after start time.")

        start_label = fmt_time(sdt)
        end_label = fmt_time(edt)

        valid_windows = set(getattr(config, "SUMMER_SHIFT_WINDOWS", []))
        if (start_label, end_label) not in valid_windows:
            fail(
                "Summer callouts must be either "
                "7:00 AM–3:30 PM or 3:30 PM–12:00 AM."
            )

        target_date = None

        # Prefer an actual date embedded in the selected schedule block.
        # The UI update below will pass start/end plus date-aware blocks where possible.
        try:
            if hasattr(start, "date"):
                possible = start.date()
                if possible.year == 2026:
                    target_date = possible
        except Exception:
            target_date = None

        # Fallback: infer from selected worksheet/week/day.
        # This is weaker because summer sheets repeat weekdays.
        if target_date is None:
            try:
                from ..ui.page import _date_for_weekday_in_current_la_week
                day_canon = str(day or "").strip().lower()
                target_date = _date_for_weekday_in_current_la_week(day_canon)
            except Exception:
                target_date = None

        if target_date is None:
            fail(
                "Could not figure out the actual summer date for this callout. "
                "Callouts need a real date, not just a weekday."
            )

        return summer_schedule.mark_person_callout(
            ss=ss,
            target_date=target_date,
            start_label=start_label,
            end_label=end_label,
            person_name=canon_target_name,
            covered_by=covered_by,
        )

    try:
        ws = ss.worksheet(campus_title)
    except Exception as e:
        fail(f"Could not open worksheet '{campus_title}': {e}")

    # On-Call labels contain only times, so we must anchor requested start/end
    # to the correct calendar date within the On-Call week (derived from title).
    ref_date = _oncall_ref_date(ws.title, day) if kind == "ONCALL" else None

    sdt = _ensure_dt(start, ref_date=ref_date)
    # If the UI passed a datetime with a different date, coerce it to ref_date.
    if kind == "ONCALL" and ref_date is not None:
        try:
            sdt = sdt.replace(year=ref_date.year, month=ref_date.month, day=ref_date.day)
        except Exception:
            pass

    edt = _ensure_dt(end, ref_date=sdt.date())
    if kind == "ONCALL" and ref_date is not None:
        try:
            edt = edt.replace(year=sdt.year, month=sdt.month, day=sdt.day)
        except Exception:
            pass

    if edt <= sdt:
        if 0 <= edt.time().hour <= 5:
            edt = edt + timedelta(days=1)
        else:
            fail("End time must be after start time.")
    if not (_is_half_hour_boundary_dt(sdt) and _is_half_hour_boundary_dt(edt)):
        fail("Times must be on 30-minute boundaries (:00 or :30).")

    grid = _read_grid(ws)
    if not grid:
        fail("Empty sheet.")

    day_canon = (day or "").strip().lower()
    if kind == "ONCALL":
        c0 = _find_oncall_day_col(grid, day_canon, ws.title)
    else:
        c0 = _find_day_col(grid, day_canon)
    if c0 is None:
        fail(f"Could not map weekday '{day_canon.title()}' to a column in '{ws.title}'.")

    # Find the exact lane cell(s) containing this OA in the requested window
    target_coords: list[tuple[int, int]] = []  # (row_idx_0, col_idx_0)

    if kind == "ONCALL":
        # On-Call tabs vary in layout:
        #   (1) shared block label in column A
        #   (2) per-day block labels in the day columns
        # We support partial windows by coloring any block that overlaps the
        # requested time window.

        def _overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
            return (a0 < b1) and (a1 > b0)

        # Identify label rows: any row that has a time-range label either in col A or this day column.
        label_rows: list[int] = []
        for r in range(len(grid)):
            v0 = (grid[r][0] if len(grid[r]) > 0 else "") or ""
            vd = (grid[r][c0] if c0 < len(grid[r]) else "") or ""
            if _RANGE_RE.match(str(v0).strip()) or _RANGE_RE.match(str(vd).strip()):
                label_rows.append(r)

        if not label_rows:
            fail("Couldn't find On-Call block labels.")

        label_rows.append(len(grid))

        for i in range(len(label_rows) - 1):
            r_label = label_rows[i]
            r_next = label_rows[i + 1]

            shared_v0 = (grid[r_label][0] if len(grid[r_label]) > 0 else "") or ""
            day_v = (grid[r_label][c0] if c0 < len(grid[r_label]) else "") or ""

            # Prefer per-day label if present; otherwise fall back to shared label.
            range_txt = day_v if _RANGE_RE.match(str(day_v).strip()) else shared_v0
            m = _RANGE_RE.match(str(range_txt).strip()) if range_txt else None
            if not m:
                continue

            bs = _parse_time_cell(m.group(1))
            be = _parse_time_cell(m.group(2))
            if not bs or not be:
                continue

            # Anchor On-Call label times to the same calendar date as the
            # requested window so the overlap test is meaningful.
            try:
                anchor = sdt.date()
                bs = datetime.combine(anchor, bs.time())
                be = datetime.combine(anchor, be.time())
            except Exception:
                pass

            if be <= bs:
                be = be + timedelta(days=1)

            if not _overlaps(bs, be, sdt, edt):
                continue

            # Search lane rows within this block for the target name.
            for rr in range(r_label + 1, r_next):
                if rr >= len(grid):
                    continue
                if c0 >= len(grid[rr]):
                    continue
                v = (grid[rr][c0] if c0 < len(grid[rr]) else "") or ""
                if _cell_has_name_loose(v, canon_target_name):
                    target_coords.append((rr, c0))

        if not target_coords:
            fail(f"{canon_target_name} not found in On-Call lanes for the selected window.")

    else:
        # UNH / MC: mark every half-hour slot in the window where this OA appears in a lane.
        time_rows: list[int] = []
        for r, row in enumerate(grid):
            col0 = (row[0] if len(row) >= 1 else "") or ""
            if _TIME_CELL_RE.match(col0) and _parse_time_cell(col0):
                time_rows.append(r)
        time_rows.append(len(grid))

        bands: dict[str, tuple[int, int]] = {}
        for i in range(len(time_rows) - 1):
            r0 = time_rows[i]
            r1 = time_rows[i + 1]
            start_label = (grid[r0][0] if len(grid[r0]) >= 1 else "") or ""
            dt = _parse_time_cell(start_label)
            if dt:
                bands[dt.strftime("%I:%M %p")] = (r0, r1)

        for (seg_s, _) in _range_to_slots(sdt, edt):
            lab = seg_s.strftime("%I:%M %p")
            if lab not in bands:
                continue
            r0, r1 = bands[lab]
            lane_rows = list(range(r0 + 1, r1))
            for rr in lane_rows:
                v = (grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else "") or ""
                if _cell_has_name_loose(v, canon_target_name):
                    target_coords.append((rr, c0))
                    break

        if not target_coords:
            fail(f"{canon_target_name} not found in UNH/MC lanes for the selected window.")

    # 1) Color the cells (no text changes)
    color = _ORANGE if covered_by else _RED
    _format_cells(ws, target_coords, color)

    # 2) Bump sheet version + seed cache so UI is fast on rerun
    try:
        import gspread.utils as a1
        bump_ws_version(ws)
        try:
            end_col_letter = a1.rowcol_to_a1(1, ONCALL_MAX_COLS).split("1")[0]
            full_range = f"A1:{end_col_letter}{ONCALL_MAX_ROWS}"
            seed_batch_get_cache(ws, [full_range], [grid])
        except Exception:
            pass
    except Exception:
        try:
            bump_ws_version(ws)
        except Exception:
            pass

    label = f"{day.title()} {fmt_time(sdt)}–{fmt_time(edt)}"
    if covered_by:
        return (
            f"Call-Out marked for **{canon_target_name}** on **{campus_title}** ({label}) — "
            f"**orange** (covered)."
        )
    return f"Call-Out marked for **{canon_target_name}** on **{campus_title}** ({label}) — **red** (no cover)."
