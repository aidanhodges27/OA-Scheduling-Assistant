"""Main Streamlit page.

`app.py` is intentionally tiny and calls `run()` from here.
"""

from __future__ import annotations

import re
import json
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

from .. import config
from ..actions import chat_add, chat_callout, chat_remove
from ..core import hours
from ..core import labor_rules
from ..core import schedule as schedule_mod
from ..core import utils
from ..core import week_range as week_range_mod
from ..integrations.gspread_io import open_spreadsheet
from ..services.audit_log import append_audit
from ..services.approvals import get_request as get_approval_request
from ..services.approvals import read_requests as read_approval_requests
from ..services.approvals import set_status as set_approval_status
from ..services.approvals import submit_request as submit_approval_request
from ..services.roster import load_roster
from ..services import callouts_db, pickups_db
from . import schedule_query, ui_peek
from . import pickup_scan
from .recovery import clear_hard_caches, maybe_show_recovery_popup
from .footer import render_global_footer
from .vibrant_theme import apply_vibrant_theme
from .availability import (
    campus_kind,
    clear_caches as clear_availability_caches,
    enumerate_exact_length_windows,
    list_tabs_for_sidebar,
    render_availability_expander,
    render_global_availability,
    weekday_filter,
    cached_available_ranges_for_day,
)


Schedule = schedule_mod.Schedule
name_key = utils.name_key
fmt_time = utils.fmt_time
compute_hours_fast = hours.compute_hours_fast
invalidate_hours_caches = getattr(hours, "invalidate_hours_caches", lambda: None)


# ----------------------------- Approvals meta -----------------------------
# To make approvals robust across weeks and tab renames, we embed stable
# metadata into the request Details field at submission time.
#
# Format (prefix):
#   META={"campus_key":"MC","sheet_title":"MC 1/25 - 1/31","sheet_gid":12345,"week_start":"1/25","week_end":"1/31"} | <rest>

_META_RE = re.compile(r"\bMETA=(\{.*?\})\s*(?:\||$)", flags=re.I | re.S)
_MMDD_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")


def _la_today() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def _week_bounds_la(ref: date | None = None) -> tuple[date, date]:
    """Return (sunday, saturday) for the LA-local *calendar* week containing ref."""
    d = ref or _la_today()
    # Python weekday(): Monday=0 ... Sunday=6.
    # We want weeks that start on Sunday.
    sunday = d - timedelta(days=(d.weekday() + 1) % 7)
    saturday = sunday + timedelta(days=6)
    return sunday, saturday


def _should_color_schedule_now(*, campus_key: str, event_d: date) -> bool:
    """Whether a schedule-grid write should happen immediately.

    UNH/MC keep the existing current-week-only coloring rule.
    On-Call tabs are week-specific, so future visible week tabs should color now.
    """
    campus = str(campus_key or "").strip().upper()
    cw0, cw1 = _week_bounds_la()
    in_current_week = bool(cw0 <= event_d <= cw1)
    return bool(in_current_week or campus == "ONCALL")


def _worksheet_week_bounds(ss, campus_title: str) -> tuple[date, date] | None:
    """Best-effort (week_start, week_end) for a worksheet.

    For UNH/MC we infer from the sheet's visible header area.
    For On-Call tabs, the title usually includes the week range.
    """
    try:
        ws = ss.worksheet(campus_title)
    except Exception:
        return None
    try:
        return week_range_mod.week_range_from_worksheet(ws, today=_la_today())
    except Exception:
        return None


def _date_for_weekday_in_sheet(ss, campus_title: str, day_canon: str) -> date | None:
    wr = _worksheet_week_bounds(ss, campus_title)
    if not wr:
        return None
    ws, we = wr
    return week_range_mod.date_for_weekday(ws, we, day_canon)


def _date_for_weekday_in_current_la_week(day_canon: str, *, ref: date | None = None) -> date | None:
    ws, we = _week_bounds_la(ref)
    return week_range_mod.date_for_weekday(ws, we, day_canon)


def _callout_event_date_for_sheet(ss, campus_title: str, day_canon: str) -> date | None:
    """Best-effort callout event date that stays aligned with the selected day."""
    d = _date_for_weekday_in_sheet(ss, campus_title, day_canon)
    if d:
        return d
    return _date_for_weekday_in_current_la_week(day_canon)


def _date_matches_day_canon(d: date | None, day_canon: str) -> bool:
    if not isinstance(d, date):
        return False
    want = utils.normalize_day(day_canon)
    got = utils.normalize_day(d.strftime("%A"))
    return bool(want and got and want == got)


_DETAIL_KV_RE = re.compile(r"\b([a-z_]+)\s*=\s*([^|]+)", flags=re.I)


def _parse_details_kv(details_rest: str) -> dict[str, str]:
    """Parse machine-readable key=value tokens from approval details."""
    out: dict[str, str] = {}
    for m in _DETAIL_KV_RE.finditer(str(details_rest or "")):
        k = m.group(1).strip().lower()
        v = m.group(2).strip()
        out[k] = v
    return out


def _format_request_details_for_display(details: str) -> str:
    """Humanize request details for approver/requester UI."""
    _meta, details_rest = _extract_details_meta(details)
    lines: list[str] = []

    for raw_part in [p.strip() for p in str(details_rest or "").split("|") if p.strip()]:
        m = re.match(r"^([a-z_]+)\s*[:=]\s*(.+)$", raw_part, flags=re.I)
        if not m:
            lines.append(raw_part)
            continue

        key = m.group(1).strip().lower()
        value = m.group(2).strip()

        if key == "target":
            lines.append(f"Target: {value}")
        elif key == "date":
            lines.append(f"Date: {value}")
        elif key == "overtime":
            lines.append("Overtime requested: Yes" if value.lower() == "yes" else f"Overtime requested: {value}")
        elif key == "day_after":
            lines.append(f"Total hours for the day: {value}")
        elif key == "week_after":
            lines.append(f"Total hours for the week: {value}")
        elif key == "reason":
            lines.append(f"Reason: {value}")
        elif key == "note":
            lines.append(f"Note: {value}")
        else:
            lines.append(f"{key.replace('_', ' ').title()}: {value}")

    return "\n".join(lines).strip()


def _format_callout_reason(reason: str) -> str:
    raw = str(reason or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        head, tail = raw.split(":", 1)
        head = head.strip().title()
        tail = tail.strip()
        return f"{head}: {tail}" if tail else head
    return raw.title()


def _ensure_la_timestamptz(ts: str) -> str:
    """Ensure an ISO timestamp string has timezone info (America/Los_Angeles).

    Approvals.created_at may be stored without offset. For Supabase timestamptz,
    we normalize to LA timezone.
    """
    s = str(ts or "").strip()
    if not s:
        return datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
    # If it already has Z / offset, keep it.
    if re.search(r"(Z|[+-]\d\d:\d\d)$", s):
        return s
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        return dt.isoformat(timespec="seconds")
    except Exception:
        return datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")


def _combine_date_time_la(d: date, t) -> datetime:
    """Combine a date with a time/time-like object into an aware LA datetime."""
    hh = getattr(t, "hour", 0)
    mm = getattr(t, "minute", 0)
    return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=ZoneInfo("America/Los_Angeles"))


def _duration_hours_between(start_at: datetime, end_at: datetime) -> float:
    if end_at <= start_at:
        end_at = end_at + timedelta(days=1)
    return max(0.0, float((end_at - start_at).total_seconds() / 3600.0))


def _oncall_event_date(sheet_title: str, day_canon: str) -> date | None:
    """Given an On-Call sheet title and canonical weekday, derive the calendar date."""
    ws = _oncall_week_start_from_title(sheet_title)
    if not ws:
        return None
    # Find the first date in the 7-day window that matches the requested weekday.
    want = utils.normalize_day(day_canon)
    for i in range(7):
        d = ws + timedelta(days=i)
        if utils.normalize_day(d.strftime("%A")) == want:
            return d
    return None


def _oncall_week_start_from_title(sheet_title: str) -> date | None:
    """Infer the week start date for an On-Call tab.

    Your templates use multiple title formats:
      - "On Call 1/25 - 1/31" (slashes)
      - "On Call 28 - 214" (compact M/D tokens; 28 => 2/8, 214 => 2/14)
      - "On Call 928-104" (cross-month compact; 9/28 - 10/4)

    We delegate parsing to oa_app.core.week_range which also handles year
    rollover near Jan/Dec.
    """
    try:
        from ..core import week_range as week_range_mod

        today = _la_today()
        wr = week_range_mod.week_range_from_title(str(sheet_title or ""), today=today)
        if wr:
            return wr[0]
    except Exception:
        pass
    return None


@st.cache_data(ttl=90, show_spinner=False)
def _cached_weekly_supabase_adjustments(user_name: str, week_start: str, week_end: str) -> dict:
    """Return {callout_hours, pickup_hours} for the given user/week.

    Cached because Supabase queries can be chatty under concurrency.
    """
    try:
        ws = date.fromisoformat(week_start)
        we = date.fromisoformat(week_end)
    except Exception:
        ws, we = _week_bounds_la()
    callout_h = 0.0
    pickup_h = 0.0
    try:
        callout_h = callouts_db.sum_callout_hours_for_week(caller_name=user_name, week_start=ws, week_end=we)
    except Exception:
        callout_h = 0.0
    try:
        pickup_h = pickups_db.sum_pickup_hours_for_week(picker_name=user_name, week_start=ws, week_end=we)
    except Exception:
        pickup_h = 0.0
    return {"callout_hours": float(callout_h), "pickup_hours": float(pickup_h)}


@st.cache_data(ttl=30, show_spinner=False)
def _cached_late_notice_callouts(week_start: str, week_end: str) -> list[dict]:
    """Current-week late-notice callouts for approver visibility."""
    try:
        ws = date.fromisoformat(week_start)
        we = date.fromisoformat(week_end)
    except Exception:
        ws, we = _week_bounds_la()
    try:
        return callouts_db.list_late_notice_callouts(week_start=ws, week_end=we, limit=200)
    except Exception:
        return []


