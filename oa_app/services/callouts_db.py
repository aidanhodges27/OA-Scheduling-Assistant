"""Callouts DB helpers (Supabase).

Backend-only table:
  public.callouts

This module is intentionally small:
- Safe to import when Supabase is not configured.
- Provides idempotent upsert keyed by approval_id.
- Provides a simple weekly sum query for hours adjustments.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..core.utils import name_key
from ..integrations.supabase_io import get_supabase, supabase_enabled, with_retry


def supabase_callouts_enabled() -> bool:
    return supabase_enabled()


def upsert_callout(payload: dict) -> dict:
    """Upsert a callout row keyed by approval_id.

    Returns inserted/updated row dict (best-effort).
    """
    if not supabase_callouts_enabled():
        return {}
    sb = get_supabase()
    resp = with_retry(lambda: sb.table("callouts").upsert(payload, on_conflict="approval_id").execute())
    data = getattr(resp, "data", None) or []
    return data[0] if data else {}


def _parse_iso_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _coerce_duration_hours(row: dict[str, Any]) -> float:
    try:
        hours = float(row.get("duration_hours") or 0.0)
    except Exception:
        hours = 0.0
    if hours > 0:
        return float(hours)

    start_at = _parse_iso_dt(row.get("shift_start_at"))
    end_at = _parse_iso_dt(row.get("shift_end_at"))
    if not (start_at and end_at):
        return 0.0
    if end_at <= start_at:
        end_at = end_at + timedelta(days=1)
    return max(0.0, float((end_at - start_at).total_seconds() / 3600.0))


def _coerce_notice_hours(row: dict[str, Any]) -> float | None:
    try:
        notice = float(row.get("notice_hours"))
        return float(notice)
    except Exception:
        notice = None

    submitted_at = _parse_iso_dt(row.get("submitted_at"))
    start_at = _parse_iso_dt(row.get("shift_start_at"))
    if not (submitted_at and start_at):
        return notice
    return float((start_at - submitted_at).total_seconds() / 3600.0)


def _late_notice_rule(row: dict[str, Any]) -> str | None:
    notice = _coerce_notice_hours(row)
    if notice is None:
        return None
    reason = str(row.get("reason") or "").strip().lower()
    is_sick = reason.startswith("sick")
    if is_sick and notice < 2.0:
        return "Sick callout under 2 hours"
    if (not is_sick) and notice < 48.0:
        return "Non-sick callout under 48 hours"
    return None


def list_callouts_in_range(*, week_start: date, week_end: date) -> list[dict[str, Any]]:
    """Return callout rows within [week_start, week_end] (inclusive)."""
    if not supabase_callouts_enabled():
        return []
    sb = get_supabase()
    resp = with_retry(
        lambda: sb.table("callouts")
        .select("event_date,duration_hours,caller_name,campus,reason,shift_start_at,shift_end_at,submitted_at")
        .gte("event_date", str(week_start))
        .lte("event_date", str(week_end))
        .execute()
    )
    rows: list[dict[str, Any]] = getattr(resp, "data", None) or []
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        row["duration_hours"] = _coerce_duration_hours(row)
        out.append(row)
    return out


def list_callouts_for_week(*, caller_name: str, week_start: date, week_end: date) -> list[dict[str, Any]]:
    """Return current-week callout rows for one caller (best-effort)."""
    caller = (caller_name or "").strip()
    rows = list_callouts_in_range(week_start=week_start, week_end=week_end)
    target_k = name_key(caller)
    out: list[dict[str, Any]] = []
    for r in rows:
        if name_key(str(r.get("caller_name", ""))) != target_k:
            continue
        row = dict(r)
        out.append(row)
    return out


def list_late_notice_callouts(*, week_start: date, week_end: date, limit: int = 200) -> list[dict[str, Any]]:
    """Return current-week late-notice callouts for approver visibility."""
    if not supabase_callouts_enabled():
        return []
    sb = get_supabase()
    resp = with_retry(
        lambda: sb.table("callouts")
        .select("event_date,caller_name,campus,reason,shift_start_at,shift_end_at,submitted_at,duration_hours,notice_hours")
        .gte("event_date", str(week_start))
        .lte("event_date", str(week_end))
        .execute()
    )
    rows: list[dict[str, Any]] = getattr(resp, "data", None) or []
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        row["duration_hours"] = _coerce_duration_hours(row)
        row["notice_hours"] = _coerce_notice_hours(row)
        row["late_notice_rule"] = _late_notice_rule(row)
        if not row["late_notice_rule"]:
            continue
        out.append(row)

    out.sort(
        key=lambda row: (
            str(row.get("event_date") or ""),
            str(row.get("shift_start_at") or ""),
            str(row.get("caller_name") or ""),
        ),
        reverse=True,
    )
    return out[: max(0, int(limit))]


def sum_callout_hours_for_week(*, caller_name: str, week_start: date, week_end: date) -> float:
    """Sum duration_hours for a caller within [week_start, week_end] (inclusive)."""
    total = 0.0
    for r in list_callouts_for_week(caller_name=caller_name, week_start=week_start, week_end=week_end):
        try:
            total += float(r.get("duration_hours") or 0.0)
        except Exception:
            pass
    return float(total)

def list_open_summer_callouts(*, today: date | None = None) -> list[dict[str, Any]]:
    """
    Return future/current SUMMER callouts that are not fully covered by pickups.

    A summer callout is considered covered when pickups for the same target,
    date, and overlapping time cover the whole callout window.
    """
    if not supabase_callouts_enabled():
        return []

    today = today or date.today()
    sb = get_supabase()

    callout_resp = with_retry(
        lambda: sb.table("callouts")
        .select("*")
        .eq("campus", "SUMMER")
        .gte("event_date", str(today))
        .execute()
    )
    pickup_resp = with_retry(
        lambda: sb.table("pickups")
        .select("*")
        .eq("campus", "SUMMER")
        .gte("event_date", str(today))
        .execute()
    )

    callouts: list[dict[str, Any]] = list(getattr(callout_resp, "data", None) or [])
    pickups: list[dict[str, Any]] = list(getattr(pickup_resp, "data", None) or [])

    def _window(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
        sdt = _parse_iso_dt(row.get("shift_start_at"))
        edt = _parse_iso_dt(row.get("shift_end_at"))
        if not (sdt and edt):
            return None
        if edt <= sdt:
            edt = edt + timedelta(days=1)
        return sdt, edt

    open_rows: list[dict[str, Any]] = []

    for c in callouts:
        c_window = _window(c)
        if not c_window:
            continue

        c_start, c_end = c_window
        total_mins = max(0.0, (c_end - c_start).total_seconds() / 60.0)
        if total_mins <= 0:
            continue

        caller_key = name_key(str(c.get("caller_name", "")))
        event_date = str(c.get("event_date", ""))

        covered_mins = 0.0

        for p in pickups:
            if str(p.get("event_date", "")) != event_date:
                continue

            if name_key(str(p.get("target_name", ""))) != caller_key:
                continue

            p_window = _window(p)
            if not p_window:
                continue

            p_start, p_end = p_window

            overlap_start = max(c_start, p_start)
            overlap_end = min(c_end, p_end)
            if overlap_end > overlap_start:
                covered_mins += (overlap_end - overlap_start).total_seconds() / 60.0

        # Leave a tiny tolerance for rounding.
        if covered_mins + 0.5 < total_mins:
            row = dict(c)
            row["covered_minutes"] = covered_mins
            row["uncovered_minutes"] = max(0.0, total_mins - covered_mins)
            open_rows.append(row)

    open_rows.sort(
        key=lambda r: (
            str(r.get("event_date", "")),
            str(r.get("shift_start_at", "")),
            str(r.get("caller_name", "")),
        )
    )
    return open_rows
