"""Idempotent rendering of swap/callout sections from Supabase into Google Sheets.

Supabase is the **source of truth** for callouts/pickups. Google Sheets contains:
  1) the schedule grid (colored cells)
  2) two "side" rendered sections:
       - "Shift Swaps for the week"  (current calendar week only)
       - "Future Swaps/Call outs"    (anything after current calendar week)

This job rewrites those two sections **idempotently** (safe to run repeatedly)
and (optionally) applies schedule-grid colors for events that fall in the
**current calendar week in America/Los_Angeles (Sunday → Saturday)**.

Key rules implemented (matches project spec):
  - Current week = LA-local Sunday..Saturday containing "today".
  - Events outside current week are treated as "future" (no grid coloring).
  - Callout uncovered windows are computed by subtracting pickup intervals.
  - Weekly rollover is automatic: rerun moves items between sections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from hashlib import sha1
from typing import Any, Callable

from zoneinfo import ZoneInfo
import re

import gspread

from ..config import ONCALL_MAX_COLS, ONCALL_MAX_ROWS
from ..core.quotas import bump_ws_version
from ..integrations.gspread_io import with_backoff
from ..core.sheets_sections import Section, blanks, compute_section, pad_rows
from ..core.week_range import la_today, week_range_from_title
from ..core import utils
from .. import config


LA = ZoneInfo("America/Los_Angeles")

HDR_WEEKLY = "Shift Swaps for the week"
HDR_FUTURE = "Future Swaps/Call outs"

# Header variants seen across templates.
HDR_WEEKLY_ALIASES = [
    "Shift Swaps for the week",
    "Shift Swaps/Covers",
    "Shift Swaps",
]
HDR_FUTURE_ALIASES = [
    "Future Swaps/Call outs",
    "Future Swaps/Callouts",
    "Future Swaps",
]

# Colors (Sheets API RGB in 0..1)
ORANGE = {"red": 1.0, "green": 0.65, "blue": 0.0}
RED = {"red": 0.95, "green": 0.25, "blue": 0.25}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

# Swap-section column fills (match the workbook template palette)
#   - "Light orange 3" (weekly swaps column)
#   - "Light green 2"  (future swaps/callouts column)
#
# NOTE: In these two columns, we **do not** use the red/orange "coverage" colors.
# Red/orange are reserved for the main schedule grid cells. These swap columns
# must keep their fixed template fills even when rows are cleared/rewritten.
SWAP_WEEKLY_BG = {"red": 252 / 255, "green": 229 / 255, "blue": 205 / 255}  # FCE5CD
SWAP_FUTURE_BG = {"red": 182 / 255, "green": 215 / 255, "blue": 168 / 255}  # B6D7A8

# Metadata columns (far-right) used to store stable id keys for idempotent upserts.
# These are intentionally far beyond the schedule template width (which typically ends near column AA).
KEY_WEEKLY_COL = 52  # AZ
KEY_FUTURE_COL = 53  # BA
SECTION_MAX_ROWS = 200
DAY_ORDER = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

def _ensure_ws_dimensions(ws: gspread.Worksheet, *, min_rows: int, min_cols: int) -> None:
    """Ensure the worksheet has at least (min_rows, min_cols) grid size.

    Some templates are created with only A:Z (26) columns. This sync job stores
    idempotency keys in far-right metadata columns (AZ/BA). If the sheet hasn't
    been expanded yet, writes will fail with a 400 "exceeds grid limits".
    """
    # gspread exposes current grid size via row_count / col_count.
    try:
        cur_rows = int(getattr(ws, "row_count", 0) or 0)
        cur_cols = int(getattr(ws, "col_count", 0) or 0)
    except Exception:
        cur_rows, cur_cols = 0, 0

    # Expand columns first (most common failure).
    if cur_cols and cur_cols < min_cols:
        add = int(min_cols - cur_cols)
        if add > 0:
            with_backoff(ws.add_cols, add)

    # Expand rows if needed.
    if cur_rows and cur_rows < min_rows:
        add = int(min_rows - cur_rows)
        if add > 0:
            with_backoff(ws.add_rows, add)



def _week_bounds_sun_sat(ref: date) -> tuple[date, date]:
    """Return (sunday, saturday) calendar week bounds for LA-local date."""
    # Python weekday(): Mon=0..Sun=6.
    sunday = ref - timedelta(days=(ref.weekday() + 1) % 7)
    saturday = sunday + timedelta(days=6)
    return sunday, saturday


def _parse_dt(x: Any) -> datetime | None:
    if not x:
        return None
    if isinstance(x, datetime):
        return x
    s = str(x)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _to_la(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LA)
    return dt.astimezone(LA)


def _mins(dt: datetime) -> int:
    d = _to_la(dt)
    if d.time() == time(0, 0):
        return 24 * 60
    return d.hour * 60 + d.minute


def _fmt_mmdd(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}"


def _fmt_time_short(dt: datetime) -> str:
    s = utils.fmt_time(_to_la(dt)).replace(":00 ", " ")
    return s


def _fmt_timerange(sdt: datetime, edt: datetime) -> str:
    return f"{_fmt_time_short(sdt)}-{_fmt_time_short(edt)}"


def _pretty_location(campus: str) -> str:
    c = (campus or "").strip().upper()
    if c == "ONCALL":
        return "On Call"
    return c or ""


def _key_hash(s: str) -> str:
    return sha1(s.encode("utf-8")).hexdigest()[:12]


def _infer_visible_row_date(text: str, *, today: date) -> date | None:
    """Best-effort parse of a swap/callout row date from visible text.

    Used to clean legacy rows that predate the current LA-local date, even when
    those rows were created before metadata keys were introduced.
    """
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", str(text or ""))
    if not m:
        return None
    mm = int(m.group(1))
    dd = int(m.group(2))
    candidates: list[date] = []
    for yy in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(yy, mm, dd))
        except Exception:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - today).days))


def _stable_key(parts: list[str]) -> str:
    def n(x: str) -> str:
        return " ".join((x or "").strip().lower().split())

    return "|".join(n(p) for p in parts)


def _subtract_many(interval: tuple[int, int], covers: list[tuple[int, int]]) -> list[tuple[int, int]]:
    a0, b0 = interval
    out = [(a0, b0)]
    for cs, ce in covers:
        if ce <= cs:
            continue
        new_out: list[tuple[int, int]] = []
        for a, b in out:
            if b <= cs or ce <= a:
                new_out.append((a, b))
                continue
            # overlap exists
            if cs <= a and ce >= b:
                continue
            if cs <= a < ce < b:
                new_out.append((ce, b))
                continue
            if a < cs < b <= ce:
                new_out.append((a, cs))
                continue
            if a < cs and ce < b:
                new_out.append((a, cs))
                new_out.append((ce, b))
                continue
        out = new_out
    out.sort()
    return out


@dataclass
class RowOut:
    values: list[str]  # single-cell entry
    color: dict
    sort_key: tuple
    oa_key: str = ""


def _row_text_callout(*, name: str, d: date, tr: str, campus: str) -> str:
    # Required format:
    #   {Name} called out | {MM/DD} | {start}-{end} | {Location} | NO COVER
    return f"{name} called out | {_fmt_mmdd(d)} | {tr} | {_pretty_location(campus)} | NO COVER"


def _row_text_pickup(*, coverer: str, caller: str, d: date, tr: str, campus: str) -> str:
    # Required format:
    #   {CoveringName} covering {CalledOutName} | {MM/DD} | {start}-{end} | {Location}
    return f"{coverer} covering {caller} | {_fmt_mmdd(d)} | {tr} | {_pretty_location(campus)}"


def _rows_from_callout(c: dict, *, pickup_intervals: list[tuple[int, int]]) -> tuple[list[RowOut], list[tuple[int, int]]]:
    """Return (rows, uncovered_segments) for a callout."""
    try:
        d = date.fromisoformat(str(c.get("event_date")))
    except Exception:
        return ([], [])

    sdt = _parse_dt(c.get("shift_start_at"))
    edt = _parse_dt(c.get("shift_end_at"))
    if not (sdt and edt):
        return ([], [])

    caller = str(c.get("caller_name", "")).strip()
    campus = str(c.get("campus", "")).upper()
    approval_id = str(c.get("approval_id") or "").strip()

    base_interval = (_mins(sdt), _mins(edt))
    covers: list[tuple[int, int]] = []
    for a, b in pickup_intervals:
        if b <= base_interval[0] or a >= base_interval[1]:
            continue
        covers.append((max(a, base_interval[0]), min(b, base_interval[1])))
    uncovered = _subtract_many(base_interval, covers)

    if not uncovered:
        return ([], [])

    out: list[RowOut] = []
    for a, b in uncovered:
        # Render each uncovered segment as its own row.
        a_dt = _to_la(sdt).replace(hour=a // 60 % 24, minute=a % 60, second=0, microsecond=0)
        b_dt = _to_la(edt).replace(hour=(0 if b == 24 * 60 else b) // 60 % 24, minute=(0 if b == 24 * 60 else b) % 60, second=0, microsecond=0)
        tr = _fmt_timerange(a_dt, b_dt)
        key_src = approval_id or _stable_key(["CALLOUT", d.isoformat(), campus, caller, str(a), str(b)])
        oa_key = _key_hash(key_src + f"|{a}-{b}")
        txt = _row_text_callout(name=caller, d=d, tr=tr, campus=campus)
        out.append(RowOut(values=[txt], color=RED, sort_key=(d.isoformat(), campus, a, caller), oa_key=oa_key))
    return (out, uncovered)


def _row_from_pickup(p: dict) -> tuple[RowOut | None, tuple[int, int] | None, tuple[date, str] | None]:
    try:
        d = date.fromisoformat(str(p.get("event_date")))
    except Exception:
        return (None, None, None)
    sdt = _parse_dt(p.get("shift_start_at"))
    edt = _parse_dt(p.get("shift_end_at"))
    if not (sdt and edt):
        return (None, None, None)
    target = str(p.get("target_name", "")).strip()
    picker = str(p.get("picker_name", "")).strip()
    campus = str(p.get("campus", "")).upper()
    approval_id = str(p.get("approval_id") or "").strip()
    tr = _fmt_timerange(sdt, edt)
    key_src = approval_id or _stable_key(["PICKUP", d.isoformat(), campus, target, picker, tr])
    oa_key = _key_hash(key_src)
    txt = _row_text_pickup(coverer=picker, caller=target, d=d, tr=tr, campus=campus)
    row = RowOut(values=[txt], color=ORANGE, sort_key=(d.isoformat(), campus, _mins(sdt), target, picker), oa_key=oa_key)
    interval = (_mins(sdt), _mins(edt))
    key = (d, utils.name_key(target))
    return (row, interval, key)


def _format_rows(ws: gspread.Worksheet, *, start_row: int, start_col: int, row_colors: list[dict], num_cols: int) -> None:
    """Apply background color per row across num_cols columns."""
    if not row_colors:
        return
    requests: list[dict] = []
    for i, rgb in enumerate(row_colors):
        r0 = (start_row - 1) + i
        c0 = start_col - 1
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": r0,
                        "endRowIndex": r0 + 1,
                        "startColumnIndex": c0,
                        "endColumnIndex": c0 + num_cols,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": rgb}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    with_backoff(ws.spreadsheet.batch_update, {"requests": requests})


def _clear_format(ws: gspread.Worksheet, section: Section) -> None:
    """Best-effort reset of background color for the section."""
    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": section.start_row - 1,
                    "endRowIndex": section.start_row - 1 + section.max_rows,
                    "startColumnIndex": section.start_col - 1,
                    "endColumnIndex": section.start_col - 1 + section.num_cols,
                },
                "cell": {
                    "userEnteredFormat": {"backgroundColor": WHITE},
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        }
    ]
    with_backoff(ws.spreadsheet.batch_update, {"requests": requests})



def _col_letter(n: int) -> str:
    # 1-based column number -> A1 column letters.
    s = ""
    x = n
    while x > 0:
        x, r = divmod(x - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_a1(title: str, rng: str) -> str:
    # Return a range qualified with the sheet title, safely quoted.
    t = (title or "").replace("'", "''")
    return f"'{t}'!{rng}"


def _values_batch_get(spreadsheet: gspread.Spreadsheet, ranges: list[str]) -> list[list[list[str]]]:
    # Batch-get values for multiple ranges, minimizing read requests.
    if hasattr(spreadsheet, 'values_batch_get'):
        try:
            resp = with_backoff(spreadsheet.values_batch_get, ranges)
        except TypeError:
            resp = with_backoff(spreadsheet.values_batch_get, ranges=ranges)
        vrs = resp.get('valueRanges', []) if isinstance(resp, dict) else []
        out: list[list[list[str]]] = []
        for vr in vrs:
            vals = vr.get('values', []) if isinstance(vr, dict) else []
            out.append(vals or [])
        while len(out) < len(ranges):
            out.append([])
        return out

    # Fallback: do individual reads (more likely to hit quota).
    out2: list[list[list[str]]] = []
    for r in ranges:
        try:
            resp = with_backoff(spreadsheet.values_get, r)  # type: ignore[attr-defined]
            out2.append(resp.get('values', []) or [])
        except Exception:
            out2.append([])
    return out2


def _values_batch_update(spreadsheet: gspread.Spreadsheet, updates: list[dict]) -> None:
    # Batch-update values for many small ranges.
    if not updates:
        return
    body = {'valueInputOption': 'RAW', 'data': updates}
    if hasattr(spreadsheet, 'values_batch_update'):
        with_backoff(spreadsheet.values_batch_update, body)
        return
    if hasattr(spreadsheet, 'batch_update'):
        with_backoff(spreadsheet.batch_update, body)  # type: ignore[arg-type]
        return
    raise RuntimeError('Spreadsheet does not support values_batch_update')


def _format_rows_at(ws: gspread.Worksheet, *, col: int, rows: list[int], colors: list[dict]) -> None:
    # Apply background colors to a single column for specific rows.
    if not rows or not colors:
        return
    requests: list[dict] = []
    for r, rgb in zip(rows, colors):
        r0 = r - 1
        c0 = col - 1
        requests.append(
            {
                'repeatCell': {
                    'range': {
                        'sheetId': ws.id,
                        'startRowIndex': r0,
                        'endRowIndex': r0 + 1,
                        'startColumnIndex': c0,
                        'endColumnIndex': c0 + 1,
                    },
                    'cell': {'userEnteredFormat': {'backgroundColor': rgb}},
                    'fields': 'userEnteredFormat.backgroundColor',
                }
            }
        )
    with_backoff(ws.spreadsheet.batch_update, {'requests': requests})


def _format_cells_at(ws: gspread.Worksheet, coords: list[tuple[int, int]], rgb: dict) -> None:
    """Apply background color to arbitrary 0-based cell coords."""
    if not coords:
        return
    requests: list[dict] = []
    for r0, c0 in coords:
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
    with_backoff(ws.spreadsheet.batch_update, {"requests": requests})


def _dedupe_coords(coords: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for coord in coords:
        if coord in seen:
            continue
        seen.add(coord)
        out.append(coord)
    return out


def _fetch_grid_with_formats(
    ws: gspread.Worksheet,
    *,
    max_rows: int = ONCALL_MAX_ROWS,
    max_cols: int = ONCALL_MAX_COLS,
) -> tuple[list[list[str]], list[list[dict | None]]]:
    """Return bounded worksheet values + background colors from gridData."""
    from ..ui import pickup_scan

    grid, bg_grid = pickup_scan._fetch_griddata(
        ws.spreadsheet,
        ws.title,
        max_rows=max_rows,
        max_cols=max_cols,
    )
    return (grid, bg_grid)


def _bg_at(bg_grid: list[list[dict | None]], row: int, col: int) -> dict | None:
    if row < 0 or col < 0:
        return None
    if row >= len(bg_grid):
        return None
    rr = bg_grid[row]
    if col >= len(rr):
        return None
    return rr[col]


def _is_redish(bg: dict | None) -> bool:
    if not bg:
        return False
    r = float(bg.get("red", 0.0) or 0.0)
    g = float(bg.get("green", 0.0) or 0.0)
    b = float(bg.get("blue", 0.0) or 0.0)
    if r >= 0.93 and g >= 0.93 and b >= 0.93:
        return False
    if (r >= 0.78) and (g <= 0.55) and (b <= 0.55):
        return True
    if r >= 0.85 and g >= 0.55 and b >= 0.55:
        if (r - g) >= 0.06 and (r - b) >= 0.06:
            return True
    return False


def _is_orangeish(bg: dict | None) -> bool:
    if not bg:
        return False
    r = float(bg.get("red", 0.0) or 0.0)
    g = float(bg.get("green", 0.0) or 0.0)
    b = float(bg.get("blue", 0.0) or 0.0)
    return (r >= 0.90) and (g >= 0.50) and (b <= 0.20)


def _local_naive(dt: datetime) -> datetime:
    return _to_la(dt).replace(tzinfo=None)


def _schedule_day_columns(
    grid: list[list[str]],
    *,
    ws_title: str,
    bucket_start: date,
) -> dict[date, tuple[str, int]]:
    from ..actions import chat_callout
    from ..ui import pickup_scan

    out: dict[date, tuple[str, int]] = {}
    kind = _campus_key_for_sheet_title(ws_title)
    generic_day_cols = pickup_scan._day_cols_from_grid(grid) if kind != "ONCALL" else {}
    for idx, day_canon in enumerate(DAY_ORDER):
        col = None
        try:
            if kind == "ONCALL":
                col = chat_callout._find_oncall_day_col(grid, day_canon, ws_title)
            else:
                col = generic_day_cols.get(day_canon)
                if col is None:
                    col = chat_callout._find_day_col(grid, day_canon)
        except Exception:
            col = None
        if col is None:
            continue
        out[bucket_start + timedelta(days=idx)] = (day_canon, col)
    return out


def _lane_coords_for_day(
    grid: list[list[str]],
    *,
    ws_title: str,
    day_col: int,
) -> list[tuple[int, int]]:
    from ..actions import chat_callout

    coords: list[tuple[int, int]] = []
    kind = _campus_key_for_sheet_title(ws_title)

    if kind == "ONCALL":
        label_rows: list[int] = []
        for r in range(len(grid)):
            v0 = (grid[r][0] if len(grid[r]) > 0 else "") or ""
            vd = (grid[r][day_col] if day_col < len(grid[r]) else "") or ""
            if chat_callout._RANGE_RE.match(str(v0).strip()) or chat_callout._RANGE_RE.match(str(vd).strip()):
                label_rows.append(r)
        if not label_rows:
            return []
        label_rows.append(len(grid))
        for i in range(len(label_rows) - 1):
            r_label = label_rows[i]
            r_next = label_rows[i + 1]
            for rr in range(r_label + 1, r_next):
                if rr >= len(grid):
                    continue
                coords.append((rr, day_col))
        return coords

    time_rows: list[int] = []
    for r, row in enumerate(grid):
        col0 = (row[0] if len(row) >= 1 else "") or ""
        if chat_callout._TIME_CELL_RE.match(col0) and chat_callout._parse_time_cell(col0):
            time_rows.append(r)
    if not time_rows:
        return []
    time_rows.append(len(grid))
    for i in range(len(time_rows) - 1):
        r0 = time_rows[i]
        r1 = time_rows[i + 1]
        for rr in range(r0 + 1, r1):
            if rr >= len(grid):
                continue
            coords.append((rr, day_col))
    return coords


def _find_schedule_coords(
    grid: list[list[str]],
    *,
    ws_title: str,
    target_name: str,
    day_canon: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[tuple[int, int]]:
    from ..actions import chat_callout

    kind = _campus_key_for_sheet_title(ws_title)
    if kind == "ONCALL":
        c0 = chat_callout._find_oncall_day_col(grid, day_canon, ws_title)
    else:
        c0 = chat_callout._find_day_col(grid, day_canon)
    if c0 is None:
        return []

    sdt = _local_naive(start_dt)
    edt = _local_naive(end_dt)
    if edt <= sdt:
        edt = edt + timedelta(days=1)

    target_coords: list[tuple[int, int]] = []

    if kind == "ONCALL":
        def _overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
            return (a0 < b1) and (a1 > b0)

        label_rows: list[int] = []
        for r in range(len(grid)):
            v0 = (grid[r][0] if len(grid[r]) > 0 else "") or ""
            vd = (grid[r][c0] if c0 < len(grid[r]) else "") or ""
            if chat_callout._RANGE_RE.match(str(v0).strip()) or chat_callout._RANGE_RE.match(str(vd).strip()):
                label_rows.append(r)
        if not label_rows:
            return []
        label_rows.append(len(grid))

        for i in range(len(label_rows) - 1):
            r_label = label_rows[i]
            r_next = label_rows[i + 1]
            shared_v0 = (grid[r_label][0] if len(grid[r_label]) > 0 else "") or ""
            day_v = (grid[r_label][c0] if c0 < len(grid[r_label]) else "") or ""
            range_txt = day_v if chat_callout._RANGE_RE.match(str(day_v).strip()) else shared_v0
            m = chat_callout._RANGE_RE.match(str(range_txt).strip()) if range_txt else None
            if not m:
                continue
            bs = chat_callout._parse_time_cell(m.group(1))
            be = chat_callout._parse_time_cell(m.group(2))
            if not bs or not be:
                continue
            bs_dt = datetime.combine(sdt.date(), bs.time())
            be_dt = datetime.combine(sdt.date(), be.time())
            if be_dt <= bs_dt:
                be_dt = be_dt + timedelta(days=1)
            if not _overlaps(bs_dt, be_dt, sdt, edt):
                continue
            for rr in range(r_label + 1, r_next):
                if rr >= len(grid) or c0 >= len(grid[rr]):
                    continue
                v = (grid[rr][c0] if c0 < len(grid[rr]) else "") or ""
                if chat_callout._cell_has_name_loose(v, target_name):
                    target_coords.append((rr, c0))
        return _dedupe_coords(target_coords)

    time_rows: list[int] = []
    for r, row in enumerate(grid):
        col0 = (row[0] if len(row) >= 1 else "") or ""
        if chat_callout._TIME_CELL_RE.match(col0) and chat_callout._parse_time_cell(col0):
            time_rows.append(r)
    if not time_rows:
        return []
    time_rows.append(len(grid))

    bands: dict[str, tuple[int, int]] = {}
    for i in range(len(time_rows) - 1):
        r0 = time_rows[i]
        r1 = time_rows[i + 1]
        start_label = (grid[r0][0] if len(grid[r0]) >= 1 else "") or ""
        dt = chat_callout._parse_time_cell(start_label)
        if dt:
            bands[dt.strftime("%I:%M %p")] = (r0, r1)

    for seg_s, _seg_e in chat_callout._range_to_slots(sdt, edt):
        lab = seg_s.strftime("%I:%M %p")
        if lab not in bands:
            continue
        r0, r1 = bands[lab]
        for rr in range(r0 + 1, r1):
            v = (grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else "") or ""
            if chat_callout._cell_has_name_loose(v, target_name):
                target_coords.append((rr, c0))
                break
    return _dedupe_coords(target_coords)


def _upsert_section(
    ws: gspread.Worksheet,
    section: Section,
    rows: list[RowOut],
    *,
    key_col: int,
    base_bg: dict,
    clear_past_before: date | None = None,
    clear_visible_row_when: Callable[[str], bool] | None = None,
) -> int:
    """Upsert rows into a section without clearing existing manual content.

    - Uses a far-right metadata column (key_col) to store oa_key for each managed row.
    - Updates existing managed rows in-place (idempotent).
    - Inserts new managed rows into the next available empty row in the visible column,
      without overwriting manual entries.
    - Clears only rows that are managed by the system (those that have a key) but are no
      longer present in the desired set (week rollover / interval changes).
    - Optionally clears legacy visible rows whose rendered event date is before
      clear_past_before, even if they do not have a metadata key yet.
    """
    start_r = section.start_row
    end_r = section.start_row + section.max_rows - 1
    vis_col = section.start_col
    ss = ws.spreadsheet

    # Ensure the sheet has enough grid size for the metadata key column.
    _ensure_ws_dimensions(ws, min_rows=end_r, min_cols=max(vis_col, key_col))

    vis_rng = f"{_col_letter(vis_col)}{start_r}:{_col_letter(vis_col)}{end_r}"
    key_rng = f"{_col_letter(key_col)}{start_r}:{_col_letter(key_col)}{end_r}"

    ranges = [_sheet_a1(ws.title, vis_rng), _sheet_a1(ws.title, key_rng)]
    vis_vals_raw, key_vals_raw = _values_batch_get(ss, ranges)

    def _col_list(raw: list[list[str]]) -> list[str]:
        out: list[str] = []
        for rr in raw:
            if rr and len(rr) > 0:
                out.append(str(rr[0]))
            else:
                out.append('')
        if len(out) < section.max_rows:
            out.extend([''] * (section.max_rows - len(out)))
        return out[: section.max_rows]

    vis_vals = _col_list(vis_vals_raw)
    key_vals = _col_list(key_vals_raw)

    value_updates: list[dict] = []
    fmt_rows: list[int] = []
    fmt_colors: list[dict] = []

    def _cell_a1(r: int, c: int) -> str:
        return f"{_col_letter(c)}{r}"

    # Upgrade cleanup: remove past-dated visible rows, including legacy rows
    # that were written before metadata keys existed.
    if clear_past_before is not None or clear_visible_row_when is not None:
        for i, txt in enumerate(vis_vals):
            txt_s = str(txt or "")
            clear_row = False
            if clear_visible_row_when is not None:
                try:
                    clear_row = bool(clear_visible_row_when(txt_s))
                except Exception:
                    clear_row = False
            if not clear_row and clear_past_before is not None:
                parsed = _infer_visible_row_date(txt_s, today=clear_past_before)
                clear_row = bool(parsed is not None and parsed < clear_past_before)
            if not clear_row:
                continue
            rownum = start_r + i
            value_updates.append({'range': _sheet_a1(ws.title, _cell_a1(rownum, vis_col)), 'values': [['']]})
            value_updates.append({'range': _sheet_a1(ws.title, _cell_a1(rownum, key_col)), 'values': [['']]})
            fmt_rows.append(rownum)
            fmt_colors.append(base_bg)
            vis_vals[i] = ''
            key_vals[i] = ''

    key_to_row: dict[str, int] = {}
    for i, k in enumerate(key_vals):
        kk = (k or '').strip()
        if kk:
            key_to_row[kk] = start_r + i

    desired_keys = [r.oa_key for r in rows if (r.oa_key or '').strip()]
    desired_set = set(desired_keys)

    # Clear managed rows not in desired set.
    # IMPORTANT: keep the section's base background color when clearing.
    for i, k in enumerate(key_vals):
        kk = (k or '').strip()
        if not kk:
            continue
        if kk not in desired_set:
            rownum = start_r + i
            value_updates.append({'range': _sheet_a1(ws.title, _cell_a1(rownum, vis_col)), 'values': [['']]})
            value_updates.append({'range': _sheet_a1(ws.title, _cell_a1(rownum, key_col)), 'values': [['']]})
            fmt_rows.append(rownum)
            fmt_colors.append(base_bg)

    # Insert/update desired rows.
    for rr in rows:
        k = (rr.oa_key or '').strip()
        if not k:
            continue
        if k in key_to_row:
            rownum = key_to_row[k]
        else:
            rownum = None
            for i in range(section.max_rows):
                if (vis_vals[i] or '').strip():
                    continue
                if (key_vals[i] or '').strip():
                    continue
                rownum = start_r + i
                break
            if rownum is None:
                rownum = end_r + 1

            idx = rownum - start_r
            if 0 <= idx < section.max_rows:
                vis_vals[idx] = 'X'
                key_vals[idx] = k
            key_to_row[k] = rownum

        txt = (rr.values[0] if rr.values else '').strip()
        value_updates.append({'range': _sheet_a1(ws.title, _cell_a1(rownum, vis_col)), 'values': [[txt]]})
        value_updates.append({'range': _sheet_a1(ws.title, _cell_a1(rownum, key_col)), 'values': [[k]]})
        # In swap-section columns, always keep the template fill.
        fmt_rows.append(rownum)
        fmt_colors.append(base_bg)

    _values_batch_update(ss, value_updates)

    try:
        _format_rows_at(ws, col=vis_col, rows=fmt_rows, colors=fmt_colors)
    except Exception:
        pass

    return len(rows)



def _header_cols_for_sheet(title: str) -> tuple[int, int]:
    """Return (weekly_col, future_col) for a schedule worksheet.

    Your workbook templates use consistent placements:
      - MC/UNH schedule tabs: I1 (weekly), J1 (future)
      - On Call tabs (including General + week tabs): K1 (weekly), L1 (future)

    Using fixed locations avoids expensive sheet reads and prevents 429
    quota errors from repeated header-scanning.
    """
    tl = (title or "").strip().lower()
    if re.search(r"\bon\s*[- ]?\s*call\b", tl) or "oncall" in tl:
        return (11, 12)  # K, L
    return (9, 10)  # I, J


def _ensure_fixed_headers(ws: gspread.Worksheet) -> tuple[Section, Section]:
    """Ensure swap headers exist at the fixed template positions (write-only).

    This function performs **no reads**. It simply writes the expected header
    strings into the known header cells for this worksheet template.
    """
    weekly_col, future_col = _header_cols_for_sheet(ws.title)
    # Write both headers in one values.update call.
    import gspread.utils as a1

    a1_start = a1.rowcol_to_a1(1, weekly_col)
    a1_end = a1.rowcol_to_a1(1, future_col)
    with_backoff(ws.update, f"{a1_start}:{a1_end}", [[HDR_WEEKLY, HDR_FUTURE]], value_input_option="RAW")

    weekly = compute_section(1, weekly_col, max_rows=SECTION_MAX_ROWS, num_cols=1)
    future = compute_section(1, future_col, max_rows=SECTION_MAX_ROWS, num_cols=1)
    return weekly, future


def _campus_key_for_sheet_title(title: str) -> str:
    tl = (title or "").strip().lower()
    if getattr(config, "SUMMER_MODE", False) and tl in {"may", "june", "july", "august"}:
        return "SUMMER"
    if re.search(r"\bon\s*[- ]?\s*call\b", tl) or "oncall" in tl:
        return "ONCALL"
    return utils.normalize_campus(title, "UNH")


def _worksheet_is_hidden(ws: gspread.Worksheet) -> bool:
    try:
        return bool(getattr(ws, "_properties", {}).get("hidden", False))
    except Exception:
        return False


def _should_auto_sync_worksheet(ws: gspread.Worksheet) -> bool:
    """Manual/global sync should only touch visible schedule worksheets."""
    if _worksheet_is_hidden(ws):
        return False
    tl = (getattr(ws, "title", "") or "").strip().lower()
    if tl in {"(names of hired oas)", "eo schedule policies", "audit log", "_locks"}:
        return False
    if getattr(config, "SUMMER_MODE", False):
        return tl in {"may", "june", "july", "august"}
    return bool("(oa and goas)" in tl or re.search(r"\bon\s*[- ]?call\b", tl))


def _bucket_window_for_sheet(title: str, *, today: date) -> tuple[date, date, bool]:
    """Return (bucket_start, bucket_end, allow_future_column) for a sheet.

    MC/UNH sheets use the existing app behavior:
      - "Shift Swaps for the week" = current LA calendar week
      - "Future Swaps/Call outs"  = anything after that week

    On-Call week tabs are different: each tab already corresponds to one specific
    week, so it should only show that tab's own week in the weekly column and it
    should not carry other On-Call weeks into the future column.
    """
    campus_key = _campus_key_for_sheet_title(title)
    if campus_key == "SUMMER":
        month_by_name = {
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
        }

        month_num = month_by_name.get((title or "").strip().lower())
        if month_num:
            year = today.year
            start = date(year, month_num, 1)

            if month_num == 12:
                end = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(year, month_num + 1, 1) - timedelta(days=1)

            # For summer month tabs, treat the whole visible month as the weekly/current section.
            # This puts the pickup/callout list under "Shift Swaps for the week" in column I.
            return start, end, False
    if campus_key == "ONCALL":
        try:
            wr = week_range_from_title(str(title or ""), today=today)
        except Exception:
            wr = None
        if wr:
            return wr[0], wr[1], False
        cw0, cw1 = _week_bounds_sun_sat(today)
        return cw0, cw1, False

    cw0, cw1 = _week_bounds_sun_sat(today)
    return cw0, cw1, True


def _bucket_label_for_sheet_event(title: str, event_d: date, *, today: date) -> str | None:
    bucket_start, bucket_end, allow_future = _bucket_window_for_sheet(title, today=today)
    if event_d < bucket_start:
        return None
    if bucket_start <= event_d <= bucket_end:
        return "weekly"
    if allow_future and event_d > bucket_end:
        return "future"
    return None


def _should_apply_grid_color_for_sheet(title: str, event_d: date, *, today: date) -> bool:
    """Whether an event should recolor the visible schedule grid on this sheet."""
    return _bucket_label_for_sheet_event(title, event_d, today=today) == "weekly"


def _looks_like_week_tab(title: str) -> bool:
    """Heuristic: does a tab title represent a specific week?

    We see a few patterns in this project:
      - "On Call 1/25 - 1/31"
      - "MC 1/25 - 1/31"
      - "On Call 928-104" (meaning 9/28 - 10/4)

    If we can't infer the week range, we use this to avoid wiping historical tabs.
    """
    t = title or ""
    # Any presence of two mm/dd tokens is treated as a week-tab indicator.
    mmdd = re.findall(r"\b\d{1,2}/\d{1,2}\b", t)
    if len(mmdd) >= 2:
        return True
    # Compact "928-104" style.
    if re.search(r"\b\d{3,4}\s*[-–—]\s*\d{3,4}\b", t):
        return True
    return False


def _query_all_from(supabase, table: str, *, start_date: date) -> list[dict]:
    resp = supabase.table(table).select("*").gte("event_date", start_date.isoformat()).execute()
    return list(getattr(resp, "data", None) or [])


def _apply_grid_colors(
    ws: gspread.Worksheet,
    *,
    callouts: list[dict],
    pickups: list[dict],
    today: date,
) -> list[str]:
    """Reconcile schedule colors for this sheet from Supabase truth.

    Workflow:
      1) Clear stale red/orange callout colors in the sheet's weekly bucket.
      2) Repaint active uncovered callouts red.
      3) Repaint active pickups orange so covered segments win.
    """
    errors: list[str] = []
    ws_title = ws.title

    try:
        grid, bg_grid = _fetch_grid_with_formats(ws)
    except Exception as e:
        return [f"grid_fetch_failed:{ws_title}:{e}"]
    if not grid:
        return [f"grid_fetch_empty:{ws_title}"]

    bucket_start, _bucket_end, _allow_future = _bucket_window_for_sheet(ws_title, today=today)
    day_cols = _schedule_day_columns(grid, ws_title=ws_title, bucket_start=bucket_start)

    clear_coords: list[tuple[int, int]] = []
    for day_d, (_day_canon, col) in day_cols.items():
        if _bucket_label_for_sheet_event(ws_title, day_d, today=today) != "weekly":
            continue
        for rr, cc in _lane_coords_for_day(grid, ws_title=ws_title, day_col=col):
            bg = _bg_at(bg_grid, rr, cc)
            if _is_redish(bg) or _is_orangeish(bg):
                clear_coords.append((rr, cc))

    pickups_by_target: dict[tuple[date, str], list[tuple[int, int]]] = {}
    for p in pickups:
        try:
            d = date.fromisoformat(str(p.get("event_date")))
        except Exception:
            continue
        if d < today or not _should_apply_grid_color_for_sheet(ws_title, d, today=today):
            continue
        sdt = _parse_dt(p.get("shift_start_at"))
        edt = _parse_dt(p.get("shift_end_at"))
        if not (sdt and edt):
            continue
        target_k = utils.name_key(str(p.get("target_name", "")).strip())
        if not target_k:
            continue
        pickups_by_target.setdefault((d, target_k), []).append((_mins(sdt), _mins(edt)))

    red_coords: list[tuple[int, int]] = []
    for c in callouts:
        try:
            d = date.fromisoformat(str(c.get("event_date")))
        except Exception:
            continue
        if d < today or not _should_apply_grid_color_for_sheet(ws_title, d, today=today):
            continue
        sdt = _parse_dt(c.get("shift_start_at"))
        edt = _parse_dt(c.get("shift_end_at"))
        if not (sdt and edt):
            continue
        caller = str(c.get("caller_name", "")).strip()
        caller_k = utils.name_key(caller)
        if not caller_k:
            continue

        base_local = _local_naive(sdt)
        base_interval = (_mins(sdt), _mins(edt))
        covers = pickups_by_target.get((d, caller_k), [])
        uncovered = _subtract_many(base_interval, covers)
        for a, b in uncovered:
            seg_s = base_local.replace(hour=a // 60 % 24, minute=a % 60, second=0, microsecond=0)
            b_min = 0 if b == 24 * 60 else b
            seg_e = base_local.replace(hour=b_min // 60 % 24, minute=b_min % 60, second=0, microsecond=0)
            if b == 24 * 60 or seg_e <= seg_s:
                seg_e = seg_e + timedelta(days=1)
            coords = _find_schedule_coords(
                grid,
                ws_title=ws_title,
                target_name=caller,
                day_canon=d.strftime("%A").lower(),
                start_dt=seg_s,
                end_dt=seg_e,
            )
            if coords:
                red_coords.extend(coords)
            else:
                errors.append(f"callout_color_target_not_found:{ws_title}:{caller}:{d}:{a}-{b}")

    orange_coords: list[tuple[int, int]] = []
    for p in pickups:
        try:
            d = date.fromisoformat(str(p.get("event_date")))
        except Exception:
            continue
        if d < today or not _should_apply_grid_color_for_sheet(ws_title, d, today=today):
            continue
        sdt = _parse_dt(p.get("shift_start_at"))
        edt = _parse_dt(p.get("shift_end_at"))
        if not (sdt and edt):
            continue
        target = str(p.get("target_name", "")).strip()
        if not target:
            continue
        coords = _find_schedule_coords(
            grid,
            ws_title=ws_title,
            target_name=target,
            day_canon=d.strftime("%A").lower(),
            start_dt=sdt,
            end_dt=edt,
        )
        if coords:
            orange_coords.extend(coords)
        else:
            errors.append(f"pickup_color_target_not_found:{ws_title}:{target}:{d}")

    changed = False
    clear_coords = _dedupe_coords(clear_coords)
    red_coords = _dedupe_coords(red_coords)
    orange_coords = _dedupe_coords(orange_coords)

    try:
        if clear_coords:
            _format_cells_at(ws, clear_coords, WHITE)
            changed = True
        if red_coords:
            _format_cells_at(ws, red_coords, RED)
            changed = True
        if orange_coords:
            _format_cells_at(ws, orange_coords, ORANGE)
            changed = True
        if changed:
            bump_ws_version(ws)
    except Exception as e:
        errors.append(f"grid_color_batch_failed:{ws_title}:{e}")

    return errors


def sync_swaps_to_sheets(
    ss: gspread.Spreadsheet,
    supabase,
    *,
    worksheet: gspread.Worksheet | None = None,
    sheet_title: str | None = None,
    apply_grid_colors: bool = False,
) -> dict[str, Any]:
    """Rewrite swap sections from Supabase into the workbook.

    If worksheet (or sheet_title) is provided, only that worksheet is updated.
    Otherwise, we update any worksheets that contain at least one swap header.

    Why this is necessary:
      - Some workbooks keep the schedule week shown in cells (or inferred by
        heuristics) out-of-sync with the calendar week definition we use for
        business logic (Sun→Sat).
      - Relying on inferred week ranges to select the target tab can therefore
        exclude the *actual* live schedule tabs, causing "headers not found"
        failures.

    We still keep a light safety heuristic to avoid wiping obvious archive tabs,
    but the primary filter is now "contains both headers".
    """

    today = la_today()
    cw0, cw1 = _week_bounds_sun_sat(today)

    # Choose target worksheets.
    # IMPORTANT: To avoid Google Sheets read-quota (429) issues, we do not scan
    # the sheet for headers. Your workbook templates use fixed header cells:
    #   - MC/UNH: I1 / J1
    #   - On Call: K1 / L1
    # We therefore write headers at those locations and treat the sections as
    # fixed underneath them (write-only, no reads).
    targets: list[gspread.Worksheet] = []
    sections_by_title: dict[str, tuple[Section, Section]] = {}
    if worksheet is not None:
        ws = worksheet
        weekly_sec, future_sec = _ensure_fixed_headers(ws)
        targets = [ws]
        sections_by_title[ws.title] = (weekly_sec, future_sec)
    elif sheet_title:
        ws = with_backoff(ss.worksheet, sheet_title)
        weekly_sec, future_sec = _ensure_fixed_headers(ws)
        targets = [ws]
        sections_by_title[ws.title] = (weekly_sec, future_sec)
    else:
        # Admin/manual run: update the primary schedule tabs only.
        for ws in with_backoff(ss.worksheets):
            if not _should_auto_sync_worksheet(ws):
                continue
            weekly_sec, future_sec = _ensure_fixed_headers(ws)
            targets.append(ws)
            sections_by_title[ws.title] = (weekly_sec, future_sec)

    if not targets:
        raise ValueError("No schedule worksheets found to sync swaps into.")

    # Query Supabase once, starting at today. This keeps the rendered sections
    # focused on live and upcoming events and prevents past entries from being
    # re-rendered midweek.
    callouts_all = _query_all_from(supabase, "callouts", start_date=today)
    pickups_all = _query_all_from(supabase, "pickups", start_date=today)

    summary: dict[str, Any] = {
        "calendar_week": {"start": cw0.isoformat(), "end": cw1.isoformat()},
        "min_event_date": today.isoformat(),
        "sheets_updated": [],
        "sheet_errors": [],
        "weekly_written": 0,
        "future_written": 0,
        "grid_errors": [],
    }

    # Optional grid reconciliation pass:
    #   - clears stale red/orange schedule cells in the weekly bucket
    #   - repaints active weekly callouts/pickups from Supabase
    do_grid = bool(apply_grid_colors)

    for ws in targets:
        weekly_sec, future_sec = sections_by_title[ws.title]

        campus_key = _campus_key_for_sheet_title(ws.title)
        bucket_start, bucket_end, allow_future = _bucket_window_for_sheet(ws.title, today=today)

        weekly_clear_predicate = None
        future_clear_predicate = None
        if campus_key == "ONCALL":
            def _weekly_clear(txt: str, *, _today=bucket_start, _ws=bucket_start, _we=bucket_end) -> bool:
                parsed = _infer_visible_row_date(txt, today=_today)
                return bool(parsed is not None and not (_ws <= parsed <= _we))

            def _future_clear(txt: str, *, _today=bucket_start) -> bool:
                parsed = _infer_visible_row_date(txt, today=_today)
                return bool(parsed is not None)

            weekly_clear_predicate = _weekly_clear
            future_clear_predicate = _future_clear

        def _campus_match(row: dict) -> bool:
            rc = str(row.get("campus", "") or "").strip()
            if campus_key == "ONCALL":
                # Historical data sometimes stored as "ON" / "On Call".
                return rc.lower().startswith("on")
            # Tolerate storing full tab titles or other variants.
            try:
                return utils.normalize_campus(rc, campus_key) == campus_key
            except Exception:
                return rc.upper().startswith(campus_key)

        c_rows = [r for r in callouts_all if _campus_match(r)]
        p_rows = [r for r in pickups_all if _campus_match(r)]

        # Build pickup intervals keyed by (event_date, target_name_key).
        pickups_by_target: dict[tuple[date, str], list[tuple[int, int]]] = {}
        for p in p_rows:
            _row, interval, key = _row_from_pickup(p)
            if interval is None or key is None:
                continue
            d, target_k = key
            pickups_by_target.setdefault((d, target_k), []).append(interval)

        weekly_out: list[RowOut] = []
        future_out: list[RowOut] = []

        # Callouts: render uncovered segments.
        for c in c_rows:
            try:
                d = date.fromisoformat(str(c.get("event_date")))
            except Exception:
                continue
            bucket = _bucket_label_for_sheet_event(ws.title, d, today=today)
            if not bucket:
                continue

            caller_k = utils.name_key(str(c.get("caller_name", "")).strip())
            rows, _uncovered = _rows_from_callout(c, pickup_intervals=pickups_by_target.get((d, caller_k), []))
            if not rows:
                continue
            if bucket == "weekly":
                weekly_out.extend(rows)
            elif bucket == "future":
                future_out.extend(rows)

        # Pickups: bucket by date.
        for p in p_rows:
            row, _interval, _key = _row_from_pickup(p)
            if row is None:
                continue
            # derive date from sort_key
            d_iso = str(row.sort_key[0])
            try:
                d = date.fromisoformat(d_iso)
            except Exception:
                continue
            bucket = _bucket_label_for_sheet_event(ws.title, d, today=today)
            if bucket == "weekly":
                weekly_out.append(row)
            elif bucket == "future":
                future_out.append(row)

        weekly_out.sort(key=lambda r: r.sort_key)
        future_out.sort(key=lambda r: r.sort_key)

        wrote_any = False
        try:
            if weekly_sec:
                n1 = _upsert_section(
                    ws,
                    weekly_sec,
                    weekly_out,
                    key_col=KEY_WEEKLY_COL,
                    base_bg=SWAP_WEEKLY_BG,
                    clear_past_before=today,
                    clear_visible_row_when=weekly_clear_predicate,
                )
                summary["weekly_written"] += n1
                wrote_any = True
            if future_sec:
                n2 = _upsert_section(
                    ws,
                    future_sec,
                    future_out,
                    key_col=KEY_FUTURE_COL,
                    base_bg=SWAP_FUTURE_BG,
                    clear_past_before=today,
                    clear_visible_row_when=future_clear_predicate,
                )
                summary["future_written"] += n2
                wrote_any = True
            if wrote_any:
                summary["sheets_updated"].append(ws.title)
        except Exception as e:
            summary["sheet_errors"].append({"sheet": ws.title, "error": str(e)})
            # Don't attempt grid coloring if the write failed.
            continue

        if do_grid:
            errs = _apply_grid_colors(ws, callouts=c_rows, pickups=p_rows, today=today)
            summary["grid_errors"].extend(errs)

    # In strict single-sheet mode (approval path), bubble failures so the UI can
    # tell the admin exactly why the sheet was not updated.
    if sheet_title and (not summary.get("sheets_updated")) and (summary.get("sheet_errors")):
        first = (summary.get("sheet_errors") or [{}])[0]
        raise ValueError(f"Swap section write failed for '{sheet_title}'. Details: {first}")

    return summary