def _extract_details_meta(details: str) -> tuple[dict, str]:
    """Return (meta, rest_details)."""
    s = str(details or "").strip()
    m = _META_RE.search(s)
    if not m:
        return {}, s
    try:
        meta = json.loads(m.group(1)) if m.group(1) else {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    # Remove the first META=... occurrence.
    rest = (s[: m.start()] + s[m.end() :]).strip()
    if rest.startswith("|"):
        rest = rest[1:].strip()
    return meta, rest


def _sheet_gid_for_title(schedule_global: Schedule, title: str) -> int | None:
    """Best-effort lookup of a worksheet gid without extra API calls."""
    try:
        schedule_global._load_ws_map()  # type: ignore[attr-defined]
        ws_map = getattr(schedule_global, "_ws_map", None) or {}
        ws = ws_map.get(title)
        gid = getattr(ws, "id", None)
        if gid is None:
            gid = getattr(ws, "_properties", {}).get("sheetId") if ws is not None else None
        return int(gid) if gid is not None else None
    except Exception:
        return None


def _attach_details_meta(*, details: str, campus_key: str, sheet_title: str, sheet_gid: int | None) -> str:
    meta: dict = {
        "campus_key": str(campus_key or "").strip().upper(),
        "sheet_title": str(sheet_title or "").strip(),
    }
    if sheet_gid is not None:
        meta["sheet_gid"] = int(sheet_gid)

    # Capture week markers if present in the title (helps matching if title changes).
    mmdd = _MMDD_RE.findall(meta["sheet_title"])
    if len(mmdd) >= 2:
        meta["week_start"] = f"{mmdd[0][0]}/{mmdd[0][1]}"
        meta["week_end"] = f"{mmdd[1][0]}/{mmdd[1][1]}"

    prefix = "META=" + json.dumps(meta, separators=(",", ":"), ensure_ascii=False)
    rest = (details or "").strip()
    return f"{prefix} | {rest}" if rest else prefix


def _resolve_ws_title_from_meta(ss, schedule_global: Schedule, *, campus_fallback: str, details: str) -> str:
    """Resolve the worksheet title to execute a request against.

    Priority:
      1) sheet_gid from META (survives tab rename)
      2) sheet_title from META
      3) campus_fallback (may be a campus key or a tab-ish string)
    """
    meta, _ = _extract_details_meta(details)

    # 1) gid
    gid = meta.get("sheet_gid")
    if gid is not None:
        try:
            gid_int = int(gid)
            # Prefer gspread helper if available
            if hasattr(ss, "get_worksheet_by_id"):
                ws = ss.get_worksheet_by_id(gid_int)
                if ws is not None and getattr(ws, "title", None):
                    return str(ws.title)
            # Fallback: search loaded ws map
            schedule_global._load_ws_map()  # type: ignore[attr-defined]
            ws_map = getattr(schedule_global, "_ws_map", None) or {}
            for t, w in ws_map.items():
                wid = getattr(w, "id", None)
                if wid is None:
                    wid = getattr(w, "_properties", {}).get("sheetId")
                if wid is not None and int(wid) == gid_int:
                    return str(t)
        except Exception:
            pass

    # 2) exact title
    sheet_title = str(meta.get("sheet_title") or "").strip()
    if sheet_title:
        try:
            # Only accept if it still exists.
            titles = [w.title for w in ss.worksheets()]
            if sheet_title in titles:
                return sheet_title
        except Exception:
            # If we can't list titles due to quota, still try using it.
            return sheet_title

    # 3) campus key + week markers (best-effort match)
    campus_key = str(meta.get("campus_key") or "").strip().upper() or str(campus_fallback or "").strip()
    wk_s = str(meta.get("week_start") or "").strip()
    wk_e = str(meta.get("week_end") or "").strip()
    if campus_key and (wk_s and wk_e):
        try:
            titles = [w.title for w in ss.worksheets()]
            for t in titles:
                tl = t.lower()
                if campus_key.lower() in tl and wk_s in t and wk_e in t:
                    return t
        except Exception:
            pass

    # Final fallback: existing resolver (handles aliases like "UHall" / "MC")
    campus_ws_title, _ = chat_add._resolve_campus_title(ss, campus_fallback, None)
    return campus_ws_title


def _pickup_windows_from_cached(rows: list[dict]) -> list[pickup_scan.PickupWindow]:
    out: list[pickup_scan.PickupWindow] = []
    for r in rows or []:
        try:
            out.append(
                pickup_scan.PickupWindow(
                    campus_title=str(r.get("campus_title", "")),
                    kind=str(r.get("kind", "")),
                    day_canon=str(r.get("day_canon", "")),
                    target_name=str(r.get("target_name", "")),
                    start=datetime.fromisoformat(str(r.get("start"))),
                    end=datetime.fromisoformat(str(r.get("end"))),
                )
            )
        except Exception:
            continue
    # de-dup
    seen = set()
    uniq: list[pickup_scan.PickupWindow] = []
    for w in out:
        k = (w.campus_title, w.kind, w.day_canon, w.target_name, w.start, w.end)
        if k not in seen:
            seen.add(k)
            uniq.append(w)
    return uniq


def _approver_identity_key(canon_name: str) -> str | None:
    """Return the canonical approver identity key (name_key), or None.

    Approver identity is intentionally *loose* to support roster variants and
    short names.
    """

    nk = name_key(canon_name)

    # Canonical identities
    vraj = name_key("vraj patel")
    kat = name_key("kat brosvik")
    nile = name_key("nile bernal")
    andy = name_key("barth andrew")
    jaden = name_key("schutt jaden")

    aliases = {
        # Vraj
        vraj: vraj,
        # Kat (allow "Kat" prefix / roster variants)
        kat: kat,
        name_key("kat"): kat,
        # Nile
        nile: nile,
        # Andy / Andrew Barth
        andy: andy,
        name_key("andrew barth"): andy,
        name_key("andy"): andy,
        # Jaden / Jaden Schutt
        jaden: jaden,
        name_key("jaden schutt"): jaden,
        name_key("jaden"): jaden,
    }

    if nk in aliases:
        return aliases[nk]

    # Fallback: treat any "Kat ..." as Kat Brosvik for approver auth.
    if (canon_name or "").strip().lower().startswith("kat"):
        return kat

    return None


def _is_approver(canon_name: str) -> bool:
    """Return True if this user should see the approvals UI."""
    ident = _approver_identity_key(canon_name)
    if not ident:
        return False
    return ident in {
        name_key("vraj patel"),
        name_key("kat brosvik"),
        name_key("nile bernal"),
        name_key("barth andrew"),
        name_key("schutt jaden"),
    }


def _approver_unlocked(canon_name: str) -> bool:
    ident = _approver_identity_key(canon_name)
    if not ident:
        return False
    if not _is_approver(canon_name):
        return False
    return bool(st.session_state.get("APPROVER_AUTH")) and st.session_state.get("APPROVER_AUTH_FOR") == ident


def _strip_debug_blob(msg: str) -> str:
    if not msg:
        return msg
    return msg.split("\n\n--- DEBUG", 1)[0].strip()


def _parse_iso_dt(s: str) -> datetime:
    """Best-effort ISO datetime parser for approval rows."""
    try:
        return datetime.fromisoformat(str(s).strip())
    except Exception:
        return datetime(1970, 1, 1)


def _status_chip(status: str) -> str:
    s = (status or "").strip().upper()
    if s == "PENDING":
        return "🕓 PENDING"
    if s == "APPROVED":
        return "✅ APPROVED"
    if s == "REJECTED":
        return "❌ REJECTED"
    if s == "FAILED":
        return "⚠️ FAILED"
    return s or "—"


def _sort_requests_newest(rows: list[dict]) -> list[dict]:
    return sorted(rows or [], key=lambda r: _parse_iso_dt(r.get("Created", "")), reverse=True)


def _requests_for_user(rows: list[dict], canon_name: str) -> list[dict]:
    nk = name_key(canon_name)
    out = []
    for r in rows or []:
        if name_key(str(r.get("Requester", ""))) == nk:
            out.append(r)
    return out


def _versions_key(ss, extra_titles: list[str] | None = None):
    """Create a cache key that changes when relevant worksheets change.

    We key most read caches off a (title, version) tuple. Versions are bumped
    on writes and the batch_get cache is seeded, so reruns stay fast without
    clearing Streamlit caches.
    """
    ver = st.session_state.get("WS_VER", {}) or {}
    base_titles = schedule_query._open_three(ss) or []  # UNH, MC, On-Call
    titles = list(base_titles)
    if extra_titles:
        for t in extra_titles:
            if t and t not in titles:
                titles.append(t)
    return tuple((t, int(ver.get(t, 0))) for t in titles)


def _bust_hours_cache() -> None:
    """Increment a lightweight UI epoch.

    IMPORTANT: we do **not** clear Streamlit caches here.
    Writes already bump worksheet versions, and our read caches are seeded after
    writes, so clearing would throw away that work and make the app feel slow.
    """
    st.session_state["UI_EPOCH"] = st.session_state.get("UI_EPOCH", 0) + 1


def _flash(kind: str, msg: str) -> None:
    """Store a one-shot message to be rendered on the next rerun."""
    st.session_state["_FLASH"] = {"kind": kind, "msg": msg}


@st.cache_data(ttl=30, show_spinner=False)
def cached_user_schedule(ss_id: str, canon_name: str, epoch):
    ss = st.session_state.get("_SS_HANDLE_BY_ID", {}).get(ss_id)
    if not ss:
        return {}
    schedule_global = st.session_state.get("_SCHEDULE_GLOBAL")
    if not schedule_global:
        return {}
    sched = schedule_query.get_user_schedule(ss, schedule_global, canon_name)
    return sched or {}


@st.cache_data(ttl=30, show_spinner=False)
def cached_user_schedule_for_titles(
    ss_id: str,
    canon_name: str,
    unh_title: str | None,
    mc_title: str | None,
    oncall_title: str | None,
    epoch,
):
    ss = st.session_state.get("_SS_HANDLE_BY_ID", {}).get(ss_id)
    if not ss:
        return {}
    schedule_global = st.session_state.get("_SCHEDULE_GLOBAL")
    if not schedule_global:
        return {}
    sched = schedule_query.get_user_schedule_for_titles(
        ss,
        schedule_global,
        canon_name,
        unh_title=unh_title,
        mc_title=mc_title,
        oncall_title=oncall_title,
    )
    return sched or {}


@st.cache_data(ttl=30, show_spinner=False)
def cached_schedule_df(schedule_dict: dict, epoch):
    # epoch is only used to invalidate the cache after writes
    return schedule_query.build_schedule_dataframe(schedule_dict)


@st.cache_data(ttl=30, show_spinner=False)
def cached_people_working_now(ss_id: str, epoch, when_key: str):
    ss = st.session_state.get("_SS_HANDLE_BY_ID", {}).get(ss_id)
    if not ss:
        return {}
    try:
        when = datetime.fromisoformat(str(when_key))
    except Exception:
        when = None
    snapshot = schedule_query.get_people_working_now(ss, when=when) or {}
    return _annotate_working_now_snapshot(snapshot, when=when) or snapshot


@st.cache_data(ttl=30, show_spinner=False)
def _cached_working_now_coverage_rows(week_start: str, week_end: str) -> dict:
    try:
        ws = date.fromisoformat(str(week_start))
        we = date.fromisoformat(str(week_end))
    except Exception:
        ws, we = _week_bounds_la()

    out = {"callouts": [], "pickups": []}
    try:
        if callouts_db.supabase_callouts_enabled():
            out["callouts"] = callouts_db.list_callouts_in_range(week_start=ws, week_end=we) or []
    except Exception:
        out["callouts"] = []
    try:
        if pickups_db.supabase_pickups_enabled():
            out["pickups"] = pickups_db.list_pickups_in_range(week_start=ws, week_end=we) or []
    except Exception:
        out["pickups"] = []
    return out


@st.cache_data(ttl=5, show_spinner=False)
def cached_approval_table(ss_id: str, approvals_epoch: int, max_rows: int = 500):
    """Read approval requests with a short cache.

    approvals_epoch should change when the Pending Actions sheet changes.
    max_rows bounds the range read from Sheets (speed vs. history depth).
    """
    ss = st.session_state.get("_SS_HANDLE_BY_ID", {}).get(ss_id)
    if not ss:
        return []
    # Use the service helper (bounded range) for fast reads.
    return read_approval_requests(ss, max_rows=int(max_rows)) or []


@st.cache_data(ttl=5, show_spinner=False)
def _mins_for_day(user_sched, day_canon: str) -> int:
    _sum = getattr(schedule_query, "_sum_ranges_minutes")
    buckets = (user_sched or {}).get(day_canon, {}) or {}
    total = 0
    for k in ("UNH", "MC", "On-Call"):
        total += _sum(buckets.get(k, []))
    return total


def _format_pair_12h(s24: str, e24: str) -> str:
    sdt = datetime.strptime(s24, "%H:%M")
    edt = datetime.strptime(e24, "%H:%M")
    return f"{fmt_time(sdt)} – {fmt_time(edt)}"


def _coerce_la_datetime(when: datetime | None = None) -> datetime:
    if when is None:
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    if when.tzinfo is None:
        return when.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    return when.astimezone(ZoneInfo("America/Los_Angeles"))


def _parse_iso_datetime_la(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    return dt.astimezone(ZoneInfo("America/Los_Angeles"))


def _interval_contains_moment(start_at: datetime, end_at: datetime, moment: datetime) -> bool:
    end_norm = end_at if end_at > start_at else end_at + timedelta(days=1)
    return bool(start_at <= moment < end_norm)


def _intervals_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    end_a_norm = end_a if end_a > start_a else end_a + timedelta(days=1)
    end_b_norm = end_b if end_b > start_b else end_b + timedelta(days=1)
    return bool(start_a < end_b_norm and start_b < end_a_norm)


def _working_now_entry_window(entry: dict, when: datetime) -> tuple[datetime, datetime] | None:
    start_txt = str(entry.get("start") or "").strip()
    end_txt = str(entry.get("end") or "").strip()
    start_dt = schedule_query._parse_time_cell(start_txt)
    end_dt = schedule_query._parse_time_cell(end_txt)
    if not (start_dt and end_dt):
        return None

    local_when = _coerce_la_datetime(when)
    start_at = datetime(
        local_when.year,
        local_when.month,
        local_when.day,
        start_dt.hour,
        start_dt.minute,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    )
    end_at = datetime(
        local_when.year,
        local_when.month,
        local_when.day,
        end_dt.hour,
        end_dt.minute,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    )
    if end_at <= start_at:
        end_at = end_at + timedelta(days=1)
    return start_at, end_at


def _coverage_campus_key(source_or_campus: str) -> str:
    return utils.normalize_campus(source_or_campus, source_or_campus)


def _dedupe_display_names(names: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for raw in names or []:
        name = str(raw or "").strip()
        nk = name_key(name)
        if not name or nk in seen:
            continue
        seen.add(nk)
        out.append(name)
    return out


def _annotate_working_now_snapshot(
    snapshot: dict,
    *,
    when: datetime | None = None,
    callouts_rows: list[dict] | None = None,
    pickups_rows: list[dict] | None = None,
) -> dict:
    local_when = _coerce_la_datetime(when)
    ws, we = _week_bounds_la(local_when.date())

    if callouts_rows is None or pickups_rows is None:
        coverage = _cached_working_now_coverage_rows(ws.isoformat(), we.isoformat()) or {}
        if callouts_rows is None:
            callouts_rows = list(coverage.get("callouts") or [])
        if pickups_rows is None:
            pickups_rows = list(coverage.get("pickups") or [])

    active_callouts: dict[tuple[str, str], list[dict]] = {}
    for row in callouts_rows or []:
        start_at = _parse_iso_datetime_la(row.get("shift_start_at"))
        end_at = _parse_iso_datetime_la(row.get("shift_end_at"))
        if not (start_at and end_at):
            continue
        if not _interval_contains_moment(start_at, end_at, local_when):
            continue
        campus_key = _coverage_campus_key(str(row.get("campus") or ""))
        caller_key = name_key(str(row.get("caller_name") or ""))
        if not (campus_key and caller_key):
            continue
        active_callouts.setdefault((campus_key, caller_key), []).append(dict(row))

    active_pickups: dict[tuple[str, str], list[dict]] = {}
    for row in pickups_rows or []:
        start_at = _parse_iso_datetime_la(row.get("shift_start_at"))
        end_at = _parse_iso_datetime_la(row.get("shift_end_at"))
        if not (start_at and end_at):
            continue
        if not _interval_contains_moment(start_at, end_at, local_when):
            continue
        campus_key = _coverage_campus_key(str(row.get("campus") or ""))
        target_key = name_key(str(row.get("target_name") or ""))
        if not (campus_key and target_key):
            continue
        active_pickups.setdefault((campus_key, target_key), []).append(dict(row))

    out = dict(snapshot or {})
    annotated_entries: list[dict] = []
    for entry in list(snapshot.get("entries") or []):
        row = dict(entry)
        source = str(row.get("source") or "")
        original_name = str(row.get("name") or "").strip()
        campus_key = _coverage_campus_key(source)
        target_key = name_key(original_name)
        row["display_name"] = original_name
        row["display_role"] = str(row.get("role") or "").strip()
        row["status"] = "Scheduled"
        row["status_kind"] = "scheduled"

        entry_window = _working_now_entry_window(row, local_when)
        callouts = list(active_callouts.get((campus_key, target_key), []))
        pickups = list(active_pickups.get((campus_key, target_key), []))
        if entry_window:
            start_at, end_at = entry_window
            callouts = [
                c for c in callouts
                if _intervals_overlap(
                    start_at,
                    end_at,
                    _parse_iso_datetime_la(c.get("shift_start_at")) or start_at,
                    _parse_iso_datetime_la(c.get("shift_end_at")) or end_at,
                )
            ]
            pickups = [
                p for p in pickups
                if _intervals_overlap(
                    start_at,
                    end_at,
                    _parse_iso_datetime_la(p.get("shift_start_at")) or start_at,
                    _parse_iso_datetime_la(p.get("shift_end_at")) or end_at,
                )
            ]

        if pickups:
            coverers = _dedupe_display_names([str(p.get("picker_name") or "").strip() for p in pickups])
            if coverers:
                row["display_name"] = ", ".join(coverers)
                row["display_role"] = ""
                row["status"] = f"Covering {original_name}"
                row["status_kind"] = "covered"
                row["covered_by"] = coverers
        elif callouts:
            row["status"] = "No cover"
            row["status_kind"] = "no_cover"

        annotated_entries.append(row)

    out["entries"] = annotated_entries
    return out


def _render_people_working_now_panel(ss, epoch_key) -> None:
    now_la = datetime.now(ZoneInfo("America/Los_Angeles"))
    when_key = now_la.strftime("%Y-%m-%dT%H:%M")
    snapshot = cached_people_working_now(ss.id, epoch_key, when_key) or {}

    display_mode = str(snapshot.get("display_mode") or "campus")
    sources = list(snapshot.get("sources") or ([] if display_mode == "oncall" else ["UNH", "MC"]))
    entries = list(snapshot.get("entries") or [])

    grouped: dict[str, list[dict]] = {src: [] for src in sources}
    for row in entries:
        grouped.setdefault(str(row.get("source") or "Unknown"), []).append(row)

    mode_label = "On-Call" if display_mode == "oncall" else "UNH + MC"
    with st.expander("Who's Working Now", expanded=True):
        st.caption(f"{snapshot.get('when_label', when_key)} (Los Angeles). Showing {mode_label} coverage.")
        st.caption("Saturday/Sunday uses On-Call. Monday-Friday switches to On-Call from 7:00 PM to 12:00 AM. Red means No cover; orange shows who is covering.")

        if not sources:
            st.info("No schedule sources are available right now.")
            return

        cols = st.columns(len(sources))
        for idx, source in enumerate(sources):
            with cols[idx]:
                st.markdown(f"**{source}**")
                rows = grouped.get(source, [])
                if not rows:
                    st.caption("No one is on shift right now.")
                    continue

                table = []
                for row in rows:
                    person = str(row.get("display_name") or row.get("name") or "").strip()
                    role = str(row.get("display_role") or row.get("role") or "").strip()
                    if role:
                        person = f"{person} ({role})"
                    table.append(
                        {
                            "Person": person,
                            "Shift": f"{row.get('start', '')} - {row.get('end', '')}",
                            "Status": str(row.get("status") or ""),
                        }
                    )
                st.dataframe(table, hide_index=True, use_container_width=True)


def _iter_pairs(seq):
    """Yield (start,end) from a possibly messy list.

    Accepts items like:
      - (s,e) or (s,e,...) tuples/lists
      - "HH:MM - HH:MM" / "7:00 AM – 9:00 AM" strings
      - {"start":..., "end":...} dicts
    """
    if not seq:
        return []
    out = []
    for item in seq:
        if item is None:
            continue
        s = e = None
        if isinstance(item, dict):
            s = item.get("start") or item.get("s")
            e = item.get("end") or item.get("e")
        elif isinstance(item, (list, tuple)):
            if len(item) >= 2:
                s, e = item[0], item[1]
        elif isinstance(item, str):
            txt = item.strip()
            mm = re.match(r"^\s*([^–-]+?)\s*[-–]\s*([^–-]+?)\s*$", txt)
            if mm:
                s, e = mm.group(1).strip(), mm.group(2).strip()
        if s is None or e is None:
            continue
        out.append((str(s).strip(), str(e).strip()))
    return out


def _parse_12h_time(s: str) -> datetime:
    """Parse a 12h time string from schedule data.

    Accepts common variants like "7:00 AM", "7 AM", "7:00AM".
    Returns a datetime (date is arbitrary).
    """
    t = (s or "").strip().upper().replace(".", "")
    t = re.sub(r"\s+", " ", t)
    # Ensure "7AM" / "7:00AM" have a space before AM/PM
    t = re.sub(r"(\d)(AM|PM)$", r"\1 \2", t)
    t = re.sub(r"(\d:\d\d)(AM|PM)$", r"\1 \2", t)
    # Ensure minutes exist ("7 AM" -> "7:00 AM")
    if re.fullmatch(r"\d{1,2} (AM|PM)", t):
        t = t.replace(" ", ":00 ")
    return datetime.strptime(t, "%I:%M %p")


def _enumerate_subwindows_within_shift(s12: str, e12: str, need_minutes: int) -> list[tuple[datetime, datetime]]:
    """Enumerate contiguous sub-windows within a scheduled shift.

    Windows are produced in 30-minute steps, matching the add-flow behavior.
    Handles overnight shifts by rolling the end into the next day when needed.
    """
    if need_minutes <= 0:
        return []
    sdt = _parse_12h_time(s12)
    edt = _parse_12h_time(e12)
    if edt <= sdt:
        edt = edt + timedelta(days=1)
    out: list[tuple[datetime, datetime]] = []
    cur = sdt
    step = timedelta(minutes=30)
    dur = timedelta(minutes=int(need_minutes))
    while cur + dur <= edt:
        out.append((cur, cur + dur))
        cur += step
    return out


_WEEK_TOKEN_RE = re.compile(r"(\d{1,2}/\d{1,2})")


def _week_token_from_title(title: str) -> str | None:
    """Extract a lightweight week token like '1/25-1/31' from a tab title."""
    if not title:
        return None
    parts = _WEEK_TOKEN_RE.findall(title)
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return None


def _token_from_bounds(wr: tuple[date, date] | None) -> str | None:
    """Convert (week_start, week_end) to a token like '1/25-1/31'."""
    if not wr:
        return None
    try:
        ws, we = wr
        return f"{ws.month}/{ws.day}-{we.month}/{we.day}"
    except Exception:
        return None


def _most_recent_title_by_week(cands: list[str]) -> str | None:
    """Pick the most recent week-like title by parsing its week range.

    IMPORTANT: We do **not** rely on spreadsheet tab order, because many
    templates keep older weeks at the bottom or interleave non-week tabs.
    """
    if not cands:
        return None
    today = _la_today()
    best_t = None
    best_ws = None
    for t in cands:
        try:
            wr = week_range_mod.week_range_from_title(t, today=today)
        except Exception:
            wr = None
        if not wr:
            continue
        ws, _we = wr
        if best_ws is None or ws > best_ws:
            best_ws = ws
            best_t = t
    return best_t or cands[-1]


def _resolve_week_titles(all_titles: list[str], seed_title: str, *, ss=None) -> dict[str, str | None]:
    """Find UNH/MC/ONCALL titles that match the same week as `seed_title`.

    We primarily match by a lightweight title token (e.g. '1/25-1/31'). If the
    seed title does not contain a token (common for rolling tabs like
    'UNH (OA and GOAs)'), we *infer* the token by scanning the worksheet header
    for a week range.
    """
    today = _la_today()
    # Prefer an actual week range (more reliable than token parsing).
    seed_wr = None
    try:
        seed_wr = week_range_mod.week_range_from_title(seed_title, today=today)
    except Exception:
        seed_wr = None
    if not seed_wr and ss is not None:
        try:
            seed_wr = _worksheet_week_bounds(ss, seed_title)
        except Exception:
            seed_wr = None

    token = _week_token_from_title(seed_title) or _token_from_bounds(seed_wr)

    seed_kind = campus_kind(seed_title)

    def _pick(kind: str) -> str | None:
        cands = [t for t in all_titles if campus_kind(t) == kind]
        # On-Call General is not a week-specific sheet; never use it for week matching.
        if kind == "ONCALL":
            cands = [t for t in cands if "general" not in (t or "").lower()]
        if not cands:
            return None

        # If the seed itself is of this kind, prefer it (it is the source tab).
        if kind == seed_kind and seed_title in cands:
            return seed_title

        # Prefer exact week-range match across tabs.
        if seed_wr:
            for t in cands:
                try:
                    wr = week_range_mod.week_range_from_title(t, today=today)
                except Exception:
                    wr = None
                if wr and wr == seed_wr:
                    return t

        # Fall back to lightweight title token match (works for M/D - M/D titles).
        if token:
            for t in cands:
                if _week_token_from_title(t) == token:
                    return t

        # If this workbook uses rolling tabs (e.g., "MC (OA and GOAs)"), prefer them.
        if kind in {"UNH", "MC"}:
            rolling = []
            for t in cands:
                try:
                    if not week_range_mod.week_range_from_title(t, today=today):
                        rolling.append(t)
                except Exception:
                    rolling.append(t)
            if rolling:
                # Prefer the configured base title when present.
                for pref in getattr(config, "OA_SCHEDULE_SHEETS", []) or []:
                    for t in rolling:
                        if t.strip().lower() == str(pref).strip().lower():
                            return t
                return rolling[0]

        # Otherwise, pick the most recent week-like title (not tab order).
        return _most_recent_title_by_week(cands)

    return {"UNH": _pick("UNH"), "MC": _pick("MC"), "ONCALL": _pick("ONCALL")}


_OVERTIME_MARKER_RE = re.compile(r"\bovertime\s*[:=]\s*yes\b", re.IGNORECASE)


def _is_overtime_request(req: dict) -> bool:
    details = str(req.get("Details", "") or "")
    if _OVERTIME_MARKER_RE.search(details):
        return True
    # Backward/alternate marker
    return "[overtime]" in details.lower()


def _append_my_pickups_into_sched(
    user_sched_all: dict,
    approvals_rows: list[dict],
    *,
    requester: str,
    week_titles: set[str],
    include_statuses: set[str] | None = None,
    ss=None,
    week_bounds: tuple[date, date] | None = None,
) -> dict:
    """Overlay this requester's pickup requests into the schedule dict.

    `week_bounds` is the authoritative week filter when available. This avoids
    pulling old pickup requests from prior weeks on rolling UNH/MC tabs.
    """
    include_statuses = include_statuses or {"PENDING", "APPROVED"}
    out = {d: {k: list(v) for k, v in (buckets or {}).items()} for d, buckets in (user_sched_all or {}).items()}
    for d in out.values():
        for k in ("UNH", "MC", "On-Call"):
            d.setdefault(k, [])

    want_req = name_key(requester)

    for r in approvals_rows or []:
        if str(r.get("Action", "") or "").strip().lower() != "pickup":
            continue
        status = str(r.get("Status", "") or "").strip().upper()
        if status not in include_statuses:
            continue
        if name_key(str(r.get("Requester", "") or "")) != want_req:
            continue
        campus = str(r.get("Campus", "") or "").strip()
        meta, _rest = _extract_details_meta(str(r.get("Details", "") or ""))
        campus_title = str(meta.get("sheet_title") or "").strip() or campus
        if week_titles and campus_title not in week_titles:
            continue

        event_d = _approval_row_event_date(ss, r) if ss is not None else None
        if week_bounds and event_d and not (week_bounds[0] <= event_d <= week_bounds[1]):
            continue

        day_raw = str(r.get("Day", "") or "")
        day_canon = re.sub(r"[^a-z]", "", day_raw.strip().lower())
        if day_canon not in out:
            continue

        start = str(r.get("Start", "") or "").strip()
        end = str(r.get("End", "") or "").strip()
        if not start or not end:
            continue

        kind = campus_kind(str(meta.get("campus_key") or "") or campus_title)
        bucket = "On-Call" if kind == "ONCALL" else kind
        out[day_canon].setdefault(bucket, []).append((start, end))

    return out


def _sum_minutes_sched(user_sched_all: dict) -> tuple[int, dict[str, int]]:
    """Return (week_total_minutes, per_day_minutes)."""
    day_totals: dict[str, int] = {}
    week_total = 0
    for day_canon, buckets in (user_sched_all or {}).items():
        mins = 0
        for k in ("UNH", "MC", "On-Call"):
            for s12, e12 in _iter_pairs((buckets or {}).get(k, []) or []):
                try:
                    sd = _parse_12h_time(s12)
                    ed = _parse_12h_time(e12)
                except Exception:
                    continue
                if ed <= sd:
                    ed = ed + timedelta(days=1)
                mins += int((ed - sd).total_seconds() // 60)
        day_totals[day_canon] = mins
        week_total += mins
    return week_total, day_totals


def _minutes_from_hours(value) -> int:
    try:
        return int(round(float(value or 0.0) * 60.0))
    except Exception:
        return 0


def _request_week_bounds(ss, week_titles_map: dict[str, str | None], seed_title: str) -> tuple[date, date]:
    """Best-effort week bounds for the pickup being validated."""
    for cand in [seed_title, week_titles_map.get("ONCALL"), week_titles_map.get("UNH"), week_titles_map.get("MC")]:
        if not cand:
            continue
        try:
            wr = week_range_mod.week_range_from_title(str(cand), today=_la_today())
        except Exception:
            wr = None
        if not wr:
            try:
                wr = _worksheet_week_bounds(ss, str(cand))
            except Exception:
                wr = None
        if wr:
            return wr
    return _week_bounds_la()


def _approval_created_at_date(req: dict) -> date | None:
    s = str(req.get("Created", "") or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        return dt.astimezone(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return None


def _approval_created_week_fallback(req: dict, day_canon: str) -> date | None:
    created_d = _approval_created_at_date(req)
    if not created_d:
        return None
    try:
        ws, we = _week_bounds_la(created_d)
        return week_range_mod.date_for_weekday(ws, we, day_canon)
    except Exception:
        return None


def _approval_meta_week_bounds(meta: dict, *, ref_date: date | None = None) -> tuple[date, date] | None:
    wk_start = str((meta or {}).get("week_start") or "").strip()
    wk_end = str((meta or {}).get("week_end") or "").strip()
    if not (wk_start and wk_end):
        return None
    try:
        return week_range_mod.week_range_from_text(
            f"{wk_start} - {wk_end}",
            today=ref_date or _la_today(),
        )
    except Exception:
        return None


def _approval_row_event_date(ss, req: dict) -> date | None:
    """Best-effort event date for an approval row.

    Used to keep validation week math from counting old requests that happen to
    share the same rolling sheet title. For rolling UNH/MC tabs, never project
    an old approval onto the *current* worksheet week unless the row carries an
    explicit shift date or week marker.
    """
    details = str(req.get("Details", "") or "")
    meta, details_rest = _extract_details_meta(details)
    kv = _parse_details_kv(details_rest)
    ds = (kv.get("date") or "").strip()
    if ds:
        try:
            return date.fromisoformat(ds)
        except Exception:
            pass

    day_raw = str(req.get("Day", "") or "")
    day_canon = re.sub(r"[^a-z]", "", day_raw.strip().lower())
    if not day_canon:
        return None

    campus = str(req.get("Campus", "") or "").strip()
    campus_title = str(meta.get("sheet_title") or "").strip() or campus
    campus_key = str(meta.get("campus_key") or "").strip().upper()
    if not campus_key:
        campus_kind_guess = campus_kind(campus_title or campus)
        campus_key = "ONCALL" if campus_kind_guess == "ONCALL" else str(campus_kind_guess or campus).upper()

    created_d = _approval_created_at_date(req)
    ref_date = created_d or _la_today()

    explicit_week = None
    if campus_title:
        try:
            explicit_week = week_range_mod.week_range_from_title(campus_title, today=ref_date)
        except Exception:
            explicit_week = None
    if not explicit_week:
        explicit_week = _approval_meta_week_bounds(meta, ref_date=created_d)
    if explicit_week:
        try:
            d = week_range_mod.date_for_weekday(explicit_week[0], explicit_week[1], day_canon)
            if d:
                return d
        except Exception:
            pass

    if campus_key == "ONCALL" and campus_title:
        try:
            d = _oncall_event_date(campus_title, day_canon)
            if d:
                return d
        except Exception:
            pass

    return _approval_created_week_fallback(req, day_canon)


def _pending_pickup_minutes_for_week(
    ss,
    approvals_rows: list[dict],
    *,
    requester: str,
    week_bounds: tuple[date, date],
) -> tuple[int, dict[str, int]]:
    """Pending pickup minutes for one requester inside the validation week."""
    want_req = name_key(requester)
    week_total = 0
    per_day: dict[str, int] = {}
    ws, we = week_bounds

    for r in approvals_rows or []:
        if str(r.get("Action", "") or "").strip().lower() != "pickup":
            continue
        if str(r.get("Status", "") or "").strip().upper() != "PENDING":
            continue
        if name_key(str(r.get("Requester", "") or "")) != want_req:
            continue

        event_d = _approval_row_event_date(ss, r)
        if not event_d or not (ws <= event_d <= we):
            continue

        try:
            sd = _parse_12h_time(str(r.get("Start", "") or ""))
            ed = _parse_12h_time(str(r.get("End", "") or ""))
        except Exception:
            continue
        if ed <= sd:
            ed = ed + timedelta(days=1)
        mins = int((ed - sd).total_seconds() // 60)
        day_canon = utils.normalize_day(event_d.strftime("%A")) or re.sub(r"[^a-z]", "", str(r.get("Day", "") or "").strip().lower())
        week_total += mins
        per_day[day_canon] = int(per_day.get(day_canon, 0)) + mins

    return week_total, per_day


def _approved_adjustment_minutes_for_week(
    requester: str,
    week_bounds: tuple[date, date],
) -> tuple[int, dict[str, int], int, dict[str, int]]:
    """Return approved pickup and callout minutes for one requester/week."""
    ws, we = week_bounds
    pickup_week = 0
    callout_week = 0
    pickup_day: dict[str, int] = {}
    callout_day: dict[str, int] = {}

    try:
        for row in pickups_db.list_pickups_for_week(picker_name=requester, week_start=ws, week_end=we):
            try:
                event_d = date.fromisoformat(str(row.get("event_date")))
            except Exception:
                continue
            mins = _minutes_from_hours(row.get("duration_hours"))
            day_canon = utils.normalize_day(event_d.strftime("%A"))
            pickup_week += mins
            pickup_day[day_canon] = int(pickup_day.get(day_canon, 0)) + mins
    except Exception:
        pass

    try:
        for row in callouts_db.list_callouts_for_week(caller_name=requester, week_start=ws, week_end=we):
            try:
                event_d = date.fromisoformat(str(row.get("event_date")))
            except Exception:
                continue
            mins = _minutes_from_hours(row.get("duration_hours"))
            day_canon = utils.normalize_day(event_d.strftime("%A"))
            callout_week += mins
            callout_day[day_canon] = int(callout_day.get(day_canon, 0)) + mins
    except Exception:
        pass

    return pickup_week, pickup_day, callout_week, callout_day


def _overtime_baseline_minutes(
    ss,
    *,
    requester: str,
    base_sched: dict,
    approvals_rows: list[dict],
    week_bounds: tuple[date, date],
) -> tuple[int, dict[str, int]]:
    """Accurate pre-request minutes for overtime checks.

    Baseline = raw scheduled minutes for the request week
               - approved callouts for that same week
               + approved pickups for that same week

    Intentionally do not count other pending pickup requests here. Users expect
    the overtime prompt to start from the same adjusted "current hours" total
    shown in the sidebar, then add the pickup they are submitting now.
    """
    week_before_mins, per_day_before = _sum_minutes_sched(base_sched)

    if callouts_db.supabase_callouts_enabled() and pickups_db.supabase_pickups_enabled():
        pickup_week, pickup_day, callout_week, callout_day = _approved_adjustment_minutes_for_week(
            requester,
            week_bounds,
        )
        week_before_mins = max(0, week_before_mins - callout_week + pickup_week)
        for d in list(per_day_before.keys()):
            per_day_before[d] = max(0, int(per_day_before.get(d, 0)) - int(callout_day.get(d, 0)) + int(pickup_day.get(d, 0)))

    return week_before_mins, per_day_before


def _day_intervals(user_sched_all: dict, day_canon: str) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    buckets = (user_sched_all or {}).get(day_canon, {}) or {}
    for k in ("UNH", "MC", "On-Call"):
        for s12, e12 in _iter_pairs(buckets.get(k, []) or []):
            try:
                sd = _parse_12h_time(s12)
                ed = _parse_12h_time(e12)
            except Exception:
                continue
            if ed <= sd:
                ed = ed + timedelta(days=1)
            intervals.append((sd, ed))
    intervals.sort(key=lambda x: x[0])
    return intervals



def _find_unh_mc_conflict(user_sched_all: dict, day_canon: str, target_bucket: str, req_s: datetime, req_e: datetime) -> str | None:
    """Return a user-facing error message if the request overlaps UNH/MC."""
    if target_bucket not in {"UNH", "MC"}:
        return None
    # Normalize overnight requests.
    if req_e <= req_s:
        req_e = req_e + timedelta(days=1)

    buckets_today = (user_sched_all or {}).get(day_canon, {}) or {}
    for src in ("UNH", "MC"):
        for s12, e12 in _iter_pairs(buckets_today.get(src, []) or []):
            try:
                sd = _parse_12h_time(s12)
                ed = _parse_12h_time(e12)
            except Exception:
                continue
            if ed <= sd:
                ed = ed + timedelta(days=1)
            if max(req_s, sd) < min(req_e, ed):
                if src == target_bucket:
                    return (
                        f"Duplicate entry: you are already scheduled in {src} during "
                        f"{day_canon.title()} {fmt_time(sd)}–{fmt_time(ed)}."
                    )
                return (
                    f"Schedule conflict: you are already scheduled in {src} during "
                    f"{day_canon.title()} {fmt_time(sd)}–{fmt_time(ed)}. "
                    f"You can’t also be scheduled in {target_bucket} at the same time."
                )
    return None


def _find_any_conflict(user_sched_all: dict, day_canon: str, target_bucket: str, req_s: datetime, req_e: datetime) -> str | None:
    """Return a user-facing error message if the request overlaps any bucket.

    `target_bucket` should be one of: "UNH", "MC", "On-Call".
    """
    if not user_sched_all:
        return None
    # Normalize overnight requests.
    if req_e <= req_s:
        req_e = req_e + timedelta(days=1)

    buckets_today = (user_sched_all or {}).get(day_canon, {}) or {}
    for src, seq in (buckets_today or {}).items():
        for s12, e12 in _iter_pairs(seq or []):
            try:
                sd = _parse_12h_time(s12)
                ed = _parse_12h_time(e12)
            except Exception:
                continue
            if ed <= sd:
                ed = ed + timedelta(days=1)
            if max(req_s, sd) < min(req_e, ed):
                if str(src) == str(target_bucket):
                    return (
                        f"Duplicate entry: you are already scheduled in {src} during "
                        f"{day_canon.title()} {fmt_time(sd)}–{fmt_time(ed)}."
                    )
                return (
                    f"Schedule conflict: you are already scheduled in {src} during "
                    f"{day_canon.title()} {fmt_time(sd)}–{fmt_time(ed)}. "
                    f"You can’t also pick up a shift during that time."
                )
    return None


def _render_pickup_tradeboard(ss, schedule_global, canon_user: str | None) -> None:
    """Always-visible tradeboard that lists red call-outs and allows pickup requests."""

    # Determine the latest visible UNH/MC tabs, and scan *all* On-Call week tabs.
    titles = list_tabs_for_sidebar(ss.id)
    tab_unh = next((t for t in reversed(titles) if campus_kind(t) == "UNH"), None)
    tab_mc = next((t for t in reversed(titles) if campus_kind(t) == "MC"), None)
    tabs_oc = [t for t in titles if campus_kind(t) == "ONCALL"]

    ver_map = st.session_state.get("WS_VER", {}) or {}
    v_unh = int(ver_map.get(tab_unh or "", 0))
    v_mc = int(ver_map.get(tab_mc or "", 0))
    v_oc_map = {t: int(ver_map.get(t, 0)) for t in tabs_oc}

    with st.expander("📣 Pickup called-out shifts (Tradeboard)", expanded=False):
        st.caption("Shows *red* (no-cover) call-outs. Submit a pickup request to turn the slot **orange** once approved.")

        # Aesthetic “calendar block” style (like your screenshot).
        st.markdown(
            """
<style>
/* Tradeboard v2: responsive call-out cards (no fixed-height time grid) */
.tb2-grid { display:flex; gap:12px; align-items:flex-start; }
.tb2-col { min-width: 210px; flex:1; }
.tb2-dayhead { font-weight:750; font-size: 0.95rem; color: var(--oa-ink, #111827); margin: 0 0 8px 0; }
.tb2-empty {
  color: var(--oa-trade-empty-ink, rgba(15,23,42,0.62));
  font-size:0.85rem;
  padding:10px 10px;
  border:1px dashed var(--oa-trade-empty-border, rgba(15,23,42,0.16));
  border-radius: 8px;
  background: var(--oa-trade-empty-bg, linear-gradient(135deg, rgba(79,70,229,0.06), rgba(20,184,166,0.05)));
  backdrop-filter: blur(6px);
}
.tb2-card {
  background: var(--oa-trade-card-bg, linear-gradient(135deg, rgba(238,242,255,0.80), rgba(224,231,255,0.58)));
  border:1px solid var(--oa-trade-card-border, rgba(227,81,81,0.18));
  border-left: 5px solid var(--oa-trade-card-strip, rgba(227,81,81,0.72));
  border-radius: 10px;
  padding:10px 10px 10px 12px;
  margin: 0 0 10px 0;
  box-shadow: var(--oa-shadow-soft, 0 10px 22px rgba(2,6,23,0.06));
  backdrop-filter: blur(8px);
}
.tb2-top { display:flex; justify-content:space-between; gap:8px; align-items:center; }
.tb2-time { font-weight:700; font-size:0.92rem; color:var(--oa-trade-time-ink, #111827); }
.tb2-badge { font-size:0.72rem; font-weight:750; padding:3px 8px; border-radius:9px; color:var(--oa-trade-badge-ink, #0f172a); background:var(--oa-trade-badge-bg, linear-gradient(135deg, rgba(238,242,255,0.86), rgba(224,231,255,0.66))); border:1px solid var(--oa-trade-badge-border, rgba(15,23,42,0.12)); white-space:nowrap; }
.tb2-badge.unh { background:var(--oa-campus-unh-bg, #e0f2fe); border-color:var(--oa-campus-unh-border, #bae6fd); color:var(--oa-campus-unh-ink, #0c4a6e); }
.tb2-badge.mc { background:var(--oa-campus-mc-bg, #dcfce7); border-color:var(--oa-campus-mc-border, #bbf7d0); color:var(--oa-campus-mc-ink, #14532d); }
.tb2-badge.oncall { background:var(--oa-campus-oncall-bg, #ffedd5); border-color:var(--oa-campus-oncall-border, #fed7aa); color:var(--oa-campus-oncall-ink, #9a3412); }
.tb2-sub { color:var(--oa-trade-sub-ink, #6b7280); font-size:0.78rem; margin-top:4px; }
.tb2-names { margin-top:8px; display:flex; flex-direction:column; gap:6px; }
.tb2-nitem { background:var(--oa-trade-name-bg, linear-gradient(135deg, rgba(227,81,81,0.10), rgba(238,242,255,0.72))); border:1px solid var(--oa-trade-name-border, rgba(227,81,81,0.18)); border-radius:8px; padding:6px 8px; font-size:0.82rem; font-weight:650; color:var(--oa-trade-name-ink, #7f1d1d); line-height:1.1; }
.tb2-nmore { color:var(--oa-trade-more-ink, rgba(127,29,29,0.86)); font-size:0.8rem; font-weight:700; padding-left:2px; }
/* Tighten Streamlit widgets inside columns */
div[data-testid="column"] div.stButton>button { border-radius:12px; }
</style>
            """ ,
            unsafe_allow_html=True,
        )

        def _day_order_from_df(df) -> list[str]:
            if df is None or getattr(df, "empty", True):
                return []
            order = []
            for col in list(df.columns):
                if col == "Time":
                    continue
                d = schedule_query._canon_day_from_header(str(col))
                if d and d not in order:
                    order.append(d)
            return order

        def _day_label_map_from_df(df) -> dict[str, str]:
            m = {}
            if df is None or getattr(df, "empty", True):
                return m
            for col in list(df.columns):
                if col == "Time":
                    continue
                d = schedule_query._canon_day_from_header(str(col))
                if d and d not in m:
                    m[d] = str(col)
            return m


        def _render_tradeboard_v2(kind_tag: str, df, wins_for_kind: list[pickup_scan.PickupWindow]):
            """Render call-outs as responsive cards (no fixed-height time grid).
        
            Groups multiple called-out people who share the exact same window into one card.
            Each card lets the user choose which person to cover and preselects that call-out
            in the pickup form below.
            """
            import html as _html
            import hashlib
        
            if not wins_for_kind:
                st.info("No red call-outs found.")
                return
        
            order = _day_order_from_df(df)
            if not order:
                order = list(schedule_query._WEEK_ORDER_7 if kind_tag == "ONCALL" else schedule_query._WEEK_ORDER_5)
        
            day_labels = _day_label_map_from_df(df)
        
            # Group windows → one card per unique (day, start, end, sheet_title, kind)
            blocks: dict[tuple, dict] = {}
            for w in wins_for_kind:
                campus_lbl = "On-Call" if w.kind == "ONCALL" else w.kind
                key = (w.day_canon, w.start, w.end, w.campus_title, w.kind)
                if key not in blocks:
                    blocks[key] = {
                        "day": w.day_canon,
                        "start": w.start,
                        "end": w.end,
                        "sheet": w.campus_title,
                        "kind": w.kind,
                        "campus_lbl": campus_lbl,
                        "names": [],
                    }
                if w.target_name not in blocks[key]["names"]:
                    blocks[key]["names"].append(w.target_name)
        
            by_day: dict[str, list[dict]] = {d: [] for d in order}
            for b in blocks.values():
                by_day.setdefault(b["day"], []).append(b)
        
            for d in by_day:
                by_day[d].sort(key=lambda x: (x["start"], x["end"], x["sheet"]))
        
            cols = st.columns(len(order), gap="small")
            for col, d in zip(cols, order):
                with col:
                    st.markdown(
                        f"<div class='tb2-dayhead'>{_html.escape(day_labels.get(d, d.title()))}</div>",
                        unsafe_allow_html=True,
                    )
                    day_blocks = by_day.get(d, [])
                    if not day_blocks:
                        st.markdown("<div class='tb2-empty'>No call-outs</div>", unsafe_allow_html=True)
                        continue
        
                    for b in day_blocks:
                        names_all = list(b["names"])
                        shown = names_all[:10]
                        more = max(0, len(names_all) - len(shown))
        
                        names_html = "".join([f"<div class='tb2-nitem'>{_html.escape(n)}</div>" for n in shown])
                        if more:
                            names_html += f"<div class='tb2-nmore'>+{more} more</div>"

        
                        badge_cls = "oncall" if b["campus_lbl"] == "On-Call" else ("unh" if b["campus_lbl"] == "UNH" else ("mc" if b["campus_lbl"] == "MC" else ""))
                        time_txt = f"{fmt_time(b['start'])}–{fmt_time(b['end'])}"
        
                        st.markdown(
                            f"""<div class='tb2-card'>
                                  <div class='tb2-top'>
                                    <div class='tb2-time'>{_html.escape(time_txt)}</div>
                                    <div class='tb2-badge {badge_cls}'>{_html.escape(b['campus_lbl'])}</div>
                                  </div>
                                  <div class='tb2-sub'>{_html.escape(b['sheet'])}</div>
                                  <div class='tb2-names'>{names_html}</div>
                                </div>""",
                            unsafe_allow_html=True,
                        )
        
                        bid_src = f"{b['kind']}|{b['sheet']}|{b['day']}|{b['start'].isoformat()}|{b['end'].isoformat()}|{'|'.join(names_all)}"
                        bid = hashlib.md5(bid_src.encode()).hexdigest()[:10]
        
                        if len(names_all) > 1:
                            pick_name = st.selectbox(
                                "Who are you covering?",
                                names_all,
                                key=f"tb2_pick_{bid}",
                            )
                        else:
                            pick_name = names_all[0]
        
                        if st.button("Pick up", key=f"tb2_btn_{bid}", use_container_width=True):
                            # Open a popup to validate + submit this pickup request.
                            st.session_state["TB2_MODAL"] = {
                                "target": pick_name,
                                "names": names_all,
                                "day": b["day"],
                                "kind": b["kind"],
                                "start": b["start"].isoformat(),
                                "end": b["end"].isoformat(),
                                "sheet": b["sheet"],
                            }
                            st.rerun()
        # Load cached tradeboards (guard against transient Google/network/quota errors)
        def _safe_tradeboard(tab_title: str, ver: int, campus: str) -> dict:
            try:
                return pickup_scan.cached_tradeboard(ss.id, tab_title, ver, campus)
            except Exception as e:
                # Friendly recovery for transient quota/network errors (internet off, DNS, timeouts, etc.)
                if maybe_show_recovery_popup(e, where=f"loading {campus} tradeboard"):
                    return {"df": None, "windows": []}
                return {"df": None, "windows": []}

        data_unh = _safe_tradeboard(tab_unh or "", v_unh, "UNH") if tab_unh else {"df": None, "windows": []}
        data_mc = _safe_tradeboard(tab_mc or "", v_mc, "MC") if tab_mc else {"df": None, "windows": []}

        data_oc_list: list[tuple[str, dict]] = []
        for oc in tabs_oc:
            data_oc_list.append((oc, _safe_tradeboard(oc, v_oc_map.get(oc, 0), "ONCALL")))

        wins: list[pickup_scan.PickupWindow] = []
        wins.extend(_pickup_windows_from_cached(data_unh.get("windows") or []))
        wins.extend(_pickup_windows_from_cached(data_mc.get("windows") or []))
        for _, d in data_oc_list:
            wins.extend(_pickup_windows_from_cached(d.get("windows") or []))

        count_unh = len(_pickup_windows_from_cached(data_unh.get("windows") or []))
        count_mc = len(_pickup_windows_from_cached(data_mc.get("windows") or []))
        count_oc = sum(len(_pickup_windows_from_cached(d.get("windows") or [])) for _, d in data_oc_list)

        t1, t2, t3 = st.tabs([f"UNH ({count_unh})", f"MC ({count_mc})", f"On-Call ({count_oc})"])
        with t1:
            if tab_unh:
                _render_tradeboard_v2("UNH", data_unh.get("df"), _pickup_windows_from_cached(data_unh.get("windows") or []))
            else:
                st.info("No UNH tab visible.")
        with t2:
            if tab_mc:
                _render_tradeboard_v2("MC", data_mc.get("df"), _pickup_windows_from_cached(data_mc.get("windows") or []))
            else:
                st.info("No MC tab visible.")
        with t3:
            if not tabs_oc:
                st.info("No On-Call tabs visible.")
            else:
                any_shown = False
                for oc_title, oc_data in data_oc_list:
                    oc_wins = _pickup_windows_from_cached(oc_data.get("windows") or [])
                    if not oc_wins:
                        continue  # Mode A: skip weeks with no red call-outs
                    any_shown = True
                    # Use explicit HTML line break so we never render a literal "\\n".
                    st.markdown(
                        f"**{oc_title}**<br>{len(oc_wins)} called-out block(s)",
                        unsafe_allow_html=True,
                    )
                    _render_tradeboard_v2("ONCALL", oc_data.get("df"), oc_wins)
                    st.markdown("---")
                if not any_shown:
                    st.info("No red call-outs found on any On-Call week tabs.")



        # --- Popup behavior for card "Pick up" buttons ---
        def _fmt_mins(m: int) -> str:
            m = int(m)
            if m < 60:
                return f"{m} min"
            return f"{m//60} hr {m%60:02d} min"

        def _open_tb2_pickup_dialog(payload: dict) -> None:
            dialog = getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None)
            if dialog is None:
                st.warning("Pop-up dialogs are not available in this Streamlit version. Use the pickup form below.")
                return

            import hashlib
            from datetime import datetime

            mid_src = f"{payload.get('kind')}|{payload.get('sheet')}|{payload.get('day')}|{payload.get('start')}|{payload.get('end')}"
            mid = hashlib.md5(mid_src.encode()).hexdigest()[:10]

            @dialog("Pick up called-out shift")
            def _dlg():
                # Parse payload
                kind = str(payload.get("kind") or "")
                sheet_title = str(payload.get("sheet") or "")
                day_canon = str(payload.get("day") or "").lower()

                try:
                    win_start = datetime.fromisoformat(str(payload.get("start")))
                    win_end = datetime.fromisoformat(str(payload.get("end")))
                except Exception:
                    st.error("This call-out could not be parsed. Please refresh.")
                    if st.button("Close", key=f"tb2m_close_bad_{mid}"):
                        st.session_state.pop("TB2_MODAL", None)
                        st.rerun()
                    return

                if not canon_user:
                    st.error("Type your name in the left sidebar to request a pickup.")
                    if st.button("Close", key=f"tb2m_close_noname_{mid}"):
                        st.session_state.pop("TB2_MODAL", None)
                        st.rerun()
                    return

                campus_lbl = "On-Call" if kind == "ONCALL" else kind
                st.markdown(f"**{campus_lbl}** • **{day_canon.title()}** • **{fmt_time(win_start)}–{fmt_time(win_end)}**")
                st.caption(sheet_title)

                names = list(payload.get("names") or [])
                default_target = str(payload.get("target") or (names[0] if names else ""))
                if names:
                    try:
                        idx = names.index(default_target)
                    except Exception:
                        idx = 0
                    target = st.selectbox("Who are you covering?", names, index=idx, key=f"tb2m_target_{mid}")
                else:
                    target = default_target

                # Choose coverage window
                req_s, req_e = win_start, win_end
                if kind in {"UNH", "MC"}:
                    step = timedelta(minutes=30)
                    starts = []
                    cur = win_start
                    while cur + timedelta(minutes=30) <= win_end:
                        starts.append(cur)
                        cur += step

                    if not starts:
                        st.warning("This call-out is shorter than 30 minutes and must be picked up in full.")
                    else:
                        start_lbls = [fmt_time(x) for x in starts]
                        start_pick = st.selectbox("Start time", start_lbls, index=0, key=f"tb2m_start_{mid}")
                        req_s = starts[start_lbls.index(start_pick)]

                        max_m = int((win_end - req_s).total_seconds() // 60)
                        dur_opts = [m for m in range(30, max_m + 1, 30)]
                        dur_labels = [_fmt_mins(m) for m in dur_opts]
                        if dur_labels:
                            # Default to full-coverage
                            dur_pick = st.selectbox("Cover length (30-min steps)", dur_labels, index=len(dur_labels)-1, key=f"tb2m_dur_{mid}")
                            mins = dur_opts[dur_labels.index(dur_pick)]
                            req_e = req_s + timedelta(minutes=int(mins))
                else:
                    st.info("On-Call pickups are for the full block.")

                st.write(f"**Request:** {target} — {day_canon.title()} {fmt_time(req_s)}–{fmt_time(req_e)}")

                # Load schedule for validations (include pending/approved pickups)
                base_sched = {}
                appr_rows = []
                request_week_bounds = _week_bounds_la()
                try:
                    epoch = st.session_state.get("UI_EPOCH", 0)
                    week_titles_map = _resolve_week_titles(titles, sheet_title, ss=ss)
                    week_titles_set = {t for t in week_titles_map.values() if t}

                    base_sched = cached_user_schedule_for_titles(
                        ss.id,
                        canon_user,
                        week_titles_map.get("UNH"),
                        week_titles_map.get("MC"),
                        week_titles_map.get("ONCALL"),
                        epoch,
                    )

                    ver_map = st.session_state.get("WS_VER", {}) or {}
                    appr_epoch = int(ver_map.get(config.APPROVAL_SHEET, 0))
                    appr_rows = cached_approval_table(ss.id, appr_epoch, max_rows=500)
                    request_week_bounds = _request_week_bounds(ss, week_titles_map, sheet_title)
                    user_sched_all = _append_my_pickups_into_sched(
                        base_sched,
                        appr_rows,
                        requester=canon_user,
                        week_titles=week_titles_set,
                        include_statuses={"PENDING", "APPROVED"},
                        ss=ss,
                        week_bounds=request_week_bounds,
                    )
                except Exception as e:
                    # If approvals fetch failed due to network/quota, show the recovery popup.
                    if maybe_show_recovery_popup(e, where="loading approvals for pickup validations"):
                        return
                    # Otherwise, fall back to a direct schedule read (still allows basic conflict checks).
                    user_sched_all = schedule_query.get_user_schedule(ss, schedule_global, canon_user)
                    base_sched = user_sched_all

                bucket = "On-Call" if kind == "ONCALL" else kind
                conflict_msg = _find_any_conflict(user_sched_all, day_canon, bucket, req_s, req_e)
                if conflict_msg:
                    st.error(conflict_msg)

                existing_intervals = _day_intervals(user_sched_all, day_canon)

                # Minimum consecutive 1h30 rule (popup message)
                min_consec_ok = True
                min_consec_block_mins = 0
                if kind in {"UNH", "MC"}:
                    min_consec_ok, min_consec_block_mins = labor_rules.consecutive_block_minutes_for(
                        existing_intervals,
                        (req_s, req_e),
                        min_consecutive_mins=90,
                    )
                    if not min_consec_ok:
                        st.error(
                            "Minimum shift length: you must have at least **1 hr 30 min** of consecutive work. "
                            f"This pickup would give you only **{_fmt_mins(min_consec_block_mins)}** consecutive. "
                            "Pick a longer window, or pick a slot that touches one of your existing shifts so the total consecutive block is ≥ 1h 30m."
                        )

                # 5h continuous => 30m break rule
                min_pickup_mins = 30 if kind in {"UNH", "MC"} else 0
                break_res = labor_rules.break_check_with_suggestions(
                    existing_intervals,
                    (req_s, req_e),
                    window=(win_start, win_end),
                    min_duration_mins=int(min_pickup_mins),
                    step_mins=30,
                )
                if not break_res.ok:
                    st.error(
                        f"Break rule: {break_res.reason}. You need a 30-minute break before/after the 5th consecutive hour."
                    )
                    if break_res.alternatives and kind in {"UNH", "MC"}:
                        alt_s, alt_e = break_res.alternatives[0]
                        st.caption(f"Suggested option: **{fmt_time(alt_s)}–{fmt_time(alt_e)}**")
                        if st.button("Use suggested option", key=f"tb2m_use_alt_{mid}"):
                            # Update the dialog selections via session_state
                            # (start/dur keys must match the option labels)
                            alt_start_lbl = fmt_time(alt_s)
                            st.session_state[f"tb2m_start_{mid}"] = alt_start_lbl
                            alt_mins = int(labor_rules.minutes_between(alt_s, alt_e))
                            st.session_state[f"tb2m_dur_{mid}"] = _fmt_mins(alt_mins)
                            st.rerun()

                # Hours caps with overtime prompt (popup)
                week_before_mins, per_day_before = _overtime_baseline_minutes(
                    ss,
                    requester=canon_user,
                    base_sched=base_sched,
                    approvals_rows=appr_rows,
                    week_bounds=request_week_bounds,
                )
                req_mins = labor_rules.minutes_between(req_s, req_e)
                day_before_mins = int(per_day_before.get(day_canon, 0))
                week_after_mins = int(week_before_mins) + int(req_mins)
                day_after_mins = int(day_before_mins) + int(req_mins)

                overtime_reasons = []
                if week_after_mins > labor_rules.MAX_WEEKLY_MINS:
                    overtime_reasons.append(
                        f"weekly total would be {week_after_mins/60:.2f} hrs (cap {labor_rules.MAX_WEEKLY_MINS/60:.0f})"
                    )
                if day_after_mins > labor_rules.MAX_DAILY_MINS:
                    overtime_reasons.append(
                        f"day total would be {day_after_mins/60:.2f} hrs (cap {labor_rules.MAX_DAILY_MINS/60:.0f})"
                    )

                overtime_needed = bool(overtime_reasons)
                ot_choice = "No"
                if overtime_needed:
                    st.warning("This pickup would put you over the limit: " + "; ".join(overtime_reasons))
                    ot_choice = st.radio(
                        "Ask permission for overtime?",
                        ["No", "Yes"],
                        horizontal=True,
                        key=f"tb2m_ot_{mid}",
                    )

                can_submit = (not conflict_msg) and min_consec_ok and break_res.ok and ((not overtime_needed) or (ot_choice == "Yes"))

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Cancel", key=f"tb2m_cancel_{mid}"):
                        st.session_state.pop("TB2_MODAL", None)
                        st.rerun()
                with c2:
                    if st.button("Send for approval", type="secondary", disabled=not can_submit, key=f"tb2m_send_{mid}"):
                        if not can_submit:
                            st.error("Please fix the issues above before submitting.")
                            return
                        try:
                            event_d = (_date_for_weekday_in_sheet(ss, sheet_title, day_canon) if kind in {"UNH", "MC"} else _oncall_event_date(sheet_title, day_canon))
                            details = f"target={target}"
                            if event_d:
                                details += f" | date={event_d.isoformat()}"
                            if overtime_needed:
                                details += f" | overtime: yes | week_after={week_after_mins/60:.2f} | day_after={day_after_mins/60:.2f}"
                            campus_key = ("ONCALL" if kind == "ONCALL" else kind)
                            with st.spinner("Submitting request..."):
                                submit_approval_request(
                                    ss,
                                    requester=canon_user,
                                    action="pickup",
                                    campus=campus_key,
                                    day=day_canon.title(),
                                    start=fmt_time(req_s),
                                    end=fmt_time(req_e),
                                    details=_attach_details_meta(
                                        details=details,
                                        campus_key=campus_key,
                                        sheet_title=sheet_title,
                                        sheet_gid=_sheet_gid_for_title(schedule_global, sheet_title),
                                    ),
                                )
                            st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                            st.session_state.pop("TB2_MODAL", None)
                            _flash("success", "Pickup request submitted for approval.")
                            st.rerun()
                        except Exception as e:
                            # Friendly recovery for transient quota/network errors.
                            if maybe_show_recovery_popup(e, where="submitting approval request"):
                                return
                            st.error(_strip_debug_blob(str(e)))

            _dlg()

        _modal_payload = st.session_state.get("TB2_MODAL")
        if _modal_payload:
            _open_tb2_pickup_dialog(_modal_payload)

        st.markdown("---")
        st.subheader("Pick up a called-out shift")
        if not wins:
            st.caption("Nothing to pick up right now.")
            return

        if not canon_user:
            st.info("Type your name in the sidebar to request a pickup.")
            return

        # Build labels
        label_to_win: dict[str, pickup_scan.PickupWindow] = {}
        for w in wins:
            campus_tag = "UNH" if w.kind == "UNH" else ("MC" if w.kind == "MC" else "On-Call")
            lbl = f"{w.target_name} — {fmt_time(w.start)}–{fmt_time(w.end)} ({campus_tag} • {w.day_canon.title()})"
            # Disambiguate duplicates
            base = lbl
            k = 2
            while lbl in label_to_win:
                lbl = f"{base} ({k})"
                k += 1
            label_to_win[lbl] = w

        direct = st.session_state.pop("_PICKUP_DIRECT", None)
        if direct:
            for _lbl, _w in label_to_win.items():
                if (
                    _w.target_name == direct.get("target")
                    and _w.kind == direct.get("kind")
                    and _w.day_canon == direct.get("day")
                    and _w.campus_title == direct.get("sheet")
                    and _w.start.isoformat() == direct.get("start")
                    and _w.end.isoformat() == direct.get("end")
                ):
                    st.session_state["pickup_pick"] = _lbl
                    break

        pick_lbl = st.selectbox("Choose a call-out", list(label_to_win.keys()), key="pickup_pick")
        w = label_to_win[pick_lbl]

        # For UNH/MC: allow partial coverage in 30-minute increments (min 1 hr 30 min).
        req_s = w.start
        req_e = w.end
        if w.kind in {"UNH", "MC"}:
            step = timedelta(minutes=30)
            min_cover = timedelta(minutes=90)

            # Only allow starts that can fit at least 1h30.
            starts = []
            cur = w.start
            while cur + min_cover <= w.end:
                starts.append(cur)
                cur += step

            if not starts:
                st.warning("This call-out is shorter than 1 hr 30 min, so it can only be picked up in full.")
                req_s = w.start
                req_e = w.end
            else:
                start_lbls = [fmt_time(x) for x in starts]
                start_pick = st.selectbox("Start time", start_lbls, index=0, key="pickup_start")
                req_s = starts[start_lbls.index(start_pick)]

                max_m = int((w.end - req_s).total_seconds() // 60)
                dur_opts = [m for m in range(30, max_m + 1, 30)]
                dur_labels = [f"{m//60} hr {m%60:02d} min" if m >= 60 else f"{m} min" for m in dur_opts]
                dur_pick = st.selectbox(
                    "How long will you cover? (30-min steps; must result in ≥ 1 hr 30 min consecutive total)",
                    dur_labels,
                    index=0,
                    key="pickup_dur",
                )
                mins = dur_opts[dur_labels.index(dur_pick)]
                req_e = req_s + timedelta(minutes=int(mins))
        else:
            st.caption("On-Call pickups are for the full block.")

        st.write(f"**Request:** {w.target_name} — {w.day_canon.title()} {fmt_time(req_s)}–{fmt_time(req_e)} ({w.campus_title})")

        # ------------------------------------------------------------------
        # Labor-rule validations (hours caps + break rule)
        #
        # We compute these *before* submission so the user can see why it would
        # be blocked, and (when possible) choose a suggested alternative.
        # ------------------------------------------------------------------
        base_sched = {}
        appr_rows = []
        request_week_bounds = _week_bounds_la()
        try:
            epoch = st.session_state.get("UI_EPOCH", 0)
            week_titles_map = _resolve_week_titles(titles, w.campus_title, ss=ss)
            week_titles_set = {t for t in week_titles_map.values() if t}

            base_sched = cached_user_schedule_for_titles(
                ss.id,
                canon_user,
                week_titles_map.get("UNH"),
                week_titles_map.get("MC"),
                week_titles_map.get("ONCALL"),
                epoch,
            )

            ver_map = st.session_state.get("WS_VER", {}) or {}
            appr_epoch = int(ver_map.get(config.APPROVAL_SHEET, 0))
            appr_rows = cached_approval_table(ss.id, appr_epoch, max_rows=500)
            request_week_bounds = _request_week_bounds(ss, week_titles_map, w.campus_title)
            user_sched_all = _append_my_pickups_into_sched(
                base_sched,
                appr_rows,
                requester=canon_user,
                week_titles=week_titles_set,
                include_statuses={"PENDING", "APPROVED"},
                ss=ss,
                week_bounds=request_week_bounds,
            )
        except Exception as e:
            if maybe_show_recovery_popup(e, where="loading approvals for labor-rule checks"):
                return
            # Fallback: still allow submission with at least overlap checks.
            user_sched_all = schedule_query.get_user_schedule(ss, schedule_global, canon_user)
            base_sched = user_sched_all

        # Break rule check (5h continuous => need a 30m break)
        min_pickup_mins = 30 if w.kind in {"UNH", "MC"} else 0

        existing_intervals = _day_intervals(user_sched_all, w.day_canon)

        # Minimum consecutive work rule (1h 30m). The pickup itself may be shorter,
        # but the consecutive *block* it participates in must be >= 90 minutes.
        min_consec_ok = True
        min_consec_block_mins = 0
        if w.kind in {"UNH", "MC"}:
            min_consec_ok, min_consec_block_mins = labor_rules.consecutive_block_minutes_for(
                existing_intervals,
                (req_s, req_e),
                min_consecutive_mins=90,
            )
            if not min_consec_ok:
                block_lbl = (
                    f"{int(min_consec_block_mins)} min"
                    if int(min_consec_block_mins) < 60
                    else f"{int(min_consec_block_mins)//60} hr {int(min_consec_block_mins)%60:02d} min"
                )
                st.error(
                    "Minimum shift length: you must have **at least 1 hr 30 min** of consecutive work. "
                    f"This pickup would give you only **{block_lbl}** consecutive. "
                    "Pick a longer window, or pick a slot that touches one of your existing shifts so the total consecutive block is ≥ 1h 30m."
                )

        break_res = labor_rules.break_check_with_suggestions(
            existing_intervals,
            (req_s, req_e),
            window=(w.start, w.end),
            min_duration_mins=int(min_pickup_mins),
            step_mins=30,
        )

        def _dur_label(m: int) -> str:
            if m < 60:
                return f"{m} min"
            return f"{m//60} hr {m%60:02d} min"

        if not break_res.ok:
            st.error(
                f"Break rule: {break_res.reason}. "
                f"You need a 30-minute break before/after the 5th consecutive hour."
            )
            if break_res.alternatives and w.kind in {"UNH", "MC"}:
                alt_s, alt_e = break_res.alternatives[0]
                alt_mins = labor_rules.minutes_between(alt_s, alt_e)
                alt_start_lbl = fmt_time(alt_s)
                alt_dur_lbl = _dur_label(int(alt_mins))
                st.caption(f"Suggested option: **{fmt_time(alt_s)}–{fmt_time(alt_e)}**")
                if st.button(
                    f"Use suggested option ({alt_start_lbl} → {alt_dur_lbl})",
                    key="btn_pickup_use_break_suggest",
                    use_container_width=True,
                ):
                    # These keys match the selectboxes above.
                    st.session_state["pickup_start"] = alt_start_lbl
                    st.session_state["pickup_dur"] = alt_dur_lbl
                    st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                st.session_state["_SCROLL_TO_PENDING"] = True
                st.rerun()

        # Hours caps (20h/week, 8h/day) — allow overtime *request* to approvers.
        week_before_mins, per_day_before = _overtime_baseline_minutes(
            ss,
            requester=canon_user,
            base_sched=base_sched,
            approvals_rows=appr_rows,
            week_bounds=request_week_bounds,
        )
        req_mins = labor_rules.minutes_between(req_s, req_e)
        day_before_mins = int(per_day_before.get(w.day_canon, 0))
        week_after_mins = int(week_before_mins) + int(req_mins)
        day_after_mins = int(day_before_mins) + int(req_mins)

        overtime_reasons: list[str] = []
        if week_after_mins > labor_rules.MAX_WEEKLY_MINS:
            overtime_reasons.append(
                f"weekly total would be {week_after_mins/60:.2f} hrs (cap {labor_rules.MAX_WEEKLY_MINS/60:.0f})"
            )
        if day_after_mins > labor_rules.MAX_DAILY_MINS:
            overtime_reasons.append(
                f"day total would be {day_after_mins/60:.2f} hrs (cap {labor_rules.MAX_DAILY_MINS/60:.0f})"
            )

        overtime_needed = bool(overtime_reasons)
        ot_choice = "No"
        if overtime_needed:
            st.warning(
                "This pickup would put you over the limit: " + "; ".join(overtime_reasons)
            )
            ot_choice = st.radio(
                "Ask permission for overtime?",
                ["No", "Yes"],
                horizontal=True,
                key="pickup_overtime_choice",
            )

        if st.button("📤 Send pickup request for approval", type="secondary", use_container_width=True, key="btn_send_pickup"):
            try:
                bucket = "On-Call" if w.kind == "ONCALL" else w.kind

                # 1) Overlap check (includes user's pending/approved pickups if we were able to load them)
                msg = _find_any_conflict(user_sched_all, w.day_canon, bucket, req_s, req_e)
                if msg:
                    st.error(msg)
                    return

                # 2) Minimum consecutive work check
                if not min_consec_ok:
                    st.error(
                        "Cannot submit: minimum consecutive work is **1 hr 30 min**. "
                        "Pick a longer pickup window, or one that touches an existing shift so the combined consecutive block is ≥ 1h 30m."
                    )
                    return


                # 3) Break rule check
                if not break_res.ok:
                    st.error("Cannot submit: break rule violation. Use the suggested option or adjust your pickup window.")
                    return

                # 3) Hours caps
                if overtime_needed and ot_choice != "Yes":
                    st.error("This exceeds daily/weekly caps. Select **Yes** to request overtime approval, or shorten the pickup.")
                    return

                event_d = (_date_for_weekday_in_sheet(ss, w.campus_title, w.day_canon) if w.kind in {"UNH", "MC"} else _oncall_event_date(w.campus_title, w.day_canon))
                details = f"target={w.target_name}"
                if event_d:
                    details += f" | date={event_d.isoformat()}"
                if overtime_needed:
                    details += (
                        f" | overtime: yes"
                        f" | week_after={week_after_mins/60:.2f}"
                        f" | day_after={day_after_mins/60:.2f}"
                    )
                campus_key = ("ONCALL" if w.kind == "ONCALL" else w.kind)
                with st.spinner("Submitting request..."):
                    submit_approval_request(
                        ss,
                        requester=canon_user,
                        action="pickup",
                        campus=campus_key,
                        day=w.day_canon.title(),
                        start=fmt_time(req_s),
                        end=fmt_time(req_e),
                        details=_attach_details_meta(
                            details=details,
                            campus_key=campus_key,
                            sheet_title=w.campus_title,
                            sheet_gid=_sheet_gid_for_title(schedule_global, w.campus_title),
                        ),
                    )
                _flash("success", "Pickup request submitted for approval.")
                st.rerun()
            except Exception as e:
                if maybe_show_recovery_popup(e, where="submitting pickup approval request"):
                    return
                st.error(_strip_debug_blob(str(e)))


def run() -> None:
    st.set_page_config(page_title="OA Scheduling Assistant", page_icon="🗓️", layout="wide")
    legacy_mode = str(st.session_state.get("UI_THEME_MODE", "")).strip().lower()
    if "UI_THEME_DARK" not in st.session_state:
        st.session_state["UI_THEME_DARK"] = bool(legacy_mode == "dark")
    theme_mode = "dark" if bool(st.session_state.get("UI_THEME_DARK", False)) else "light"
    st.session_state["UI_THEME_MODE"] = theme_mode
    # Global UI theme (purely visual). Safe: no functional behavior changes.
    apply_vibrant_theme(theme_mode)
    # Global, fixed footer visible on every screen.
    render_global_footer()

    # Hero header (visual only)
    st.markdown(
        """
        <div class="oa-hero">
          <div class="oa-hero__title">🗓️ OA Scheduling Assistant</div>
          <div class="oa-hero__sub">
            Enter your name, pick an action (Add / Remove / Call-Out). Guided steps ensure caps and available slots.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ------------------------------------------------------------------
    # Flash messages
    #
    # Streamlit reruns top-to-bottom. Our sidebar "Current hours" metric is
    # rendered *before* the Add/Remove handlers execute. So after a successful
    # write, we set a flash message + bust caches and then `st.rerun()` so the
    # sidebar recomputes from the updated sheet.
    # ------------------------------------------------------------------
    flash = st.session_state.pop("_FLASH", None)
    if isinstance(flash, dict):
        kind = flash.get("kind")
        msg = flash.get("msg")
        if msg:
            if kind == "success":
                st.success(msg)
            elif kind == "error":
                st.error(msg)
            elif kind == "info":
                st.info(msg)
            else:
                st.toast(msg)

    # If a previous run requested a hard refresh, do it before we touch any caches.
    if st.session_state.pop("_DO_HARD_REFRESH", False):
        clear_hard_caches()

    sheet_url = st.secrets.get("SHEET_URL", config.DEFAULT_SHEET_URL)
    if not sheet_url:
        st.error("Missing SHEET_URL in secrets and no DEFAULT_SHEET_URL set.")
        st.stop()

    # ──────────────────────────────────────────────────────────────────
    # Google Sheets connect (friendly recovery UI on failures)
    # ──────────────────────────────────────────────────────────────────
    try:
        ss = open_spreadsheet(sheet_url)
    except Exception as e:  # noqa: BLE001
        # Examples seen in the wild:
        # - DNS/Internet down: "Failed to resolve oauth2.googleapis.com"
        # - RemoteDisconnected / ConnectionResetError during token refresh
        maybe_show_recovery_popup(e, where="connecting to Google Sheets")
        return

    # Reuse Schedule object across reruns to reduce worksheet/header reads.
    sched_by_id = st.session_state.setdefault("_SCHEDULE_BY_ID", {})
    schedule_global = sched_by_id.get(getattr(ss, "id", ""))
    if schedule_global is None:
        schedule_global = Schedule(ss)
        sched_by_id[getattr(ss, "id", "")] = schedule_global
    else:
        # Keep the spreadsheet handle fresh in case caches were cleared.
        try:
            schedule_global.ss = ss
        except Exception:
            pass

    st.session_state.setdefault("_SS_HANDLE_BY_ID", {})[ss.id] = ss
    st.session_state["_SCHEDULE_GLOBAL"] = schedule_global
    # Back-compat: some modules still look for HOURS_EPOCH.
    st.session_state.setdefault("HOURS_EPOCH", 0)
    st.session_state.setdefault("sched_expanded", True)

    # Cache key based on worksheet versions; updates automatically after writes.
    try:
        epoch_key = _versions_key(ss)
    except Exception as e:
        maybe_show_recovery_popup(e, where="listing worksheet titles")
        return

    # Roster
    try:
        roster = load_roster(sheet_url)
    except Exception as e:
        if maybe_show_recovery_popup(e, where="loading roster"):
            return
        roster = []
    roster_canon_by_key = {name_key(n): n for n in roster}

    # Sidebar
    with st.sidebar:
        st.toggle("Dark mode", key="UI_THEME_DARK")

        st.subheader("Who are you?")

        # Name entry (match against roster display names from (Names of hired OAs)).
        oa_name_input = st.text_input("Your full name (from hired OA list)", key="OA_NAME_INPUT")

        canon_name = None
        approver_recognized = False
        if oa_name_input:
            # Exact (normalized) match
            canon_name = roster_canon_by_key.get(name_key(oa_name_input))

            # Handle "Last, First" input
            if not canon_name and "," in oa_name_input:
                try:
                    last, first = [p.strip() for p in oa_name_input.split(",", 1)]
                    swapped = f"{first} {last}".strip()
                    canon_name = roster_canon_by_key.get(name_key(swapped))
                except Exception:
                    pass

            # High-confidence fuzzy match (only if it's very close)
            if not canon_name and roster:
                import difflib

                user_k = name_key(oa_name_input)
                keys = list(roster_canon_by_key.keys())
                best = difflib.get_close_matches(user_k, keys, n=1, cutoff=0.96)
                if best:
                    canon_name = roster_canon_by_key.get(best[0])

            # Approver aliases (allow approver access even if not in roster)
            if not canon_name:
                try:
                    ident = _approver_identity_key(oa_name_input)
                except Exception:
                    ident = None
                if ident:
                    # Map identity key -> canonical display name used throughout the app
                    display_by_ident = {
                        name_key("vraj patel"): "Vraj Patel",
                        name_key("kat brosvik"): "Kat Brosvik",
                        name_key("nile bernal"): "Nile Bernal",
                        name_key("barth andrew"): "Barth Andrew",
                        name_key("schutt jaden"): "Schutt Jaden",
                    }
                    canon_name = display_by_ident.get(ident)
                    approver_recognized = True

        # If still not found, show a clear message (no extra dropdown)
        if oa_name_input and not canon_name:
            if not roster:
                st.warning("Roster list is empty. Check the '(Names of hired OAs)' sheet and its Name column.")
            else:
                st.info("Name not found in roster. Please type your name exactly as it appears in the hired OA list (capitalization doesn't matter).")
                try:
                    import difflib

                    user_k = name_key(oa_name_input)
                    keys = list(roster_canon_by_key.keys())
                    close = difflib.get_close_matches(user_k, keys, n=5, cutoff=0.75)
                    if close:
                        st.caption("Closest matches in roster:")
                        for k in close:
                            st.write(f"• {roster_canon_by_key.get(k)}")
                except Exception:
                    pass

        # If we recognized an approver alias that is not in the hired OA roster,
        # give a helpful hint so users understand why their schedule may be blank.
        if oa_name_input and approver_recognized and canon_name and name_key(oa_name_input) not in roster_canon_by_key:
            st.caption("✅ Approver recognized (not in hired OA roster). You can unlock approver mode to review requests. Your schedule/hours may show as 0 if you're not on the roster.")

        if canon_name:
            try:
                canon_tmp = canon_name
                # Fast path: if a successful add/remove just occurred, we can
                # display the computed post-action hours immediately (no reads).
                ov = st.session_state.get("_HOURS_OVERRIDE")
                if (
                    isinstance(ov, dict)
                    and ov.get("user") == canon_tmp
                    and float(ov.get("expires", 0)) > time.time()
                ):
                    hours_now = float(ov.get("hours", 0.0))
                else:
                    # Reuse last computed value if it matches the current versions key.
                    last = st.session_state.get("_LAST_HOURS")
                    if isinstance(last, dict) and last.get("user") == canon_tmp and last.get("epoch") == epoch_key:
                        hours_now = float(last.get("hours", 0.0))
                    else:
                        hours_now = compute_hours_fast(ss, schedule_global, canon_tmp, epoch=epoch_key)

                # Save for fast post-write messaging.
                st.session_state["_LAST_HOURS"] = {"user": canon_tmp, "epoch": epoch_key, "hours": float(hours_now)}

                # Sidebar hours widget:
                # - The BIG number should match the schedule chart/table (raw scheduled hours from Sheets).
                # - If Supabase is configured, we also show an *adjusted* total after approved callouts/pickups.
                #   (Users found it confusing when the main number didn't match the schedule table.)
                ws, we = _week_bounds_la()
                scheduled_h = float(hours_now)
                callout_h = pickup_h = 0.0
                adjusted_h = scheduled_h
                if callouts_db.supabase_callouts_enabled() and pickups_db.supabase_pickups_enabled():
                    adj = _cached_weekly_supabase_adjustments(canon_tmp, str(ws), str(we))
                    callout_h = float(adj.get("callout_hours", 0.0))
                    pickup_h = float(adj.get("pickup_hours", 0.0))
                    adjusted_h = max(0.0, scheduled_h - callout_h + pickup_h)

                pct = min(max(scheduled_h / 20.0, 0.0), 1.0)
                def _fmt_md(d: date) -> str:
                    try:
                        return d.strftime('%b %d').replace(' 0', ' ')
                    except Exception:
                        return str(d)

                subtitle = f"Week: {_fmt_md(ws)}–{_fmt_md(we)}"
                st.markdown(
                    f"""
                    <div class='oa-hours-card oa-hours-card--classic'>
                      <div class='oa-hours-card__label'>
                        General hours <span class='oa-hours-card__scope'>(this week)</span>
                      </div>
                      <div class='oa-hours-card__valueBig'>
                        {scheduled_h:.1f} <span class='oa-hours-card__cap'>/ 20</span>
                      </div>
                      <div class='oa-hours-card__subline'>
                        Adjusted (callouts/pickups): <b>{adjusted_h:.1f}</b>
                        {'&nbsp;&nbsp;<span style="opacity:.75">(' + f"-{callout_h:.1f} callouts, +{pickup_h:.1f} pickups" + ')</span>' if (callout_h or pickup_h) else ''}
                      </div>
                      <div class='oa-hours-bar'>
                        <div class='oa-hours-bar__fill' style='width:{pct*100:.0f}%'></div>
                      </div>
                      <div class='oa-hours-card__hint'>{subtitle}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                if not (callouts_db.supabase_callouts_enabled() and pickups_db.supabase_pickups_enabled()):
                    st.caption("Cover/callout adjustments unavailable (Supabase not configured).")

                # Lightweight notifications about approval requests.
                try:
                    ver_map = st.session_state.get("WS_VER", {}) or {}
                    approvals_epoch = int(ver_map.get(config.APPROVAL_SHEET, 0))
                    approval_rows = cached_approval_table(ss.id, approvals_epoch) or []
                    my_rows = _requests_for_user(approval_rows, canon_tmp)
                    my_pending = sum(1 for r in my_rows if str(r.get("Status", "")).upper() == "PENDING")
                    if my_pending:
                        st.info(f"🕓 You have **{my_pending}** pending request(s).", icon="🔔")
                    else:
                        st.caption("No pending requests.")

                    if _is_approver(canon_tmp):
                        total_pending = sum(1 for r in approval_rows if str(r.get("Status", "")).upper() == "PENDING")
                        if total_pending:
                            st.warning(f"Approver inbox: **{total_pending}** pending request(s).", icon="📥")
                except Exception:
                    pass

                # Approver unlock (name-based allowlist)
                if _is_approver(canon_tmp):
                    if _approver_unlocked(canon_tmp):
                        st.success("Approver mode unlocked")
                        if st.button("🔒 Lock approver mode", key="btn_lock_approver"):
                            st.session_state.pop("APPROVER_AUTH", None)
                            st.session_state.pop("APPROVER_AUTH_FOR", None)
                            st.rerun()
                    else:
                        st.warning("Approver mode locked — enter password")
                        pw = st.text_input("Approver password", type="password", key="APPROVER_PW")
                        if st.button("Unlock", key="btn_unlock_approver"):
                            ident = _approver_identity_key(canon_tmp)
                            expected_global = str(st.secrets.get("APPROVER_PASSWORD", "")).strip()

                            # Temporary per-approver passwords (requested).
                            per_password = {
                                name_key("kat brosvik"): "change-me",
                                name_key("nile bernal"): "change-me",
                                name_key("barth andrew"): "change-me",
                                name_key("schutt jaden"): "change-me",
                            }

                            ok = False
                            if expected_global and pw == expected_global:
                                ok = True
                            elif ident and pw and pw == per_password.get(ident, ""):
                                ok = True
                            elif (not expected_global) and pw == "change-me":
                                # If no global password is configured, default to "change-me"
                                # for all approvers so local dev works out-of-the-box.
                                ok = True

                            if ok and ident:
                                st.session_state["APPROVER_AUTH"] = True
                                st.session_state["APPROVER_AUTH_FOR"] = ident
                                st.session_state.pop("APPROVER_PW", None)
                                st.rerun()
                            else:
                                st.error("Wrong password")
            except Exception as e:
                st.caption(f"Hours unavailable: {e}")

        # Expose canonical name for the main page logic below.
        st.session_state["CANON_USER"] = canon_name

        st.subheader("Roster tab (target)")
        tabs = list_tabs_for_sidebar(ss.id)
        if tabs:
            st.session_state["_LAST_TABS"] = tabs
        else:
            tabs = st.session_state.get("_LAST_TABS", [])

        # Keep selection stable even if tabs list momentarily fails to load.
        prev = st.session_state.get("active_sheet")
        idx = 0
        if prev and prev in tabs:
            idx = tabs.index(prev)

        # First run after adding a front-matter sheet (e.g., Policies) will
        # otherwise default to index 0 and crash schedule parsing. Prefer a
        # known schedule tab as the default selection.
        if (not prev) and tabs:
            for preferred in getattr(config, "OA_SCHEDULE_SHEETS", []) or []:
                if preferred in tabs:
                    idx = tabs.index(preferred)
                    break

        active_tab = st.selectbox("Select a tab", tabs, index=idx) if tabs else None
        st.session_state["active_sheet"] = active_tab

        col1, col2 = st.columns(2)
        with col1:
            if st.button("↻ Refresh tabs"):
                list_tabs_for_sidebar.clear()  # type: ignore[attr-defined]
                st.rerun()
        with col2:
            st.checkbox(
                "Hard clear",
                key="_HARD_CLEAR",
                help="If enabled, also clears Streamlit caches (forces Google Sheets to be re-read). Usually keep OFF for speed.",
            )
            if st.button("🧹 Clear caches"):
                # Fast clear: recompute derived UI (hours, rosters, availability) WITHOUT forcing
                # a full re-read of Google Sheets. This keeps the app responsive under concurrency.
                #
                # If you truly want to force a Sheets re-read, use the "↻ Refresh tabs" button
                # (or temporarily enable a hard-clear toggle below).

                # Clear sidebar fast-path hour values so hours recompute immediately
                st.session_state.pop("_LAST_HOURS", None)
                st.session_state.pop("_HOURS_OVERRIDE", None)

                # Optional: clear cached tab list snapshot (doesn't force Sheets read by itself)
                st.session_state.pop("_LAST_TABS", None)

                # Clear availability module caches
                clear_availability_caches()

                # Legacy bump (some modules still watch this)
                try:
                    invalidate_hours_caches()
                except Exception:
                    pass

                # Hard clear toggle (rarely needed): if set, clear Streamlit caches too.
                if st.session_state.get("_HARD_CLEAR", False):
                    st.cache_data.clear()
                    st.cache_resource.clear()
                    st.session_state.pop("WS_RANGE_CACHE", None)
                    st.session_state.pop("DAY_CACHE", None)

                st.rerun()
    # Main
    active_tab = st.session_state.get("active_sheet")
    canon_name = st.session_state.get("CANON_USER")

    # Always show the Pickup Tradeboard first (no-cover red callouts).
    # This is intentionally rendered even before the user types their name.
    _render_pickup_tradeboard(ss, schedule_global, canon_name)
    _render_people_working_now_panel(ss, epoch_key)

    # Global availability is useful but expensive; make it opt-in.
    # Render it *after* the pickup tradeboard so the pickup view is always first.
    if st.checkbox(
        "Show global availability (slower)",
        value=bool(st.session_state.get("SHOW_GLOBAL_AVAIL", False)),
        key="SHOW_GLOBAL_AVAIL",
    ):
        render_global_availability(st, ss, epoch_key)

    if not (active_tab and canon_name):
        if active_tab:
            if re.search(r"\bon\s*[- ]?call\b", active_tab, flags=re.I):
                ui_peek.peek_oncall(ss)
            else:
                ui_peek.peek_exact(schedule_global, [active_tab])
        else:
            st.info("Select a roster tab on the left to peek.")
        return

    approver_mode = _approver_unlocked(canon_name)

    def _approval_epoch() -> int:
        ver_map = st.session_state.get("WS_VER", {}) or {}
        # In Supabase mode there is no worksheet version bump, so we also
        # include a lightweight UI epoch that we bump after approve/reject.
        return int(ver_map.get(config.APPROVAL_SHEET, 0)) + int(st.session_state.get("APPROVALS_EPOCH", 0))

    def render_pending_actions() -> None:
        # Anchor for scroll-to behavior after approve/reject.
        st.markdown('<div id="pending-actions"></div>', unsafe_allow_html=True)
        st.markdown("### Pending Actions")
        if st.session_state.pop("_SCROLL_TO_PENDING", False):
            components.html(
                """<script>
                const el = window.parent.document.getElementById('pending-actions');
                if (el) { el.scrollIntoView({behavior:'instant', block:'start'}); }
                </script>""",
                height=0,
            )

        st.caption("Approver inbox — approve or reject requests. History shows past decisions.")

        top_l, top_r = st.columns([1, 1])
        with top_l:
            if st.button("↻ Refresh", key="btn_refresh_pending"):
                try:
                    cached_approval_table.clear()  # type: ignore[attr-defined]
                except Exception:
                    pass
                st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                st.session_state["_SCROLL_TO_PENDING"] = True
                st.rerun()

        # Admin-only manual sync for swap sections (rendered from Supabase).
        # Shown in approver mode. This does NOT run automatically on reruns.
        if approver_mode:
            with top_r:
                st.caption("Admin")
                try:
                    from ..integrations.supabase_io import supabase_enabled

                    st.caption("Supabase: ✅" if supabase_enabled() else "Supabase: ❌ (set SUPABASE_URL / SUPABASE_KEY in secrets)")
                except Exception:
                    pass
                # Simple throttling to avoid accidental repeated clicks on reruns.
                last = st.session_state.get("_LAST_SWAP_SYNC_TS")
                now_ts = __import__("time").time()
                cooldown = 60
                can_run = not last or (now_ts - float(last)) > cooldown
                if st.button("🔁 Sync swaps now", disabled=not can_run, key="btn_sync_swaps"):
                    from ..integrations.supabase_io import supabase_enabled, get_supabase
                    from ..jobs.sync_swaps_to_sheets import sync_swaps_to_sheets

                    if not supabase_enabled():
                        st.warning("Supabase not configured — cannot sync swaps.")
                    else:
                        try:
                            sb = get_supabase()
                            res = sync_swaps_to_sheets(ss, sb, apply_grid_colors=True)
                            st.session_state["_LAST_SWAP_SYNC_TS"] = now_ts
                            sheet_errs = res.get("sheet_errors") or []
                            grid_errs = res.get("grid_errors") or []
                            msg = (
                                f"Synced swaps. Weekly written: {res.get('weekly_written')} • "
                                f"Future written: {res.get('future_written')} • "
                                f"Sheets: {', '.join(res.get('sheets_updated', []) or [])}"
                            )
                            if sheet_errs:
                                msg += f" • Errors: {len(sheet_errs)}"
                            if grid_errs:
                                msg += f" • Grid issues: {len(grid_errs)}"
                            st.success(msg)
                            if sheet_errs:
                                st.caption("First error:")
                                st.code(str(sheet_errs[0]))
                            if grid_errs:
                                st.caption("First grid issue:")
                                st.code(str(grid_errs[0]))
                        except Exception as e:
                            st.error(f"Swap sync failed: {str(e)}")

                if not can_run:
                    st.caption("Synced recently — try again in a minute.")
        with top_r:
            history_rows = st.selectbox(
                "History depth",
                options=[200, 500, 1000],
                index=1,
                help="How many rows to read from the approvals sheet (more = slower).",
                key="approvals_history_depth",
            )

        try:
            rows_all = _sort_requests_newest(
                cached_approval_table(ss.id, _approval_epoch(), max_rows=int(history_rows))
            )
        except Exception as e:
            if maybe_show_recovery_popup(e, where="loading approvals inbox"):
                return
            rows_all = []
        pending = [r for r in rows_all if str(r.get("Status", "")).upper() == "PENDING"]
        history = [r for r in rows_all if str(r.get("Status", "")).upper() != "PENDING"]
        late_callouts: list[dict] = []
        if callouts_db.supabase_callouts_enabled():
            ws_notice, we_notice = _week_bounds_la()
            late_callouts = _cached_late_notice_callouts(str(ws_notice), str(we_notice))

        pending_ot = [r for r in pending if _is_overtime_request(r)]
        pending_regular = [r for r in pending if not _is_overtime_request(r)]
        st.caption(
            f"Pending: {len(pending_regular)} • Pending OT: {len(pending_ot)} • "
            f"Approved: {sum(1 for r in history if str(r.get('Status', '')).upper() == 'APPROVED')} • "
            f"Rejected: {sum(1 for r in history if str(r.get('Status', '')).upper() == 'REJECTED')}"
        )

        with st.expander(f"📣 Late callout notices ({len(late_callouts)})", expanded=bool(late_callouts)):
            st.caption(
                "Sick callouts submitted under 2 hours before shift and all other callouts submitted under 48 hours before shift."
            )
            if not callouts_db.supabase_callouts_enabled():
                st.info("Callout notice alerts are unavailable because Supabase is not configured.")
            elif not late_callouts:
                st.success("No late-notice callouts this week.")
            else:
                def _fmt_shift_time(ts: str) -> str:
                    dt = _parse_iso_dt(ts)
                    if not dt:
                        return ""
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
                    return dt.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%I:%M %p").lstrip("0")

                alert_table = [
                    {
                        "Caller": row.get("caller_name", ""),
                        "Campus": row.get("campus", ""),
                        "Date": row.get("event_date", ""),
                        "Shift": f"{_fmt_shift_time(str(row.get('shift_start_at') or ''))}-{_fmt_shift_time(str(row.get('shift_end_at') or ''))}",
                        "Reason": _format_callout_reason(str(row.get("reason", "") or "")),
                        "Notice (hrs)": f"{float(row.get('notice_hours') or 0.0):.2f}",
                        "Rule": row.get("late_notice_rule", ""),
                    }
                    for row in late_callouts
                ]
                st.dataframe(alert_table, hide_index=True, use_container_width=True)

        tab_inbox, tab_ot, tab_hist = st.tabs(
            [f"📥 Inbox ({len(pending_regular)})", f"⏱️ Overtime ({len(pending_ot)})", f"🗂️ History ({len(history)})"]
        )

        def _apply_request(req: dict) -> str:
            action = str(req.get("Action", "")).strip().lower()
            requester = str(req.get("Requester", "")).strip()
            campus = str(req.get("Campus", "")).strip()
            day = str(req.get("Day", "")).strip()
            start_s = str(req.get("Start", "")).strip()
            end_s = str(req.get("End", "")).strip()
            details = str(req.get("Details", "")).strip()

            meta, details_rest = _extract_details_meta(details)

            day_canon = re.sub(r"[^a-z]", "", day.lower())
            sdt = _parse_12h_time(start_s)
            edt = _parse_12h_time(end_s)

            # IMPORTANT: Approving a request must execute against the worksheet
            # implied by the request itself — not whatever the approver currently
            # has selected in the sidebar. We resolve the stored campus value to
            # an actual visible worksheet title (supports aliases like "MC").
            campus_ws_title = _resolve_ws_title_from_meta(
                ss,
                schedule_global,
                campus_fallback=campus,
                details=details,
            )

            if action == "add":
                return chat_add.handle_add(
                    st,
                    ss,
                    schedule_global,
                    actor_name=canon_name,
                    canon_target_name=requester,
                    campus_title=campus_ws_title,
                    day=day_canon,
                    start=sdt.time(),
                    end=edt.time(),
                )
            if action == "remove":
                return chat_remove.handle_remove(
                    st,
                    ss,
                    schedule_global,
                    canon_target_name=requester,
                    campus_title=campus_ws_title,
                    day=day_canon,
                    start=sdt.time(),
                    end=edt.time(),
                )
            if action == "callout":
                # Per policy: callouts proceed directly and are always logged as "no cover".
                # Coverage is represented by a separate PICKUP entry.

                campus_key = utils.normalize_campus(campus_ws_title, campus)
                kv = _parse_details_kv(details_rest)
                reason = kv.get("reason")

                # Event date derivation:
                ds = (kv.get("date") or "").strip()
                if ds:
                    event_d = date.fromisoformat(ds)
                elif campus_key in {"UNH", "MC"}:
                    event_d = _date_for_weekday_in_sheet(ss, campus_ws_title, day_canon)
                    if not event_d:
                        ws0, _ = _week_bounds_la()
                        want = utils.normalize_day(day_canon)
                        event_d = ws0
                        for i in range(7):
                            cand = ws0 + timedelta(days=i)
                            if utils.normalize_day(cand.strftime("%A")) == want:
                                event_d = cand
                                break
                else:
                    event_d = _oncall_event_date(campus_ws_title or meta.get("sheet_title", ""), day_canon)
                    if not event_d:
                        raise ValueError("Could not derive On-Call event date from sheet title")

                # Keep the existing current-week rule for MC/UNH, but allow
                # On-Call week tabs to color their own schedule grid immediately.
                should_color_now = _should_color_schedule_now(campus_key=campus_key, event_d=event_d)
                if getattr(config, "SUMMER_MODE", False):
                    should_color_now = True

                if should_color_now:
                    msg = chat_callout.handle_callout(
                        st,
                        ss,
                        schedule_global,
                        canon_target_name=requester,
                        campus_title=campus_ws_title,
                        day=day_canon,
                        start=sdt.time(),
                        end=edt.time(),
                        covered_by=None,
                    )
                else:
                    msg = "Logged future callout (no schedule color change)."

                # Upsert into Supabase callouts table (idempotent). Supabase is the source of truth.
                start_at = _combine_date_time_la(event_d, sdt.time())
                end_at = _combine_date_time_la(event_d, edt.time())
                if end_at <= start_at:
                    end_at = end_at + timedelta(days=1)

                db_ok = False
                try:
                    if callouts_db.supabase_callouts_enabled():
                        approval_id = str(req.get("ID", "")).strip()
                        created_at = _ensure_la_timestamptz(req.get("Created", ""))
                        callouts_db.upsert_callout(
                            {
                                "approval_id": approval_id,
                                "submitted_at": created_at,
                                "campus": "ONCALL" if campus_key not in {"UNH", "MC"} else campus_key,
                                "caller_name": requester,
                                "reason": reason,
                                "event_date": str(event_d),
                                "shift_start_at": start_at.isoformat(timespec="seconds"),
                                "shift_end_at": end_at.isoformat(timespec="seconds"),
                                "duration_hours": round(_duration_hours_between(start_at, end_at), 4),
                            }
                        )
                        db_ok = True
                except Exception as e:
                    # Do not fail the approval if DB write fails.
                    try:
                        append_audit(
                            ss,
                            actor=canon_name,
                            action="db_callout_upsert_failed",
                            campus=campus_ws_title,
                            day=day,
                            start=start_s,
                            end=end_s,
                            details=str(e),
                        )
                    except Exception:
                        pass
                    st.warning(f"Sheets updated, but callout DB sync failed: {_strip_debug_blob(str(e))}")

                # Render swap sections from Supabase (idempotent).
                if db_ok:
                    try:
                        from ..integrations.supabase_io import get_supabase
                        from ..jobs.sync_swaps_to_sheets import sync_swaps_to_sheets

                        sb = get_supabase()
                        # Sync only the worksheet the request was submitted from.
                        # For On-Call, also allow a single-sheet recolor pass so the
                        # exact future week tab is corrected even if the direct grid
                        # write missed for any reason.
                        # Prefer the already-loaded worksheet object (avoids extra Sheets read calls).
                        ws_obj = None
                        try:
                            if hasattr(schedule_global, "_load_ws_map"):
                                schedule_global._load_ws_map()  # type: ignore[attr-defined]
                                ws_obj = getattr(schedule_global, "_ws_map", {}).get(campus_ws_title)
                        except Exception:
                            ws_obj = None

                        res = sync_swaps_to_sheets(
                            ss,
                            sb,
                            worksheet=ws_obj,
                            sheet_title=campus_ws_title,
                            apply_grid_colors=True,
                        )
                        sheet_errs = res.get("sheet_errors") or []
                        grid_errs = res.get("grid_errors") or []
                        if sheet_errs:
                            st.warning(f"Swap section sync had errors. First error: {sheet_errs[0]}")
                        if grid_errs:
                            st.warning(f"Swap grid sync had issues. First issue: {grid_errs[0]}")
                    except Exception as e:
                        st.warning(f"Callout recorded, but swap section sync failed: {_strip_debug_blob(str(e))}")
                return msg
            if action == "pickup":
                # A pickup request is a *cover* for someone else's callout.
                # Requester = picker, Details carries the callout target.
                m = re.search(r"\btarget\s*=\s*([^|]+)", details_rest, flags=re.I)
                if not m:
                    raise ValueError("Pickup request missing Details: target=<called-out OA>")
                target = m.group(1).strip()
                campus_key = utils.normalize_campus(campus_ws_title, campus)
                kv = _parse_details_kv(details_rest)

                # Event date derivation:
                ds = (kv.get("date") or "").strip()
                if ds:
                    event_d = date.fromisoformat(ds)
                elif campus_key in {"UNH", "MC"}:
                    event_d = _date_for_weekday_in_sheet(ss, campus_ws_title, day_canon)
                    if not event_d:
                        ws0, _ = _week_bounds_la()
                        want = utils.normalize_day(day_canon)
                        event_d = ws0
                        for i in range(7):
                            cand = ws0 + timedelta(days=i)
                            if utils.normalize_day(cand.strftime("%A")) == want:
                                event_d = cand
                                break
                else:
                    event_d = _oncall_event_date(campus_ws_title or meta.get("sheet_title", ""), day_canon)
                    if not event_d:
                        raise ValueError("Could not derive On-Call event date from sheet title")

                should_color_now = _should_color_schedule_now(campus_key=campus_key, event_d=event_d)

                if should_color_now:
                    msg = chat_callout.handle_callout(
                        st,
                        ss,
                        schedule_global,
                        canon_target_name=target,
                        campus_title=campus_ws_title,
                        day=day_canon,
                        start=sdt.time(),
                        end=edt.time(),
                        covered_by=requester,
                    )
                else:
                    msg = "Logged future pickup (no schedule color change)."

                # Upsert into Supabase pickups table (idempotent). Supabase is the source of truth.
                start_at = _combine_date_time_la(event_d, sdt.time())
                end_at = _combine_date_time_la(event_d, edt.time())
                if end_at <= start_at:
                    end_at = end_at + timedelta(days=1)

                db_ok = False
                try:
                    if pickups_db.supabase_pickups_enabled():
                        approval_id = str(req.get("ID", "")).strip()
                        created_at = _ensure_la_timestamptz(req.get("Created", ""))
                        pickups_db.upsert_pickup(
                            {
                                "approval_id": approval_id,
                                "submitted_at": created_at,
                                "campus": "ONCALL" if campus_key not in {"UNH", "MC"} else campus_key,
                                "event_date": str(event_d),
                                "shift_start_at": start_at.isoformat(timespec="seconds"),
                                "shift_end_at": end_at.isoformat(timespec="seconds"),
                                "duration_hours": round(_duration_hours_between(start_at, end_at), 4),
                                "picker_name": requester,
                                "target_name": target,
                                "note": kv.get("note"),
                            }
                        )
                        db_ok = True
                except Exception as e:
                    try:
                        append_audit(
                            ss,
                            actor=canon_name,
                            action="db_pickup_upsert_failed",
                            campus=campus_ws_title,
                            day=day,
                            start=start_s,
                            end=end_s,
                            details=str(e),
                        )
                    except Exception:
                        pass
                    st.warning(f"Sheets updated, but pickup DB sync failed: {_strip_debug_blob(str(e))}")

                # Render swap sections from Supabase (idempotent).
                if db_ok:
                    try:
                        from ..integrations.supabase_io import get_supabase
                        from ..jobs.sync_swaps_to_sheets import sync_swaps_to_sheets

                        sb = get_supabase()
                        # Prefer the already-loaded worksheet object (avoids extra Sheets read calls).
                        ws_obj = None
                        try:
                            if hasattr(schedule_global, "_load_ws_map"):
                                schedule_global._load_ws_map()  # type: ignore[attr-defined]
                                ws_obj = getattr(schedule_global, "_ws_map", {}).get(campus_ws_title)
                        except Exception:
                            ws_obj = None

                        res = sync_swaps_to_sheets(
                            ss,
                            sb,
                            worksheet=ws_obj,
                            sheet_title=campus_ws_title,
                            apply_grid_colors=True,
                        )
                        sheet_errs = res.get("sheet_errors") or []
                        grid_errs = res.get("grid_errors") or []
                        if sheet_errs:
                            st.warning(f"Swap section sync had errors. First error: {sheet_errs[0]}")
                        if grid_errs:
                            st.warning(f"Swap grid sync had issues. First issue: {grid_errs[0]}")
                    except Exception as e:
                        st.warning(f"Pickup recorded, but swap section sync failed: {_strip_debug_blob(str(e))}")
                return msg
            raise ValueError(f"Unknown action: {action}")

        def _render_pending_tab(pending_rows: list[dict], *, key_prefix: str, empty_msg: str) -> None:
            if not pending_rows:
                st.success(empty_msg)
                return

            q = st.text_input(
                "Search",
                placeholder="name / campus / day / action",
                key=f"{key_prefix}.search",
            )
            if q:
                ql = q.strip().lower()
                pending_view = [
                    r
                    for r in pending_rows
                    if ql in str(r.get("Requester", "")).lower()
                    or ql in str(r.get("Campus", "")).lower()
                    or ql in str(r.get("Day", "")).lower()
                    or ql in str(r.get("Action", "")).lower()
                    or ql in str(r.get("Details", "")).lower()
                ]
            else:
                pending_view = pending_rows

            labels = [
                f"{r.get('Requester','')} · {str(r.get('Action','')).upper()} · {r.get('Campus','')} · {r.get('Day','')} {r.get('Start','')}–{r.get('End','')}"
                for r in pending_view
            ]
            pick = st.selectbox("Select a request", options=labels, index=0, key=f"{key_prefix}.pick")
            req = pending_view[labels.index(pick)]

            st.markdown(f"#### {str(req.get('Action','')).upper()} — {req.get('Requester','')}")
            st.markdown(
                f"**Campus:** {req.get('Campus','')}  \n"
                f"**When:** {req.get('Day','')} {req.get('Start','')}–{req.get('End','')}  \n"
                f"**Status:** {_status_chip(str(req.get('Status','')))}"
            )

            det = str(req.get("Details", "") or "")
            meta, det_rest = _extract_details_meta(det)
            if meta.get("sheet_title"):
                st.caption(f"Sheet: {meta.get('sheet_title')}")
            det_display = _format_request_details_for_display(det)
            if det_display:
                st.info(det_display)

            note = st.text_input("Note (optional)", key=f"{key_prefix}.note")
            c1, c2 = st.columns(2)
            disabled = str(req.get("Status", "")).upper() != "PENDING"

            with c1:
                if st.button(
                    "✅ Approve",
                    type="primary",
                    use_container_width=True,
                    key=f"{key_prefix}.approve",
                    disabled=disabled,
                ):
                    try:
                        req_row = int(req.get("_row", 0))
                        req_id = str(req.get("ID", ""))
                        fresh_req = get_approval_request(ss, row=req_row, req_id=req_id) or req
                        fresh_status = str(fresh_req.get("Status", "")).strip().upper()
                        if fresh_status != "PENDING":
                            _flash("info", f"This request is already {fresh_status or 'processed'}.")
                            st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                            st.session_state["_SCROLL_TO_PENDING"] = True
                            st.rerun()

                        with st.spinner("Applying request..."):
                            msg = _apply_request(fresh_req)

                        try:
                            set_approval_status(
                                ss,
                                row=req_row,
                                req_id=req_id,
                                status="APPROVED",
                                reviewed_by=canon_name,
                                note=note,
                            )
                        except Exception:
                            latest = get_approval_request(ss, row=req_row, req_id=req_id) or {}
                            latest_status = str(latest.get("Status", "")).strip().upper()
                            if latest_status != "APPROVED":
                                _flash(
                                    "error",
                                    "The schedule change was applied, but the approval record could not be updated. "
                                    "Refresh once before trying again.",
                                )
                                st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                                st.session_state["_SCROLL_TO_PENDING"] = True
                                st.rerun()

                        _flash("success", f"✅ Approved. {str(msg).strip()}")
                        st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                        st.session_state["_SCROLL_TO_PENDING"] = True
                        # Reset selection so the just-approved request doesn't linger in the selectbox state.
                        for _k in (f"{key_prefix}.pick", f"{key_prefix}.note"):
                            if _k in st.session_state:
                                del st.session_state[_k]
                        st.rerun()
                    except Exception as e:
                        if maybe_show_recovery_popup(e, where="approving request"):
                            return
                        err = _strip_debug_blob(str(e))
                        try:
                            set_approval_status(
                                ss,
                                row=int(req.get("_row", 0)),
                                req_id=str(req.get("ID", "")),
                                status="FAILED",
                                reviewed_by=canon_name,
                                note=note or "",
                                error_message=err,
                            )
                        except Exception:
                            pass
                        _flash("error", err)
                        st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                        st.session_state["_SCROLL_TO_PENDING"] = True
                        st.rerun()

            with c2:
                if st.button(
                    "❌ Reject",
                    use_container_width=True,
                    key=f"{key_prefix}.reject",
                    disabled=disabled,
                ):
                    set_approval_status(
                        ss,
                        row=int(req.get("_row", 0)),
                        req_id=str(req.get("ID", "")),
                        status="REJECTED",
                        reviewed_by=canon_name,
                        note=note,
                    )
                    _flash("info", "Rejected.")
                    st.session_state["APPROVALS_EPOCH"] = st.session_state.get("APPROVALS_EPOCH", 0) + 1
                    st.session_state["_SCROLL_TO_PENDING"] = True
                    for _k in (f"{key_prefix}.pick", f"{key_prefix}.note"):
                        if _k in st.session_state:
                            del st.session_state[_k]
                    st.rerun()

            st.markdown("---")
            st.markdown("**Pending list**")
            table = [
                {
                    "Created": r.get("Created", ""),
                    "ID": r.get("ID", ""),
                    "Requester": r.get("Requester", ""),
                    "Action": str(r.get("Action", "")).upper(),
                    "Campus": r.get("Campus", ""),
                    "Day": r.get("Day", ""),
                    "Start": r.get("Start", ""),
                    "End": r.get("End", ""),
                }
                for r in pending_view[:200]
            ]
            st.dataframe(table, hide_index=True, use_container_width=True)

        with tab_inbox:
            _render_pending_tab(pending_regular, key_prefix="appr.inbox", empty_msg="No pending requests 🎉")

        with tab_ot:
            _render_pending_tab(pending_ot, key_prefix="appr.ot", empty_msg="No overtime requests 🎉")

        with tab_hist:
            if not history:
                st.info("No approved/rejected history yet.")
            else:
                statuses = st.multiselect(
                    "Statuses",
                    options=["APPROVED", "REJECTED", "FAILED"],
                    default=["APPROVED", "REJECTED", "FAILED"],
                    key="hist_statuses",
                )
                q2 = st.text_input("Search", placeholder="name / campus / day / action", key="hist_search")
                hist_view = history
                if statuses:
                    allow = set(s.upper() for s in statuses)
                    hist_view = [r for r in hist_view if str(r.get("Status", "")).upper() in allow]
                if q2:
                    ql = q2.strip().lower()
                    hist_view = [
                        r
                        for r in hist_view
                        if ql in str(r.get("Requester", "")).lower()
                        or ql in str(r.get("Campus", "")).lower()
                        or ql in str(r.get("Day", "")).lower()
                        or ql in str(r.get("Action", "")).lower()
                        or ql in str(r.get("ReviewedBy", "")).lower()
                    ]
                table = [
                    {
                        "Created": r.get("Created", ""),
                        "Requester": r.get("Requester", ""),
                        "Action": str(r.get("Action", "")).upper(),
                        "Campus": r.get("Campus", ""),
                        "Day": r.get("Day", ""),
                        "Start": r.get("Start", ""),
                        "End": r.get("End", ""),
                        "Status": _status_chip(str(r.get("Status", ""))),
                        "ReviewedBy": r.get("ReviewedBy", ""),
                        "ReviewedAt": r.get("ReviewedAt", ""),
                        "Note": r.get("ReviewNote", ""),
                    }
                    for r in hist_view[:300]
                ]
                st.dataframe(table, hide_index=True, use_container_width=True)


    def render_my_requests() -> None:
        st.markdown("### My Requests")
        st.caption("Track your submitted requests (pending + approved/rejected).")

        if st.button("↻ Refresh", key="btn_refresh_myreq"):
            try:
                cached_approval_table.clear()  # type: ignore[attr-defined]
            except Exception:
                pass
            st.rerun()

        try:
            rows_all = _sort_requests_newest(
                cached_approval_table(ss.id, _approval_epoch(), max_rows=500)
            )
        except Exception as e:
            if maybe_show_recovery_popup(e, where="loading my requests"):
                return
            rows_all = []
        mine = _requests_for_user(rows_all, canon_name)
        pending = [r for r in mine if str(r.get("Status", "")).upper() == "PENDING"]
        history = [r for r in mine if str(r.get("Status", "")).upper() != "PENDING"]
        st.caption(f"Pending: {len(pending)} • Decisions: {len(history)}")

        t1, t2 = st.tabs([f"🕓 Pending ({len(pending)})", f"✅ History ({len(history)})"])
        with t1:
            if not pending:
                st.info("No pending requests.")
            else:
                table = [
                    {
                        "Created": r.get("Created", ""),
                        "Action": str(r.get("Action", "")).upper(),
                        "Campus": r.get("Campus", ""),
                        "Day": r.get("Day", ""),
                        "Start": r.get("Start", ""),
                        "End": r.get("End", ""),
                        "Status": _status_chip(str(r.get("Status", ""))),
                        "Details": _format_request_details_for_display(str(r.get("Details", "") or "")),
                    }
                    for r in pending[:200]
                ]
                st.dataframe(table, hide_index=True, use_container_width=True)

        with t2:
            if not history:
                st.info("No approved/rejected requests yet.")
            else:
                table = [
                    {
                        "Created": r.get("Created", ""),
                        "Action": str(r.get("Action", "")).upper(),
                        "Campus": r.get("Campus", ""),
                        "Day": r.get("Day", ""),
                        "Start": r.get("Start", ""),
                        "End": r.get("End", ""),
                        "Status": _status_chip(str(r.get("Status", ""))),
                        "ReviewedBy": r.get("ReviewedBy", ""),
                        "ReviewedAt": r.get("ReviewedAt", ""),
                        "Note": r.get("ReviewNote", ""),
                    }
                    for r in history[:300]
                ]
                st.dataframe(table, hide_index=True, use_container_width=True)

    def render_scheduler() -> None:
        force_approval = not _approver_unlocked(canon_name)
        st.subheader("Action")
        a1, a2, a3 = st.columns(3)
        with a1:
            if st.button("➕ Add shift", use_container_width=True, key="btn_add"):
                st.session_state["mode"] = "add"
                st.session_state["sched_expanded"] = False
        with a2:
            if st.button("🧹 Remove shift", use_container_width=True, key="btn_remove"):
                st.session_state["mode"] = "remove"
                st.session_state["sched_expanded"] = False
        with a3:
            if st.button("📣 Call-Out", use_container_width=True, key="btn_callout"):
                st.session_state["mode"] = "callout"
                st.session_state["sched_expanded"] = False

        mode = st.session_state.get("mode")

        # Per-tab availability panel is helpful but expensive (it computes all 7 days).
        # Keep it opt-in; the guided add flow below still shows availability for the selected day.
        ver_map = st.session_state.get("WS_VER", {}) or {}
        tab_epoch = int(ver_map.get(active_tab, 0))
        if st.checkbox("Show availability for this tab (slower)", value=False, key="SHOW_TAB_AVAIL"):
            render_availability_expander(st, ss.id, active_tab, tab_epoch)

        # Schedule viz
        try:
            user_sched_cached = cached_user_schedule(ss.id, canon_name, epoch_key)
            df_cached = cached_schedule_df(user_sched_cached, epoch_key)
            with st.expander("📊 Your Schedule (This Week)", expanded=st.session_state["sched_expanded"]):
                schedule_query.render_schedule_viz(st, df_cached, title=f"{canon_name} — This Week")
                schedule_query.render_schedule_dataframe(st, df_cached)
        except Exception as e:
            st.info(f"Could not render schedule: {_strip_debug_blob(str(e))}")

        # Guided add
        if mode == "add":
            st.markdown("### Add shift — Guided")
            kind = campus_kind(active_tab)
            days_all = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            day_pool = weekday_filter(days_all, active_tab)

            day_choice = st.radio(
                "Step A — Pick a day", options=[d.title() for d in day_pool], horizontal=True, key="add_day_radio"
            )
            day_canon = day_choice.lower()

            st.markdown("**Step B — Available**")
            avail_today = cached_available_ranges_for_day(ss.id, active_tab, day_canon, tab_epoch)
            human_ranges = [_format_pair_12h(s, e) for (s, e) in _iter_pairs(avail_today)]
            st.write(", ".join(f"`{x}`" for x in human_ranges) if human_ranges else "_none_")

            chosen = None
            if kind == "ONCALL":
                st.markdown("**Step C — Pick a block (full slot)**")
                if not avail_today:
                    st.info("No On-Call blocks available on this day.")
                else:
                    avail_pairs = _iter_pairs(avail_today)
                    labels = [_format_pair_12h(s, e) for (s, e) in avail_pairs]
                    pick = st.selectbox("Available On-Call blocks", options=labels, index=0)
                    idx = labels.index(pick)
                    s24, e24 = avail_pairs[idx]
                    chosen = (datetime.strptime(s24, "%H:%M"), datetime.strptime(e24, "%H:%M"))
            else:
                try:
                    user_sched_all = cached_user_schedule(ss.id, canon_name, epoch_key)
                except Exception:
                    user_sched_all = {}

                booked_today_min = _mins_for_day(user_sched_all, day_canon)
                open_capacity_min = sum(
                    int((datetime.strptime(e, "%H:%M") - datetime.strptime(s, "%H:%M")).total_seconds() // 60)
                    for (s, e) in _iter_pairs(avail_today)
                )
                remaining_today_min = max(0, min(8 * 60 - booked_today_min, open_capacity_min))

                st.markdown("**Step C — How many hours?**")
                if remaining_today_min < 30:
                    st.warning("No contiguous space or 8h/day cap reached for this day.")
                    requested_minutes = 0
                else:
                    increments = list(range(30, remaining_today_min + 1, 30))
                    default_idx = min(3, len(increments) - 1)
                    label_map = {m: (f"{m // 60}h {m % 60}m" if m % 60 else f"{m // 60}h") for m in increments}
                    req_label = st.selectbox(
                        "30-minute steps (auto-capped by today’s limit & open space)",
                        options=[label_map[m] for m in increments],
                        index=default_idx,
                    )
                    requested_minutes = {v: k for k, v in label_map.items()}[req_label]

                st.caption(
                    f"Remaining today: **{remaining_today_min // 60}h {remaining_today_min % 60}m** "
                    f"(cap 8h; already booked {booked_today_min // 60}h {booked_today_min % 60}m)"
                )

                st.markdown("**Step D — Pick an exact window**")
                exact_windows_24h = (
                    enumerate_exact_length_windows(_iter_pairs(avail_today), requested_minutes)
                    if remaining_today_min >= 30
                    else []
                )
                if not exact_windows_24h:
                    st.info("No contiguous window of that exact length. Try a different duration or day.")
                else:
                    labels = [_format_pair_12h(s, e) for (s, e) in _iter_pairs(exact_windows_24h)]
                    pick = st.selectbox("Available options", options=labels, index=0)
                    idx = labels.index(pick)
                    s24, e24 = exact_windows_24h[idx]
                    chosen = (datetime.strptime(s24, "%H:%M"), datetime.strptime(e24, "%H:%M"))

            want_approval = st.checkbox(
                "Send for approval",
                value=True if force_approval else False,
                disabled=force_approval,
            )
            if chosen and st.button("Submit"):
                try:
                    if want_approval or force_approval:
                        # Preflight validations so we fail fast (no waiting for approver).
                        try:
                            user_sched_all_pf = cached_user_schedule(ss.id, canon_name, epoch_key)
                        except Exception:
                            user_sched_all_pf = {}

                        dur_min = int((chosen[1] - chosen[0]).total_seconds() // 60)
                        if dur_min <= 0:
                            dur_min = int(((chosen[1] + timedelta(days=1)) - chosen[0]).total_seconds() // 60)
                        delta_h = dur_min / 60.0

                        # Minimum consecutive work rule (1h 30m). The new add window may be shorter,
                        # but the consecutive *block* it participates in must be >= 90 minutes.
                        if kind != "ONCALL":
                            existing_intervals_pf = _day_intervals(user_sched_all_pf, day_canon)
                            min_ok_pf, block_mins_pf = labor_rules.consecutive_block_minutes_for(
                                existing_intervals_pf,
                                (chosen[0], chosen[1]),
                                min_consecutive_mins=90,
                            )
                            if not min_ok_pf:
                                block_lbl_pf = (
                                    f"{int(block_mins_pf)} min"
                                    if int(block_mins_pf) < 60
                                    else f"{int(block_mins_pf)//60} hr {int(block_mins_pf)%60:02d} min"
                                )
                                st.error(
                                    "Minimum shift length: you must have **at least 1 hr 30 min** of consecutive work. "
                                    f"This add would give you only **{block_lbl_pf}** consecutive. "
                                    "Add a longer window, or add a slot that touches one of your existing shifts so the total consecutive block is ≥ 1h 30m."
                                )
                                return

                        # Weekly cap (20h)
                        hours_now = float(st.session_state.get("_LAST_HOURS", {}).get("hours", 0.0))
                        try:
                            last = st.session_state.get("_LAST_HOURS") or {}
                            if last.get("user") != canon_name or last.get("epoch") != epoch_key:
                                hours_now = float(compute_hours_fast(ss, schedule_global, canon_name, epoch=epoch_key))
                        except Exception:
                            pass
                        if hours_now + delta_h > 20.0:
                            raise ValueError(
                                f"More than 20 hours: you have {hours_now:.1f}h; request is {delta_h:.1f}h."
                            )

                        # Daily cap (8h)
                        booked_today = _mins_for_day(user_sched_all_pf, day_canon)
                        if booked_today + dur_min > 8 * 60:
                            raise ValueError(
                                f"Daily cap exceeded on {day_canon.title()}: already booked "
                                f"{booked_today // 60}h {booked_today % 60}m; request is "
                                f"{dur_min // 60}h {dur_min % 60}m."
                            )

                        # UNH/MC overlap check (prevents double-booking and duplicates).
                        target_bucket = {"UNH": "UNH", "MC": "MC"}.get(kind)
                        if target_bucket:
                            conflict = _find_unh_mc_conflict(user_sched_all_pf, day_canon, target_bucket, chosen[0], chosen[1])
                            if conflict:
                                raise ValueError(conflict)

                        with st.spinner("Submitting request..."):
                            rid = submit_approval_request(
                                ss,
                                requester=canon_name,
                                action="add",
                                campus=("ONCALL" if kind == "ONCALL" else kind),
                                day=day_canon.title(),
                                start=fmt_time(chosen[0]),
                                end=fmt_time(chosen[1]),
                                details=_attach_details_meta(
                                    details="requested",
                                    campus_key=("ONCALL" if kind == "ONCALL" else kind),
                                    sheet_title=active_tab,
                                    sheet_gid=_sheet_gid_for_title(schedule_global, active_tab),
                                ),
                            )
                        st.session_state["mode"] = None
                        st.session_state["sched_expanded"] = True
                        _flash("success", f"🕓 Submitted for approval (id {rid}).")
                        st.rerun()

                    msg = chat_add.handle_add(
                        st,
                        ss,
                        schedule_global,
                        actor_name=canon_name,
                        canon_target_name=canon_name,
                        campus_title=active_tab,
                        day=day_canon,
                        start=chosen[0].time(),
                        end=chosen[1].time(),
                    )

                    try:
                        append_audit(
                            ss,
                            actor=canon_name,
                            action="add",
                            campus=active_tab,
                            day=day_canon.title(),
                            start=fmt_time(chosen[0]),
                            end=fmt_time(chosen[1]),
                            details="ok",
                        )
                    except Exception:
                        st.toast("Note: logging skipped due to quota.", icon="⚠️")

                    st.session_state["mode"] = None
                    st.session_state["sched_expanded"] = True
                    _flash("success", f"✅ {str(msg).strip()}")
                    st.rerun()
                except Exception as e:
                    if maybe_show_recovery_popup(e, where="adding shift"):
                        return
                    st.error(_strip_debug_blob(str(e)))

        # Guided remove
        elif mode == "remove":
            st.markdown("### Remove shift — Guided")
            # IMPORTANT: when a specific tab is selected in the sidebar, we must
            # derive assignments from that same tab. (Especially for On-Call,
            # where different weekly tabs can have different layouts.)
            # Summer mode uses the date-based summer parser, not the old UNH/MC parser.
            kind = campus_kind(active_tab)

            try:
                if getattr(config, "SUMMER_MODE", False):
                    user_sched_all = cached_user_schedule(ss.id, canon_name, epoch_key)
                    src_label = "On-Call"  # summer shifts are stored here but labeled as Summer
                else:
                    base_titles = schedule_query._open_three(ss) or []  # UNH, MC, On-Call
                    unh_title = base_titles[0] if len(base_titles) >= 1 else None
                    mc_title = base_titles[1] if len(base_titles) >= 2 else None
                    oncall_title = active_tab if campus_kind(active_tab) == "ONCALL" else (base_titles[2] if len(base_titles) >= 3 else None)
                    user_sched_all = cached_user_schedule_for_titles(
                        ss.id,
                        canon_name,
                        unh_title,
                        mc_title,
                        oncall_title,
                        epoch_key,
                    )
                    src_label = {"UNH": "UNH", "MC": "MC", "ONCALL": "On-Call"}[kind]
            except Exception:
                user_sched_all = {}
                src_label = "On-Call" if getattr(config, "SUMMER_MODE", False) else {"UNH": "UNH", "MC": "MC", "ONCALL": "On-Call"}[kind]

            days_all = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            day_pool = weekday_filter(days_all, active_tab)

            d_opts = [
                d for d in day_pool
                if (user_sched_all.get(d, {}) or {}).get(src_label, [])
            ]

            if not d_opts:
                label = "summer shifts" if getattr(config, "SUMMER_MODE", False) else f"{src_label} assignments"
                st.info(f"No {label} found to call-out from.")
            else:
                dsel = st.radio("Step A — Pick day", [d.title() for d in day_options], horizontal=True)
                day_canon = dsel.lower()
                ranges_today_raw = (user_sched_all.get(day_canon, {}) or {}).get(src_label, []) or []

                # On-Call blocks must be removed as whole blocks.
                if kind == "ONCALL":
                    ranges_today = _iter_pairs(ranges_today_raw)
                    labels = [f"{s} – {e}" for (s, e) in ranges_today]
                    which = st.selectbox("Step B — Select the On-Call block to remove", labels)
                    idx = labels.index(which)
                    s_str, e_str = ranges_today[idx]
                    sdt = _parse_12h_time(s_str)
                    edt = _parse_12h_time(e_str)
                else:
                    # UNH/MC: allow partial removals in 30-minute increments.
                    ranges_today = _iter_pairs(ranges_today_raw)
                    shift_labels = [f"{s} – {e}" for (s, e) in ranges_today]
                    which_shift = st.selectbox("Step B — Pick the shift", shift_labels)
                    s_str, e_str = ranges_today[shift_labels.index(which_shift)]

                    sdt0 = _parse_12h_time(s_str)
                    edt0 = _parse_12h_time(e_str)
                    if edt0 <= sdt0:
                        edt0 = edt0 + timedelta(days=1)
                    shift_minutes = int((edt0 - sdt0).total_seconds() // 60)

                    st.markdown("**Step C — How much time do you want to remove?**")
                    increments = list(range(30, max(30, shift_minutes) + 1, 30))
                    default_minutes = 60 if 60 in increments else increments[-1]
                    label_map = {m: (f"{m // 60}h {m % 60}m" if m % 60 else f"{m // 60}h") for m in increments}
                    pick_dur = st.selectbox(
                        "30-minute steps (up to the shift length)",
                        options=[label_map[m] for m in increments],
                        index=[label_map[m] for m in increments].index(label_map[default_minutes]),
                    )
                    requested_minutes = {v: k for k, v in label_map.items()}[pick_dur]

                    st.markdown("**Step D — Select the exact window to remove**")
                    windows = _enumerate_subwindows_within_shift(s_str, e_str, requested_minutes)
                    if not windows:
                        st.info("No sub-window found for that duration. Try a different duration.")
                        sdt = edt = None
                    else:
                        win_labels = [f"{fmt_time(a)} – {fmt_time(b)}" for (a, b) in windows]
                        pick_win = st.selectbox("Available options", options=win_labels, index=0)
                        sdt, edt = windows[win_labels.index(pick_win)]

                want_approval = st.checkbox(
                    "Send for approval",
                    value=True if force_approval else False,
                    disabled=force_approval,
                )
                if (sdt is not None and edt is not None) and st.button("Remove"):
                    try:
                        if want_approval or force_approval:
                            with st.spinner("Submitting request..."):
                                rid = submit_approval_request(
                                    ss,
                                    requester=canon_name,
                                    action="remove",
                                    campus=("ONCALL" if kind == "ONCALL" else kind),
                                    day=day_canon.title(),
                                    start=fmt_time(sdt),
                                    end=fmt_time(edt),
                                    details=_attach_details_meta(
                                        details="requested",
                                        campus_key=("ONCALL" if kind == "ONCALL" else kind),
                                        sheet_title=active_tab,
                                        sheet_gid=_sheet_gid_for_title(schedule_global, active_tab),
                                    ),
                                )
                            st.session_state["mode"] = None
                            st.session_state["sched_expanded"] = True
                            _flash("success", f"🕓 Submitted for approval (id {rid}).")
                            st.rerun()

                        msg = chat_remove.handle_remove(
                            st,
                            ss,
                            schedule_global,
                            canon_target_name=canon_name,
                            campus_title=active_tab,
                            day=day_canon,
                            start=sdt.time(),
                            end=edt.time(),
                        )

                        try:
                            append_audit(
                                ss,
                                actor=canon_name,
                                action="remove",
                                campus=active_tab,
                                day=day_canon.title(),
                                start=fmt_time(sdt),
                                end=fmt_time(edt),
                                details="ok",
                            )
                        except Exception:
                            st.toast("Note: logging skipped due to quota.", icon="⚠️")

                        # Instant sidebar update: use last-known hours and subtract duration.
                        hours_after = None
                        last = st.session_state.get("_LAST_HOURS")
                        try:
                            if (
                                isinstance(last, dict)
                                and last.get("user") == canon_name
                                and last.get("epoch") == epoch_key
                            ):
                                hb = float(last.get("hours", 0.0))
                                edt2 = edt
                                if edt2 <= sdt:
                                    edt2 = edt2 + timedelta(days=1)
                                delta_h = max(0.0, (edt2 - sdt).total_seconds() / 3600.0)
                                hours_after = max(0.0, hb - delta_h)
                        except Exception:
                            hours_after = None

                        if hours_after is not None:
                            st.session_state["_HOURS_OVERRIDE"] = {
                                "user": canon_name,
                                "hours": float(hours_after),
                                "expires": time.time() + 10,
                            }
                        st.session_state["mode"] = None
                        st.session_state["sched_expanded"] = True
                        _flash("success", f"✅ {str(msg).strip()}")
                        st.rerun()
                    except Exception as e:
                        if maybe_show_recovery_popup(e, where="removing shift"):
                            return
                        st.error(_strip_debug_blob(str(e)))

        # Guided callout
        elif mode == "callout":
            st.markdown("### Call-Out — Guided")
            kind = campus_kind(active_tab)

            # Summer mode uses the new date-based summer parser.
            # Non-summer mode keeps the original UNH/MC/On-Call behavior.
            try:
                if getattr(config, "SUMMER_MODE", False):
                    user_sched_all = cached_user_schedule(ss.id, canon_name, epoch_key)
                    src_label = "On-Call"  # summer shifts are stored here, displayed as Summer
                else:
                    base_titles = schedule_query._open_three(ss) or []  # UNH, MC, On-Call
                    unh_title = base_titles[0] if len(base_titles) >= 1 else None
                    mc_title = base_titles[1] if len(base_titles) >= 2 else None
                    oncall_title = active_tab if campus_kind(active_tab) == "ONCALL" else (base_titles[2] if len(base_titles) >= 3 else None)
                    user_sched_all = cached_user_schedule_for_titles(
                        ss.id,
                        canon_name,
                        unh_title,
                        mc_title,
                        oncall_title,
                        epoch_key,
                    )
                    src_label = {"UNH": "UNH", "MC": "MC", "ONCALL": "On-Call"}[kind]
            except Exception:
                user_sched_all = {}
                if getattr(config, "SUMMER_MODE", False):
                    src_label = "On-Call"
                else:
                    src_label = {"UNH": "UNH", "MC": "MC", "ONCALL": "On-Call"}[kind]

            days_all = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            day_pool = weekday_filter(days_all, active_tab)

            d_opts = [
                d for d in day_pool
                if (user_sched_all.get(d, {}) or {}).get(src_label, [])
            ]

            if not d_opts:
                label = "summer shifts" if getattr(config, "SUMMER_MODE", False) else f"{src_label} assignments"
                st.info(f"No {label} found to call-out from.")
            else:
                dsel = st.radio("Step A — Day", [d.title() for d in d_opts], horizontal=True)
                day_canon = dsel.lower()
                ranges_today_raw = (user_sched_all.get(day_canon, {}) or {}).get(src_label, []) or []

                callout_blocks = []
                for block in ranges_today_raw:
                    pair = schedule_query._extract_time_pair(block)
                    if not pair:
                        continue

                    s_str, e_str = pair
                    actual_date = None
                    source_label = src_label

                    if isinstance(block, dict):
                        source_label = block.get("source") or source_label
                        if block.get("date"):
                            try:
                                actual_date = date.fromisoformat(str(block["date"]))
                            except Exception:
                                actual_date = None

                    label_date = actual_date.strftime("%a %-m/%-d/%Y") if actual_date else day_canon.title()

                    callout_blocks.append(
                        {
                            "label": f"{label_date} • {source_label} • {s_str} – {e_str}",
                            "date": actual_date,
                            "start": s_str,
                            "end": e_str,
                            "source": source_label,
                        }
                    )

                labels = [b["label"] for b in callout_blocks]
                which = st.selectbox("Step B — Which window are you calling out for?", labels)
                selected_callout = callout_blocks[labels.index(which)]

                s_str = selected_callout["start"]
                e_str = selected_callout["end"]

                sdt = datetime.strptime(s_str, "%I:%M %p")
                edt = datetime.strptime(e_str, "%I:%M %p")

                if selected_callout.get("date"):
                    actual_date = selected_callout["date"]
                    sdt = datetime.combine(actual_date, sdt.time())
                    edt = datetime.combine(actual_date, edt.time())
                    if edt <= sdt:
                        edt = edt + timedelta(days=1)
                # Support partial callouts (30-minute increments).
                sdt0 = sdt
                edt0 = edt
                if edt0 <= sdt0:
                    edt0 = edt0 + timedelta(days=1)
                total_mins = int((edt0 - sdt0).total_seconds() // 60)

                # Policy: On-Call callouts are always for the *entire* shift window.
                if kind == "ONCALL":
                    st.info("On-Call call-outs must be for the full shift.")
                    duration_mins = total_mins
                    callout_start = sdt0
                    callout_end = edt0
                else:
                    st.markdown("**Step C — How much time are you calling out for?**")
                    increments = [m for m in range(30, max(30, total_mins) + 1, 30)]
                    if total_mins not in increments:
                        # If the shift isn't on a 30-min grid, fall back to full shift only.
                        increments = [total_mins]

                    def _dur_label(m: int) -> str:
                        if m == total_mins:
                            if m < 60:
                                return f"Full shift ({m}m)"
                            h, r = divmod(m, 60)
                            return f"Full shift ({h}h {r}m)" if r else f"Full shift ({h}h)"
                        if m < 60:
                            return f"{m}m"
                        h, r = divmod(m, 60)
                        if r:
                            return f"{h}h {r}m"
                        return f"{h}h"

                    dur_labels = [_dur_label(m) for m in increments]
                    dur_pick = st.selectbox("Duration", options=dur_labels, index=len(dur_labels) - 1)
                    duration_mins = increments[dur_labels.index(dur_pick)]

                    st.markdown("**Step D — When does your callout start?**")
                    if duration_mins >= total_mins:
                        callout_start = sdt0
                    else:
                        start_mode = st.radio(
                            "Start time",
                            options=["Start at shift start", "Pick a start time"],
                            horizontal=True,
                            key="callout.start_mode",
                        )
                        if start_mode == "Start at shift start":
                            callout_start = sdt0
                        else:
                            latest = edt0 - timedelta(minutes=duration_mins)
                            starts: list[datetime] = []
                            t = sdt0
                            while t <= latest:
                                starts.append(t)
                                t = t + timedelta(minutes=30)
                            start_labels = [fmt_time(t) for t in starts]
                            picked = st.selectbox("Start time (30-min steps)", options=start_labels, index=0)
                            callout_start = starts[start_labels.index(picked)]

                    callout_end = callout_start + timedelta(minutes=duration_mins)
                    if callout_end > edt0:
                        callout_end = edt0

                # UNH/MC callouts: allow specifying the calendar date (used for logs/DB).
                # Summer shifts already carry their actual date from the parser.
                event_date = selected_callout.get("date")
                event_date_valid = True

                if getattr(config, "SUMMER_MODE", False):
                    if not event_date:
                        st.warning("Could not derive the actual summer shift date.")
                        event_date_valid = False
                    else:
                        st.caption(f"Selected date: {event_date.isoformat()} ({event_date.strftime('%A')})")

                elif kind in {"UNH", "MC"}:
                    default_d = _callout_event_date_for_sheet(ss, active_tab, day_canon)
                    if not default_d:
                        st.warning("Could not derive the shift date from the selected day.")
                        event_date_valid = False
                    else:
                        date_key = f"callout.date.{active_tab}.{day_canon}"
                        event_date = st.date_input(
                            "Date of shift",
                            value=default_d,
                            key=date_key,
                        )
                        if not _date_matches_day_canon(event_date, day_canon):
                            picked_day = event_date.strftime("%A") if isinstance(event_date, date) else "that day"
                            st.error(
                                f"Selected date is {picked_day}. Please choose a {day_canon.title()} date."
                            )
                            event_date_valid = False
                        else:
                            st.caption(f"Selected date: {event_date.isoformat()} ({day_canon.title()})")

                reason_opt = st.selectbox(
                    "Reason",
                    options=["sick", "personal", "emergency", "other"],
                    index=0,
                    key="callout.reason",
                )
                reason = reason_opt
                if reason_opt == "other":
                    other_txt = st.text_input("If other, describe briefly", key="callout.reason_other")
                    reason = f"other:{other_txt.strip()}" if other_txt.strip() else "other"

                # Per policy: callouts proceed directly and are always logged as "no cover".
                can_apply_callout = bool(getattr(config, "SUMMER_MODE", False) or kind == "ONCALL" or event_date_valid)
                if st.button("Apply Call-Out", disabled=not can_apply_callout):
                    try:
                        # Derive event date
                        # Derive event date
                        if getattr(config, "SUMMER_MODE", False):
                            campus_key = "SUMMER"
                            if not event_date:
                                raise ValueError("Callout missing actual summer date.")
                            event_d = event_date
                        else:
                            campus_key = ("ONCALL" if kind == "ONCALL" else kind)
                            if kind in {"UNH", "MC"}:
                                if not event_date:
                                    raise ValueError("Callout missing date. Could not derive a date for the selected day.")
                                event_d = event_date
                            else:
                                event_d = _oncall_event_date(active_tab, day_canon)
                                if not event_d:
                                    raise ValueError("Could not derive On-Call event date from sheet title")
                        # Keep the existing current-week rule for MC/UNH, but allow
                        # On-Call week tabs to color their own schedule grid immediately.
                        should_color_now = _should_color_schedule_now(campus_key=campus_key, event_d=event_d)

                        if getattr(config, "SUMMER_MODE", False):
                            should_color_now = True
                            
                        if should_color_now:
                            msg = chat_callout.handle_callout(
                                st,
                                ss,
                                schedule_global,
                                canon_target_name=canon_name,
                                campus_title=active_tab,
                                day=day_canon,
                                start=callout_start if getattr(config, "SUMMER_MODE", False) else callout_start.time(),
                                end=callout_end if getattr(config, "SUMMER_MODE", False) else callout_end.time(),
                                covered_by=None,
                            )
                        else:
                            msg = "Logged future callout (no schedule color change)."

                        # Best-effort DB upsert + swap section render.
                        start_at = _combine_date_time_la(event_d, callout_start.time())
                        end_at = _combine_date_time_la(event_d, callout_end.time())
                        if end_at <= start_at:
                            end_at = end_at + timedelta(days=1)

                        db_ok = False
                        try:
                            if callouts_db.supabase_callouts_enabled():
                                approval_id = (
                                    f"direct_callout|{campus_key}|{event_d.isoformat()}|"
                                    f"{utils.name_key(canon_name)}|{callout_start.strftime('%H:%M')}|{callout_end.strftime('%H:%M')}"
                                )
                                callouts_db.upsert_callout(
                                    {
                                        "approval_id": approval_id,
                                        "submitted_at": datetime.now(tz=ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds"),
                                        "campus": campus_key,
                                        "event_date": str(event_d),
                                        "shift_start_at": start_at.isoformat(timespec="seconds"),
                                        "shift_end_at": end_at.isoformat(timespec="seconds"),
                                        "duration_hours": round(_duration_hours_between(start_at, end_at), 4),
                                        "caller_name": canon_name,
                                        "reason": reason,
                                    }
                                )
                                db_ok = True
                        except Exception as e:
                            st.warning(f"Sheets updated, but callout DB sync failed: {_strip_debug_blob(str(e))}")

                        if db_ok:
                            try:
                                from ..integrations.supabase_io import get_supabase
                                from ..jobs.sync_swaps_to_sheets import sync_swaps_to_sheets

                                sb = get_supabase()
                                ws_obj = None
                                try:
                                    if hasattr(schedule_global, "_load_ws_map"):
                                        schedule_global._load_ws_map()  # type: ignore[attr-defined]
                                        ws_obj = getattr(schedule_global, "_ws_map", {}).get(active_tab)
                                except Exception:
                                    ws_obj = None

                                res = sync_swaps_to_sheets(
                                    ss,
                                    sb,
                                    worksheet=ws_obj,
                                    sheet_title=active_tab,
                                    apply_grid_colors=True,
                                )
                                grid_errs = res.get("grid_errors") or []
                                if grid_errs:
                                    st.warning(f"Swap grid sync had issues. First issue: {grid_errs[0]}")
                            except Exception as e:
                                st.warning(f"Callout recorded, but swap section sync failed: {_strip_debug_blob(str(e))}")

                        try:
                            append_audit(
                                ss,
                                actor=canon_name,
                                action="callout",
                                campus=active_tab,
                                day=day_canon.title(),
                                start=fmt_time(callout_start),
                                end=fmt_time(callout_end),
                                details=f"no cover | reason={reason}",
                            )
                        except Exception:
                            st.toast("Note: logging skipped due to quota.", icon="⚠️")

                        st.session_state["mode"] = None
                        st.session_state["sched_expanded"] = True
                        _flash("success", f"✅ {str(msg).strip()}")
                        st.rerun()
                    except Exception as e:
                        if maybe_show_recovery_popup(e, where="calling out"):
                            return
                        st.error(_strip_debug_blob(str(e)))

        # Peek (wrap to avoid crashing on transient quota/network errors)
        try:
            if re.search(r"\bon\s*[- ]?call\b", active_tab, flags=re.I):
                ui_peek.peek_oncall(ss)
            else:
                ui_peek.peek_exact(schedule_global, [active_tab])
        except Exception as e:
            if maybe_show_recovery_popup(e, where="reading schedule / peek"):
                return
            st.info(f"Could not render schedule: {_strip_debug_blob(str(e))}")

    # Main navigation (Scheduler / Pending Actions / My Requests).
    # Use a radio control instead of st.tabs so selection persists across reruns
    # (e.g., when pressing Refresh buttons).
    try:
        rows_all = cached_approval_table(ss.id, _approval_epoch(), max_rows=500) or []
        my_pending = sum(
            1
            for r in _requests_for_user(rows_all, canon_name)
            if str(r.get("Status", "")).upper() == "PENDING"
        )
        total_pending = sum(
            1 for r in rows_all if str(r.get("Status", "")).upper() == "PENDING"
        )
    except Exception:
        my_pending = 0
        total_pending = 0

    tab_my_label = f"My Requests ({my_pending})" if my_pending else "My Requests"
    tab_pending_label = (
        f"Pending Actions ({total_pending})" if total_pending else "Pending Actions"
    )

    label_map = {
        "scheduler": "Scheduler",
        "pending": tab_pending_label,
        "my": tab_my_label,
    }

    options = ["scheduler", "pending", "my"] if approver_mode else ["scheduler", "my"]

    # Initialize / sanitize selection
    if "main_tab" not in st.session_state:
        st.session_state["main_tab"] = options[0]
    if st.session_state.get("main_tab") not in options:
        st.session_state["main_tab"] = options[0]

    sel = st.radio(
        "Main navigation",
        options,
        horizontal=True,
        key="main_tab",
        format_func=lambda k: label_map.get(k, k),
        label_visibility="collapsed",
    )

    if sel == "scheduler":
        render_scheduler()
    elif sel == "pending":
        render_pending_actions()
    else:
        render_my_requests()


    st.markdown('---')
    st.caption('© ' + str(__import__('datetime').datetime.now().year) + ' Vraj Patel. All rights reserved.')
