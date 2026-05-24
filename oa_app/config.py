from datetime import time

# ===== workbook config =====
DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/15ZXPyZ1k2AWHpNd3WYY9XdnrY50iBpqWHIsyjmO3zd4/edit?usp=sharing"
OA_SCHEDULE_SHEETS = ["UNH (OA and GOAs)", "MC (OA and GOAs)"]
ROSTER_SHEET = "(Names of hired OAs)"
# Column header in the roster sheet that contains OA/GOA display names.
# Note: the published template uses "Name (OAs/GOAs)".
ROSTER_NAME_COLUMN_HEADER = "Name (OAs/GOAs)"
AUDIT_SHEET = "Audit Log"
APPROVAL_SHEET = "Pending Actions"
LOCKS_SHEET = "_Locks"   # tiny sheet for FCFS locking
ONCALL_SHEET_OVERRIDE = ""  # e.g., "On-Call (Fall Wk 2)"

# Tabs that exist in the workbook but are *not* schedules and should not
# appear in dropdowns where users pick a schedule/job to operate on.
HIDE_SIDEBAR_TABS = [
    "EO Schedule Policies",
    "On Call General",
]
# ===== guardrails =====
DAY_START = time(7, 0)
DAY_END = time(23, 59)
OA_PREFIX = "OA:"
GOA_PREFIX = "GOA:"

# ===== summer schedule mode =====
SUMMER_MODE = True

SUMMER_DAYS = [
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
]

SUMMER_SHIFT_WINDOWS = [
    ("7:00 AM", "3:30 PM"),
    ("3:30 PM", "12:00 AM"),
]

SUMMER_MAX_SHIFTS_PER_WEEK = 5
SUMMER_WEEKLY_CAP_MINS = 40 * 60

# Each displayed shift is 8.5 scheduled hours.
# If a 30-minute break is unpaid, it should count as 8 paid hours.
SUMMER_SCHEDULED_SHIFT_MINS = 8 * 60 + 30
SUMMER_PAID_SHIFT_MINS = 8 * 60

SUMMER_SHIFT_CAPACITY = 9

# ===== caching / quotas =====
DAY_CACHE_TTL_SEC = 20  # day-column cache lifetime
HEADER_MAX_COLS = 80
ONCALL_MAX_COLS = 100
ONCALL_MAX_ROWS = 1000
HOURS_DEBUG = True   # set False to silence debug prints
