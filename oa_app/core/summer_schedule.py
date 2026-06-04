from __future__ import annotations

import re
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, List, Dict

import gspread
import gspread.utils as a1

from .. import config


SHIFT_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}\s*(?:am|pm))\s*[-–]\s*(\d{1,2}:\d{2}\s*(?:am|pm))\s*$",
    re.I,
)

DATE_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*$"
)

MONTH_TABS = {
    "may",
    "june",
    "july",
    "august",
    "may 2026",
    "june 2026",
    "july 2026",
    "august 2026",
}


def _norm_name(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"^\s*(?:OA|GOA)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    return " ".join(text.lower().split())


def _clean_time_label(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(
        r"\s*(am|pm)\s*$",
        lambda m: " " + m.group(1).upper(),
        text,
        flags=re.I,
    )
    return datetime.strptime(text, "%I:%M %p").strftime("%I:%M %p").lstrip("0")


def parse_shift_label(value: object) -> Optional[Tuple[str, str]]:
    text = str(value or "").strip()
    match = SHIFT_RE.match(text)
    if not match:
        return None

    return _clean_time_label(match.group(1)), _clean_time_label(match.group(2))


def parse_date_cell(value: object) -> Optional[date]:
    """
    Strict date parser for the summer sheet.

    Important:
    Do NOT use dateutil fuzzy parsing here. It can accidentally parse
    shift labels, weekday names, or random text as dates.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    # Excel serial date support, useful if testing from .xlsx.
    # Google Sheets via gspread usually returns formatted strings instead.
    if isinstance(value, (int, float)):
        if 40000 <= float(value) <= 50000:
            return date(1899, 12, 30) + timedelta(days=int(value))
        return None

    text = str(value or "").strip()
    if not text:
        return None

    # Only accept actual date-looking cells like 6/1/2026 or 6/1/26.
    match = DATE_RE.match(text)
    if not match:
        return None

    month = int(match.group(1))
    day = int(match.group(2))
    year_raw = match.group(3)

    if year_raw is None:
        # Your summer schedule is 2026.
        year = 2026
    else:
        year = int(year_raw)
        if year < 100:
            year += 2000

    try:
        return date(year, month, day)
    except ValueError:
        return None


def month_tab_candidates(target_date: date) -> List[str]:
    month_name = target_date.strftime("%B")
    return [
        month_name,
        month_name[:3],
        f"{month_name} {target_date.year}",
        f"{month_name[:3]} {target_date.year}",
    ]


def open_month_worksheet(ss: gspread.Spreadsheet, target_date: date) -> gspread.Worksheet:
    candidates = {c.lower() for c in month_tab_candidates(target_date)}

    for ws in ss.worksheets():
        title = ws.title.strip()
        if title.lower() in candidates:
            return ws

    available = ", ".join(w.title for w in ss.worksheets())
    raise ValueError(
        f"Could not find a month tab for {target_date.strftime('%B %Y')}. "
        f"Available tabs: {available}"
    )


def date_cells_in_grid(grid: List[List[str]]) -> List[Tuple[int, int, date]]:
    """
    Only return cells from real date rows.

    A real summer date row has several date cells across Sunday-Saturday.
    This prevents weekday labels, shift labels, and random text from being
    treated as dates.
    """
    found: List[Tuple[int, int, date]] = []

    for r, row in enumerate(grid):
        row_dates: List[Tuple[int, date]] = []

        for c, value in enumerate(row):
            d = parse_date_cell(value)
            if d:
                row_dates.append((c, d))

        # A real week date row should have multiple date cells.
        # Usually 7, but allow 3+ to be safe.
        if len(row_dates) >= 3:
            for c, d in row_dates:
                found.append((r, c, d))

    return found


def find_date_cell(grid: List[List[str]], target_date: date) -> Optional[Tuple[int, int]]:
    for r, c, d in date_cells_in_grid(grid):
        if d == target_date:
            return r, c
    return None


def _row_has_week_marker(row: List[str]) -> bool:
    return any(
        re.match(r"^\s*week\s+\d+\s*$", str(cell or ""), flags=re.I)
        for cell in row
    )


def _next_date_row_after(grid: List[List[str]], date_row: int) -> int:
    for r in range(date_row + 1, len(grid)):
        row = grid[r] if r < len(grid) else []

        if _row_has_week_marker(row):
            return r

        count_dates = sum(1 for cell in row if parse_date_cell(cell))
        if count_dates >= 3:
            return r

    return len(grid)


def find_shift_block(
    grid: List[List[str]],
    date_row: int,
    col: int,
    start_label: str,
    end_label: str,
) -> Optional[Tuple[int, int]]:
    """
    Find the matching shift block for one exact date column.

    Search is limited to the current week block only.
    """
    target = (_clean_time_label(start_label), _clean_time_label(end_label))

    week_end = _next_date_row_after(grid, date_row)

    label_rows: List[int] = []

    for r in range(date_row + 1, week_end):
        row = grid[r] if r < len(grid) else []
        cell = row[col] if col < len(row) else ""

        if parse_shift_label(cell):
            label_rows.append(r)

    for i, r_label in enumerate(label_rows):
        row = grid[r_label] if r_label < len(grid) else []
        cell = row[col] if col < len(row) else ""

        if parse_shift_label(cell) != target:
            continue

        r_next = label_rows[i + 1] if i + 1 < len(label_rows) else week_end

        return r_label, r_next

    return None


def get_shift_people(
    grid: List[List[str]],
    col: int,
    r_label: int,
    r_next: int,
) -> List[Tuple[int, str]]:
    people: List[Tuple[int, str]] = []

    for rr in range(r_label + 1, r_next):
        row = grid[rr] if rr < len(grid) else []
        cell = row[col] if col < len(row) else ""
        text = str(cell or "").strip()

        if not text:
            continue

        if parse_shift_label(text):
            continue

        if _row_has_week_marker(row):
            break

        if sum(1 for item in row if parse_date_cell(item)) >= 3:
            break

        people.append((rr, text))

    return people


def first_empty_row(
    grid: List[List[str]],
    col: int,
    r_label: int,
    r_next: int,
) -> Optional[int]:
    for rr in range(r_label + 1, r_next):
        row = grid[rr] if rr < len(grid) else []

        if _row_has_week_marker(row):
            return None

        if sum(1 for item in row if parse_date_cell(item)) >= 3:
            return None

        cell = row[col] if col < len(row) else ""

        if not str(cell or "").strip():
            return rr

    return None


def find_shift(
    ss: gspread.Spreadsheet,
    target_date: date,
    start_label: str,
    end_label: str,
) -> Tuple[gspread.Worksheet, List[List[str]], int, int, int]:
    ws = open_month_worksheet(ss, target_date)
    grid = ws.get_all_values()

    date_cell = find_date_cell(grid, target_date)
    if not date_cell:
        raise ValueError(f"Could not find date {target_date} on tab '{ws.title}'.")

    date_row, col = date_cell

    block = find_shift_block(grid, date_row, col, start_label, end_label)
    if not block:
        raise ValueError(
            f"Could not find shift {start_label}-{end_label} "
            f"for {target_date} on tab '{ws.title}'."
        )

    r_label, r_next = block
    return ws, grid, col, r_label, r_next


def add_person_to_shift(
    ss: gspread.Spreadsheet,
    target_date: date,
    start_label: str,
    end_label: str,
    person_name: str,
    role_prefix: str = "OA",
    capacity: int = 9,
) -> str:
    ws, grid, col, r_label, r_next = find_shift(
        ss,
        target_date,
        start_label,
        end_label,
    )

    people = get_shift_people(grid, col, r_label, r_next)

    person_key = _norm_name(person_name)
    for _rr, existing in people:
        if _norm_name(existing) == person_key:
            raise ValueError(
                f"{person_name} is already scheduled for "
                f"{target_date} {start_label}-{end_label}."
            )

    if len(people) >= capacity:
        raise ValueError(
            f"This shift is full ({len(people)}/{capacity}) for "
            f"{target_date} {start_label}-{end_label}."
        )

    empty_rr = first_empty_row(grid, col, r_label, r_next)
    if empty_rr is None:
        raise ValueError(
            f"No blank row available under {target_date} {start_label}-{end_label}. "
            f"Add more blank rows in the spreadsheet."
        )

    value = f"{role_prefix}: {person_name}"
    cell_ref = a1.rowcol_to_a1(empty_rr + 1, col + 1)

    ws.update(cell_ref, [[value]])

    return (
        f"Added {value} to {ws.title} on "
        f"{target_date.strftime('%A %-m/%-d/%Y')} "
        f"{start_label}-{end_label}."
    )


def remove_person_from_shift(
    ss: gspread.Spreadsheet,
    target_date: date,
    start_label: str,
    end_label: str,
    person_name: str,
) -> str:
    ws, grid, col, r_label, r_next = find_shift(
        ss,
        target_date,
        start_label,
        end_label,
    )

    person_key = _norm_name(person_name)

    for rr, existing in get_shift_people(grid, col, r_label, r_next):
        if _norm_name(existing) == person_key:
            cell_ref = a1.rowcol_to_a1(rr + 1, col + 1)
            ws.update(cell_ref, [[""]])
            return (
                f"Removed {person_name} from {ws.title} on "
                f"{target_date.strftime('%A %-m/%-d/%Y')} "
                f"{start_label}-{end_label}."
            )

    raise ValueError(
        f"Could not find {person_name} in "
        f"{target_date} {start_label}-{end_label}."
    )


def list_person_shifts(
    ss: gspread.Spreadsheet,
    person_name: str,
) -> Dict[str, List[Tuple[date, str, str, str]]]:
    """
    Returns:
      {
        "Aidan Hodges": [
          (date, tab_title, start, end),
          ...
        ]
      }
    """
    person_key = _norm_name(person_name)
    found: List[Tuple[date, str, str, str]] = []

    for ws in ss.worksheets():
        title = ws.title.strip()

        if title.lower() not in MONTH_TABS:
            continue

        grid = ws.get_all_values()

        shift_windows = getattr(
            config,
            "SUMMER_SHIFT_WINDOWS",
            [("7:00 AM", "3:30 PM"), ("3:30 PM", "12:00 AM")],
        )
        for date_row, col, d in date_cells_in_grid(grid):
            for start, end in shift_windows:
                block = find_shift_block(grid, date_row, col, start, end)
                if not block:
                    continue

                r_label, r_next = block

                for _rr, existing in get_shift_people(grid, col, r_label, r_next):
                    if _norm_name(existing) == person_key:
                        found.append((d, title, start, end))
                        break

    deduped = []
    seen = set()

    for item in found:
        key = (item[0].isoformat(), item[1], item[2], item[3])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda x: (x[0], x[2]))
    return {person_name: deduped}

_RED = {"red": 0.95, "green": 0.25, "blue": 0.25}
_ORANGE = {"red": 1.0, "green": 0.65, "blue": 0.0}


def mark_person_callout(
    ss: gspread.Spreadsheet,
    target_date: date,
    start_label: str,
    end_label: str,
    person_name: str,
    covered_by: str | None = None,
) -> str:
    """
    Summer-sheet callout:
    color the exact cell containing the person's name red if uncovered,
    orange if covered.
    """
    ws, grid, col, r_label, r_next = find_shift(
        ss,
        target_date,
        start_label,
        end_label,
    )

    person_key = _norm_name(person_name)

    for rr, existing in get_shift_people(grid, col, r_label, r_next):
        if _norm_name(existing) == person_key:
            color = _ORANGE if covered_by else _RED

            ws.spreadsheet.batch_update(
                {
                    "requests": [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": ws.id,
                                    "startRowIndex": rr,
                                    "endRowIndex": rr + 1,
                                    "startColumnIndex": col,
                                    "endColumnIndex": col + 1,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": color
                                    }
                                },
                                "fields": "userEnteredFormat.backgroundColor",
                            }
                        }
                    ]
                }
            )

            status = "orange/covered" if covered_by else "red/no cover"
            return (
                f"Call-Out marked for {person_name} on {ws.title} "
                f"{target_date.strftime('%A %-m/%-d/%Y')} "
                f"{start_label}-{end_label} — {status}."
            )

    raise ValueError(
        f"Could not find {person_name} in "
        f"{target_date} {start_label}-{end_label}."
    )