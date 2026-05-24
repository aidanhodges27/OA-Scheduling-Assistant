"""Labor-rule validations for pickup / scheduling flows.

This module is UI-agnostic. It operates on time intervals and returns
structured results that the Streamlit UI can present.

Rules (per request):
  - Weekly cap: 20 hours
  - Daily cap:  8 hours
  - Break rule: after 5 hours of *continuous* work, require a 30-minute break.

"Continuous" means intervals that touch or are separated by a gap < 30 minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple
from .. import config


# ----------------------------
# Constants (minutes)
# ----------------------------

WEEKLY_CAP_MINS = getattr(config, "SUMMER_WEEKLY_CAP_MINS", 20 * 60)
DAILY_CAP_MINS = getattr(config, "SUMMER_SCHEDULED_SHIFT_MINS", 8 * 60)

# Backwards-compatible aliases (UI may reference these names)
MAX_WEEKLY_MINS = WEEKLY_CAP_MINS
MAX_DAILY_MINS = DAILY_CAP_MINS

MAX_CONTINUOUS_MINS = 5 * 60
MIN_BREAK_MINS = 30

# Minimum consecutive work required for any new add/pickup (minutes)
MIN_CONSECUTIVE_MINS = 90



# ----------------------------
# Types
# ----------------------------

Interval = Tuple[datetime, datetime]


@dataclass(frozen=True)
class BreakCheckResult:
    ok: bool
    merged_segments: List[Interval]
    # Suggested alternatives (within a callout window) that would satisfy the break rule.
    alternatives: List[Interval]
    reason: str = ""


# ----------------------------
# Helpers
# ----------------------------


def _norm_interval(s: datetime, e: datetime) -> Interval:
    """Normalize an interval; treat end<=start as an overnight shift."""
    if e <= s:
        e = e + timedelta(days=1)
    return s, e


def minutes_between(s: datetime, e: datetime) -> int:
    s, e = _norm_interval(s, e)
    return int((e - s).total_seconds() // 60)


def _sort_intervals(intervals: Iterable[Interval]) -> List[Interval]:
    out: List[Interval] = []
    for s, e in intervals:
        out.append(_norm_interval(s, e))
    out.sort(key=lambda x: x[0])
    return out


def merge_intervals(intervals: Iterable[Interval], *, min_break_mins: int = MIN_BREAK_MINS) -> List[Interval]:
    """Merge intervals where the gap is < min_break_mins."""
    gap = timedelta(minutes=int(min_break_mins))
    items = _sort_intervals(intervals)
    if not items:
        return []
    merged: List[Interval] = []
    cur_s, cur_e = items[0]
    for s, e in items[1:]:
        if s - cur_e < gap:
            # Not enough break -> continuous segment.
            if e > cur_e:
                cur_e = e
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return merged


def violates_break_rule(
    existing: Iterable[Interval],
    proposed: Interval,
    *,
    max_continuous_mins: int = MAX_CONTINUOUS_MINS,
    min_break_mins: int = MIN_BREAK_MINS,
) -> bool:
    segs = merge_intervals(list(existing) + [proposed], min_break_mins=min_break_mins)
    return any(minutes_between(s, e) > int(max_continuous_mins) for s, e in segs)


def _snap_up(dt: datetime, step_mins: int) -> datetime:
    if step_mins <= 1:
        return dt
    minute = dt.hour * 60 + dt.minute
    snapped = ((minute + step_mins - 1) // step_mins) * step_mins
    dd = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dd + timedelta(minutes=snapped)


def _snap_down(dt: datetime, step_mins: int) -> datetime:
    if step_mins <= 1:
        return dt
    minute = dt.hour * 60 + dt.minute
    snapped = (minute // step_mins) * step_mins
    dd = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dd + timedelta(minutes=snapped)


def _within(a: datetime, lo: datetime, hi: datetime) -> bool:
    return lo <= a <= hi


def break_check_with_suggestions(
    existing: Iterable[Interval],
    desired: Interval,
    *,
    window: Optional[Interval] = None,
    min_duration_mins: int = 90,
    step_mins: int = 30,
    max_continuous_mins: int = MAX_CONTINUOUS_MINS,
    min_break_mins: int = MIN_BREAK_MINS,
) -> BreakCheckResult:
    """Check the break rule and propose alternative intervals (if any).

    Parameters
    ----------
    existing:
        Existing work intervals for the same day.
    desired:
        The user's requested interval.
    window:
        The callout window bounds. Suggestions will stay within this window.
        If omitted, suggestions will stay within the desired interval.
    min_duration_mins:
        Minimum length allowed for a pickup.
    step_mins:
        Grid size (e.g., 30 minutes).
    """

    ds, de = _norm_interval(*desired)
    win_s, win_e = _norm_interval(*(window or desired))

    merged_now = merge_intervals(list(existing) + [(ds, de)], min_break_mins=min_break_mins)
    ok = not any(minutes_between(s, e) > int(max_continuous_mins) for s, e in merged_now)
    if ok:
        return BreakCheckResult(ok=True, merged_segments=merged_now, alternatives=[], reason="")

    # Build a small set of candidate alternatives.
    existing_sorted = _sort_intervals(existing)
    gap = timedelta(minutes=int(min_break_mins))

    prev_end: Optional[datetime] = None
    next_start: Optional[datetime] = None

    # Find the nearest previous interval end and next interval start.
    for s, e in existing_sorted:
        if e <= ds:
            prev_end = e
        elif s >= de and next_start is None:
            next_start = s
            break

    desired_mins = minutes_between(ds, de)

    cands: List[Interval] = []

    # Candidate A: push start forward to force a 30-minute break after the previous interval.
    if prev_end is not None and ds - prev_end < gap:
        cand_s = _snap_up(max(win_s, prev_end + gap), step_mins)
        cand_e = _snap_down(min(de, win_e), step_mins)
        if cand_e > cand_s and minutes_between(cand_s, cand_e) >= min_duration_mins:
            cands.append((cand_s, cand_e))

        # Keep same duration if possible.
        cand_s2 = _snap_up(max(win_s, prev_end + gap), step_mins)
        cand_e2 = _snap_down(cand_s2 + timedelta(minutes=desired_mins), step_mins)
        if cand_e2 <= win_e and cand_e2 > cand_s2 and minutes_between(cand_s2, cand_e2) >= min_duration_mins:
            cands.append((cand_s2, cand_e2))

    # Candidate B: pull end earlier to force a 30-minute break before the next interval.
    if next_start is not None and next_start - de < gap:
        cand_e = _snap_down(min(win_e, next_start - gap), step_mins)
        cand_s = _snap_up(max(ds, win_s), step_mins)
        if cand_e > cand_s and minutes_between(cand_s, cand_e) >= min_duration_mins:
            cands.append((cand_s, cand_e))

        # Keep same duration if possible.
        cand_e2 = _snap_down(min(win_e, next_start - gap), step_mins)
        cand_s2 = _snap_up(cand_e2 - timedelta(minutes=desired_mins), step_mins)
        if cand_s2 >= win_s and cand_e2 > cand_s2 and minutes_between(cand_s2, cand_e2) >= min_duration_mins:
            cands.append((cand_s2, cand_e2))

    # Candidate C: cap the pickup to max_continuous_mins.
    cap = int(max_continuous_mins)
    cand_s = _snap_up(max(ds, win_s), step_mins)
    cand_e = _snap_down(min(win_e, cand_s + timedelta(minutes=cap)), step_mins)
    if cand_e > cand_s and minutes_between(cand_s, cand_e) >= min_duration_mins:
        cands.append((cand_s, cand_e))

    # Candidate D: cap from the end backwards.
    cand_e = _snap_down(min(de, win_e), step_mins)
    cand_s = _snap_up(max(win_s, cand_e - timedelta(minutes=cap)), step_mins)
    if cand_e > cand_s and minutes_between(cand_s, cand_e) >= min_duration_mins:
        cands.append((cand_s, cand_e))

    # Deduplicate and validate.
    seen = set()
    alts: List[Interval] = []
    for s, e in cands:
        s, e = _norm_interval(s, e)
        if not (_within(s, win_s, win_e) and _within(e, win_s, win_e)):
            continue
        if minutes_between(s, e) < min_duration_mins:
            continue
        key = (s.hour, s.minute, e.hour, e.minute)
        if key in seen:
            continue
        seen.add(key)
        if not violates_break_rule(existing, (s, e), max_continuous_mins=max_continuous_mins, min_break_mins=min_break_mins):
            alts.append((s, e))

    reason = (
        f"Break rule: you can't work more than {max_continuous_mins//60} hours continuously "
        f"without a {min_break_mins}-minute break."
    )
    return BreakCheckResult(ok=False, merged_segments=merged_now, alternatives=alts, reason=reason)


# ----------------------------
# Minimum consecutive block rule
# ----------------------------

def merge_touching_intervals(intervals: Iterable[Interval]) -> List[Interval]:
    """Merge intervals that overlap or *touch* (end == next start).

    This is stricter than merge_intervals(): it does NOT merge across breaks.
    Used for the minimum-consecutive-work rule.
    """
    items = _sort_intervals(intervals)
    if not items:
        return []
    merged: List[Interval] = []
    cur_s, cur_e = items[0]
    for s, e in items[1:]:
        # Merge if overlapping or exactly touching.
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return merged


def consecutive_block_minutes_for(
    existing: Iterable[Interval],
    proposed: Interval,
    *,
    min_consecutive_mins: int = MIN_CONSECUTIVE_MINS,
) -> Tuple[bool, int]:
    """Return whether the proposed interval participates in a consecutive block >= min_consecutive_mins.

    The block is computed by merging only touching/overlapping intervals (no gaps).
    """
    ps, pe = _norm_interval(*proposed)
    merged = merge_touching_intervals(list(existing) + [(ps, pe)])

    block_mins = 0
    # Find the merged block that overlaps the proposed interval.
    for s, e in merged:
        if not (e <= ps or s >= pe):
            block_mins = minutes_between(s, e)
            break

    return (block_mins >= int(min_consecutive_mins), int(block_mins))
