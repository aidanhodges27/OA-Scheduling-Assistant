# oa_app/chat_remove.py
from __future__ import annotations
from datetime import datetime, timedelta   # ✅ timedelta imported here
from typing import List, Optional
import gspread
import streamlit as st

from .. import config
from ..core import summer_schedule

from ..core.quotas import bump_ws_version, seed_batch_get_cache

from ..core.utils import fmt_time
from ..core.locks import get_or_create_locks_sheet, acquire_fcfs_lock, lock_key
from ..ui.schedule_query import (
    _TIME_CELL_RE,
    _read_grid,
    _RANGE_RE,
    _parse_time_cell,
)
from .chat_add import (
    _ensure_dt, _is_half_hour_boundary_dt, _range_to_slots,
    _canon_input_day,
    _day_cols_from_first_row, _header_day_cols,
    _find_day_col_anywhere, _find_day_col_fuzzy,
    _infer_day_cols_by_blocks, _find_oncall_block_row_bounds,
    _find_working_oncall_ws, _resolve_campus_title,
    _is_blankish,
)
from ..core.hours import compute_hours_fast
from ..config import ONCALL_MAX_COLS, ONCALL_MAX_ROWS, ROSTER_SHEET, AUDIT_SHEET, LOCKS_SHEET


def handle_remove(
    st, ss, schedule, *,
    canon_target_name: str,
    campus_title: str,
    day: str,
    start, end
) -> str:
    """Removes OA from the specified shift/block (UNH / MC / On-Call)."""
    debug_log: List[str] = []

    def dbg(msg: str):
        debug_log.append(str(msg))

    def fail(msg: str):
        log = "\n".join(debug_log[-400:])
        raise ValueError(f"{msg}\n\n--- DEBUG ---------------------------------\n{log if log else '(no debug)'}\n-------------------------------------------")

    # Resolve tab
    sidebar_tab = st.session_state.get("active_sheet")
    sheet_title, campus_kind = _resolve_campus_title(ss, campus_title, sidebar_tab)
    dbg(f"📌 Using sheet: {sheet_title} ({campus_kind})")

    # ----- Normalize times -----
    start_dt = _ensure_dt(start)
    end_dt   = _ensure_dt(end, ref_date=start_dt.date())

    # Handle overnight ranges like 7 PM–12 AM or 10 PM–2 AM
    if not (_is_half_hour_boundary_dt(start_dt) and _is_half_hour_boundary_dt(end_dt)):
        fail("Times must be on 30-minute boundaries (:00 or :30).")
    if end_dt <= start_dt:
        # Allow after-midnight ends (00:00 – 05:59)
        if 0 <= end_dt.time().hour <= 5:
            end_dt = end_dt + timedelta(days=1)
            dbg("⏩ Rolled end time to next day for after-midnight window.")
        else:
            fail("End time must be after start time.")

    req_slots = _range_to_slots(start_dt, end_dt)
    req_hours = len(req_slots) * 0.5
    dbg(f"🕒 Removing {fmt_time(start_dt)}–{fmt_time(end_dt)} ({req_hours:.1f} h)")

    # Canonicalize weekday
    day_canon = _canon_input_day(day)
    if not day_canon:
        fail(f"Couldn't understand the day '{day}'.")
    dbg(f"🧭 Day parsed: {day_canon}")

    # ───────────────────────── Summer block schedule ─────────────────────────
    if getattr(config, "SUMMER_MODE", False):
        start_label = fmt_time(start_dt)
        end_label = fmt_time(end_dt)

        valid_windows = set(getattr(config, "SUMMER_SHIFT_WINDOWS", []))
        if (start_label, end_label) not in valid_windows:
            fail(
                "Summer shifts must be either "
                "7:00 AM–3:30 PM or 3:30 PM–12:00 AM."
            )

        target_date = None

        try:
            from ..ui.page import _date_for_weekday_in_sheet
            target_date = _date_for_weekday_in_sheet(ss, sheet_title, day_canon)
        except Exception:
            target_date = None

        if target_date is None:
            try:
                from ..ui.page import _date_for_weekday_in_current_la_week
                target_date = _date_for_weekday_in_current_la_week(day_canon)
            except Exception:
                target_date = None

        if target_date is None:
            fail(
                "Could not figure out the actual date for this summer shift. "
                "Summer remove needs a date, not just a weekday."
            )

        return summer_schedule.remove_person_from_shift(
            ss=ss,
            target_date=target_date,
            start_label=start_label,
            end_label=end_label,
            person_name=canon_target_name,
        )

    # Fast, cached baseline for success messaging (avoid expensive full rescans).
    # Cache key for hours should change when the underlying sheets change.
    # The UI supplies a version-keyed epoch via _LAST_HOURS.
    epoch = st.session_state.get("_LAST_HOURS", {}).get("epoch") or st.session_state.get(
        "UI_EPOCH", st.session_state.get("HOURS_EPOCH", 0)
    )
    try:
        hours_before = float(compute_hours_fast(ss, schedule, canon_target_name, epoch=epoch))
    except Exception:
        hours_before = None

    any_removed = False

    # ---------- ON-CALL ----------
    if campus_kind == "ONCALL":
        # Open tab directly (avoid schedule._get_sheet which expects weekday headers)
        try:
            ws = ss.worksheet(sheet_title)
        except Exception as e:
            fail(f"Could not open worksheet '{sheet_title}': {e}")

        # Lock
        locks_ws = get_or_create_locks_sheet(ss)
        k = lock_key(ws.title, day_canon, fmt_time(start_dt), fmt_time(end_dt))
        won, _ = acquire_fcfs_lock(locks_ws, k, canon_target_name, ttl_sec=90)
        if not won:
            fail("Another request just claimed this window. Try again.")

        grid = _read_grid(ws)
        if not grid:
            fail("On-Call sheet is empty.")

        # Discover column for this weekday
        day_cols = _day_cols_from_first_row(grid, dbg=dbg)
        if day_canon not in day_cols:
            hdr = _header_day_cols(grid, dbg=dbg)
            for k2, v2 in hdr.items():
                day_cols.setdefault(k2, v2)
        if day_canon not in day_cols:
            inferred = _infer_day_cols_by_blocks(grid, dbg=dbg)
            for k2, v2 in inferred.items():
                day_cols.setdefault(k2, v2)
        if day_canon not in day_cols:
            c_guess = _find_day_col_anywhere(grid, day_canon)
            if c_guess is not None:
                day_cols[day_canon] = c_guess
        if day_canon not in day_cols:
            c_guess2 = _find_day_col_fuzzy(grid, day_canon, dbg=dbg)
            if c_guess2 is not None:
                day_cols[day_canon] = c_guess2
        if day_canon not in day_cols:
            fail(f"Could not read weekday header from '{ws.title}'.")

        c0 = day_cols[day_canon]
        c1 = c0  # same column holds the OA names (correct for On-Call)

        want_s = start_dt.strftime("%I:%M %p")
        want_e = end_dt.strftime("%I:%M %p")
        bounds = _find_oncall_block_row_bounds(grid, c0, want_s, want_e)
        if not bounds:
            fail(f"Could not locate On-Call block '{want_s} – {want_e}' in '{ws.title}'.")
        r_label, r_next = bounds
        lane_rows = list(range(r_label + 1, r_next))
        if not lane_rows:
            fail("On-Call block has no lane rows defined.")

        removed = False
        for rr in lane_rows:
            v = grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else ""
            if v and canon_target_name.lower() in v.lower():
                import gspread.utils as a1
                ref = a1.rowcol_to_a1(rr + 1, c1 + 1)
                ws.batch_update([{ "range": f"{ref}:{ref}", "values": [[""]] }])
                bump_ws_version(ws)
                # Keep our in-memory grid consistent so we can seed caches.
                if rr < len(grid):
                    if c0 >= len(grid[rr]):
                        grid[rr] = list(grid[rr]) + [""] * (c0 + 1 - len(grid[rr]))
                    grid[rr][c0] = ""
                removed = True
                any_removed = True
                dbg(f"🧹 Cleared row {rr+1}, col {c1+1} → '{v}'")

        # Seed the cache for the bumped worksheet version so the rerun is instant.
        if removed:
            try:
                import gspread.utils as a1
                end_col_letter = a1.rowcol_to_a1(1, ONCALL_MAX_COLS).split("1")[0]
                full_range = f"A1:{end_col_letter}{ONCALL_MAX_ROWS}"
                seed_batch_get_cache(ws, [full_range], [grid])
            except Exception:
                pass
        if not removed:
            fail(f"{canon_target_name} not found in block '{want_s} – {want_e}'.")

        target_title = ws.title

    # ---------- UNH / MC ----------
    else:
        info = schedule._get_sheet(sheet_title)
        ws0: gspread.Worksheet = getattr(info, "ws", info)
        grid = _read_grid(ws0)
        day_cols = _header_day_cols(grid, dbg=dbg)
        if day_canon not in day_cols:
            c_guess = _find_day_col_anywhere(grid, day_canon)
            if c_guess is not None:
                day_cols[day_canon] = c_guess
        if day_canon not in day_cols:
            fail(f"Could not read weekday header (day '{day_canon}' missing).")

        c0 = day_cols[day_canon]
        # ✅ FIX: names are in the SAME column as the header (like in handle_add). Do NOT shift by +1.
        col_1based = c0 + 1

        # Build time bands
        rows = [r for r, row in enumerate(grid)
                if len(row) >= 1 and _TIME_CELL_RE.match(row[0] or "") and _parse_time_cell(row[0])]
        rows.append(len(grid))
        bands = {
            _parse_time_cell(grid[r0][0]).strftime("%I:%M %p").lstrip("0"): (r0, r1)
            for r0, r1 in zip(rows, rows[1:])
            if _parse_time_cell(grid[r0][0])
        }

        pending_clears: list[tuple[int,int]] = []  # (row1, col1)

        for (sdt, _edt) in _range_to_slots(start_dt, end_dt):
            label = sdt.strftime("%I:%M %p").lstrip("0")
            if label not in bands:
                fail(f"Slot {label} not found in sheet.")
            r0, r1 = bands[label]
            lane_rows = list(range(r0 + 1, r1))
            cleared = False
            for rr in lane_rows:
                v = grid[rr][c0] if (rr < len(grid) and c0 < len(grid[rr])) else ""
                if v and canon_target_name.lower() in v.lower():
                    pending_clears.append((rr + 1, col_1based))
                    cleared = True
                    any_removed = True
                    dbg(f"🧹 Cleared {label} r{rr+1} c{col_1based}")
                    # keep local grid in sync for subsequent slots
                    if rr < len(grid):
                        if c0 >= len(grid[rr]):
                            grid[rr] = list(grid[rr]) + [""] * (c0 + 1 - len(grid[rr]))
                        grid[rr][c0] = ""
            if not cleared:
                dbg(f"⚠️ {label}: {canon_target_name} not found in any lane.")
        if pending_clears:
            import gspread.utils as a1
            data = []
            for r1, c1 in pending_clears:
                ref = a1.rowcol_to_a1(r1, c1)
                data.append({"range": f"{ref}:{ref}", "values": [[""]]})
            ws0.batch_update(data)
            bump_ws_version(ws0)

            # Seed the full-grid cache for the bumped version.
            try:
                end_col_letter = a1.rowcol_to_a1(1, ONCALL_MAX_COLS).split("1")[0]
                full_range = f"A1:{end_col_letter}{ONCALL_MAX_ROWS}"
                seed_batch_get_cache(ws0, [full_range], [grid])
            except Exception:
                pass

        target_title = ws0.title

    # Success message (predict without a full strict rescan)
    if hours_before is None:
        fresh_total = None
    else:
        # Only show an updated total if the removal actually happened.
        fresh_total = max(0.0, float(hours_before) - (req_hours if any_removed else 0.0))
    return (
        f"Removed **{canon_target_name}** from **{target_title}** "
        f"({day_canon.title()} {fmt_time(start_dt)}–{fmt_time(end_dt)}). "
        + (f"Now at **{fresh_total:.1f}h / 20h** this week." if fresh_total is not None else "")
    )
