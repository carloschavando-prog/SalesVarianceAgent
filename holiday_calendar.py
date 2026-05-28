"""
holiday_calendar.py — On Par Entertainment
Classifies any date into a day type that drives:
  - DOW override  (e.g. Labor Day Monday → treat as Saturday)
  - Sales multiplier (e.g. New Year's Eve → 1.4×)
  - Closure flag (Thanksgiving, Christmas Day)

Usage in forecast_agent.py:
    from holiday_calendar import get_day_info
    info = get_day_info(forecast_date)
    effective_dow = info.dow_override if info.dow_override is not None else forecast_date.weekday()
    pred = dow_means[effective_dow] * info.multiplier
    if info.is_closed: pred = 0.0

Multipliers are defaults based on industry knowledge for an indoor entertainment
venue in Dayton, OH. Calibrate against actuals by calling calibrate() once you
have 2+ years of history loaded.
"""

from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional


# ── Day info dataclass ────────────────────────────────────────────────────────

@dataclass
class DayInfo:
    name:         str            # human label, e.g. "Memorial Day"
    type:         str            # slug used in model logic
    dow_override: Optional[int]  # None = use actual DOW; 0-6 = use this DOW's mean
    multiplier:   float          # applied after DOW override (1.0 = no change)
    is_closed:    bool           # True = venue closed, forecast = $0

    @property
    def effective_dow(self) -> Optional[int]:
        return self.dow_override


_NORMAL = DayInfo("Normal", "normal", None, 1.0, False)


# ── Moving-holiday date arithmetic ────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th occurrence of weekday (0=Mon…6=Sun) in the given month. n<0 → last."""
    if n > 0:
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return first + timedelta(days=delta + 7 * (n - 1))
    else:  # last occurrence
        if month == 12:
            last = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last = date(year, month + 1, 1) - timedelta(days=1)
        delta = (last.weekday() - weekday) % 7
        return last - timedelta(days=delta)


def _dst_start(year: int) -> date:
    """US DST start: 2nd Sunday in March."""
    return _nth_weekday(year, 3, 6, 2)


def _dst_end(year: int) -> date:
    """US DST end: 1st Sunday in November."""
    return _nth_weekday(year, 11, 6, 1)


def is_dst(d: date) -> bool:
    """True if date d is in US Eastern Daylight Time (UTC-4)."""
    return _dst_start(d.year) <= d < _dst_end(d.year)


def et_offset(d: date) -> int:
    """Hours offset from UTC for Eastern Time (-4 EDT, -5 EST)."""
    return -4 if is_dst(d) else -5


# ── Holiday catalogue for a given year ────────────────────────────────────────
#
# DOW override key:
#   5 = Saturday behaviour (peak volume)
#   4 = Friday behaviour
#   6 = Sunday behaviour
#
# Multiplier is applied ON TOP of the DOW-override mean, so:
#   Memorial Day  → uses Saturday mean × 1.05  (holiday spirit lift)
#   NYE           → uses Saturday mean × 1.40  (the biggest bar night)
#   Dead Tuesday  → uses Monday mean   × 0.60  (e.g. day after Thanksgiving)

def get_holidays(year: int) -> dict:
    """Returns { date: DayInfo } for all special days in `year`."""
    h = {}

    def add(d: date, name: str, type_: str, dow_override, mult, closed=False):
        h[d] = DayInfo(name, type_, dow_override, mult, closed)

    # ── Federal Monday holidays → Saturday baseline ───────────────────────
    mlk   = _nth_weekday(year, 1,  0, 3)   # 3rd Mon Jan
    pres  = _nth_weekday(year, 2,  0, 3)   # 3rd Mon Feb
    mem   = _nth_weekday(year, 5,  0, -1)  # Last Mon May
    labor = _nth_weekday(year, 9,  0, 1)   # 1st Mon Sep
    colum = _nth_weekday(year, 10, 0, 2)   # 2nd Mon Oct
    vet11 = date(year, 11, 11)

    add(mlk,   "MLK Day",        "holiday_monday",  5, 1.00)
    add(pres,  "Presidents Day", "holiday_monday",  5, 0.85)  # weak holiday
    add(mem,   "Memorial Day",   "holiday_monday",  5, 1.10)
    add(labor, "Labor Day",      "holiday_monday",  5, 1.05)
    add(colum, "Columbus Day",   "holiday_monday",  5, 0.80)  # minor
    if vet11.weekday() == 0:  # Veterans Day on Monday
        add(vet11, "Veterans Day (Mon)", "holiday_monday", 5, 0.85)

    # ── Memorial Day long weekend (Fri/Sat/Sun before the Monday) ─────────
    # No DOW override needed — these are already Fri/Sat/Sun.
    # Just apply the holiday-weekend multiplier on top of natural baselines.
    add(mem - timedelta(days=3), "Memorial Day Weekend Fri", "mem_weekend_fri", None, 1.15)
    add(mem - timedelta(days=2), "Memorial Day Weekend Sat", "mem_weekend_sat", None, 1.15)
    add(mem - timedelta(days=1), "Memorial Day Weekend Sun", "mem_weekend_sun", None, 1.08)

    # ── Labor Day long weekend ─────────────────────────────────────────────
    add(labor - timedelta(days=3), "Labor Day Weekend Fri", "labor_weekend_fri", None, 1.10)
    add(labor - timedelta(days=2), "Labor Day Weekend Sat", "labor_weekend_sat", None, 1.10)
    add(labor - timedelta(days=1), "Labor Day Weekend Sun", "labor_weekend_sun", None, 1.05)

    # ── July 4th ──────────────────────────────────────────────────────────
    july4 = date(year, 7, 4)
    dow4  = july4.weekday()
    if dow4 in (0, 1, 2, 3):   # Mon–Thu: treat as Saturday
        add(july4, "July 4th", "july4", 5, 1.20)
    elif dow4 == 4:             # Friday: boost the Friday
        add(july4, "July 4th (Fri)", "july4", 4, 1.20)
    elif dow4 == 5:             # Saturday: extra boost
        add(july4, "July 4th (Sat)", "july4", 5, 1.20)
    else:                       # Sunday: big Sunday
        add(july4, "July 4th (Sun)", "july4", 6, 1.15)
    # Eve of July 4th (if 4th is mid-week, eve is big)
    if dow4 in (1, 2, 3):
        add(july4 - timedelta(days=1), "July 3rd (4th Eve)", "holiday_eve", 4, 1.10)

    # ── New Year's Eve (Dec 31) — biggest bar night ────────────────────────
    nye = date(year, 12, 31)
    add(nye, "New Year's Eve", "nye", 5, 1.40)

    # ── New Year's Day (Jan 1) — slow recovery day ─────────────────────────
    ny1 = date(year, 1, 1)
    add(ny1, "New Year's Day", "new_year_day", 0, 0.60)  # Monday-like but slow

    # ── Valentine's Day ────────────────────────────────────────────────────
    val = date(year, 2, 14)
    # Only meaningful if it falls Fri/Sat/Sun; weekday effect is minor
    if val.weekday() in (4, 5, 6):
        add(val, "Valentine's Day", "valentines", None, 1.15)
    elif val.weekday() in (1, 2, 3):
        add(val, "Valentine's Day (mid-week)", "valentines", None, 1.05)

    # ── St. Patrick's Day (Mar 17) ─────────────────────────────────────────
    stp = date(year, 3, 17)
    if stp.weekday() in (4, 5, 6):
        add(stp, "St. Patrick's Day", "st_patricks", None, 1.20)
    elif stp.weekday() in (1, 2, 3):
        add(stp, "St. Patrick's Day (mid-week)", "st_patricks", None, 1.10)

    # ── Thanksgiving (4th Thu Nov) → closed ──────────────────────────────
    tg = _nth_weekday(year, 11, 3, 4)
    add(tg,                    "Thanksgiving",         "thanksgiving",  None, 0.0,  True)
    add(tg - timedelta(days=1),"Thanksgiving Eve Wed", "tg_eve",        4,    1.05)
    add(tg + timedelta(days=1),"Black Friday",         "black_friday",  4,    0.75) # slow
    add(tg + timedelta(days=2),"Thanksgiving Sat",     "tg_sat",        5,    0.90)

    # ── Christmas ─────────────────────────────────────────────────────────
    xmas = date(year, 12, 25)
    xeve = date(year, 12, 24)
    add(xmas, "Christmas Day",    "christmas",     None, 0.0,  True)
    add(xeve, "Christmas Eve",    "christmas_eve", None, 0.50, False)  # many closed
    add(date(year, 12, 26), "Day after Christmas", "xmas_after", None, 0.70)
    # Week before Christmas (Dec 18-23) is typically slow
    for d_offset in range(6):
        day = date(year, 12, 18) + timedelta(days=d_offset)
        if day not in h:
            add(day, "Pre-Christmas week", "pre_xmas", None, 0.80)

    # ── Graduation season uplift (mid-May through mid-June in SW Ohio) ────
    # Applied as a season multiplier, not a DOW override.
    # Dayton-area schools typically have graduations May 20 – June 14.
    grad_start = date(year, 5, 20)
    grad_end   = date(year, 6, 14)
    d = grad_start
    while d <= grad_end:
        if d not in h:
            add(d, "Graduation Season", "graduation_season", None, 1.12)
        d += timedelta(days=1)

    # ── Spring break (approximate: 2nd week of April, Dayton area) ────────
    # Clark / Montgomery county schools vary; use conservative week
    sb_start = date(year, 4, 7)
    for d_offset in range(7):
        day = sb_start + timedelta(days=d_offset)
        if day not in h:
            add(day, "Spring Break", "spring_break", None, 1.05)

    # ── Super Bowl Sunday ─────────────────────────────────────────────────
    # 2nd Sunday in February (approximation — actual date varies by 1 week)
    # People watch at home; entertainment venues slow down.
    sb_sun = _nth_weekday(year, 2, 6, 2)
    if sb_sun not in h:
        add(sb_sun, "Super Bowl Sunday", "super_bowl", 6, 0.75)

    return h


# ── Public API ────────────────────────────────────────────────────────────────

# Cache per process (holidays don't change mid-run)
_cache: dict = {}


def get_day_info(d: date) -> DayInfo:
    """Return DayInfo for date d, including multiplier and DOW override."""
    if d.year not in _cache:
        _cache[d.year] = get_holidays(d.year)
    return _cache[d.year].get(d, _NORMAL)


def get_effective_dow(d: date) -> int:
    """Return the day-of-week to use for selecting the seasonal baseline."""
    info = get_day_info(d)
    if info.dow_override is not None:
        return info.dow_override
    return d.weekday()


def get_multiplier(d: date) -> float:
    info = get_day_info(d)
    return info.multiplier


def is_closed(d: date) -> bool:
    return get_day_info(d).is_closed


# ── Calibration helper ────────────────────────────────────────────────────────

def calibrate_multipliers(daily_sales: dict, dow_means: dict,
                           min_samples: int = 2) -> dict:
    """
    Compare actual daily totals to the DOW baseline on special days to
    compute data-driven multipliers.  Call this after loading historical
    sales data and computing dow_means (the same dict used in build_forecasts).

    Returns { day_type: observed_multiplier } for logging/review.
    """
    from collections import defaultdict
    from statistics import mean

    type_ratios: dict = defaultdict(list)

    for d_str, day_cats in daily_sales.items():
        d = date.fromisoformat(d_str)
        info = get_day_info(d)
        if info.type == "normal":
            continue

        effective_dow = get_effective_dow(d)
        total_actual   = sum(day_cats.values())
        total_baseline = sum(
            cat_means.get(effective_dow, 0)
            for cat_means in dow_means.values()
        )
        if total_baseline < 1:
            continue

        type_ratios[info.type].append(total_actual / total_baseline)

    result = {}
    for day_type, ratios in type_ratios.items():
        if len(ratios) >= min_samples:
            result[day_type] = round(mean(ratios), 3)

    return result


# ── CLI: print calendar for a year ───────────────────────────────────────────

if __name__ == "__main__":
    import sys
    year = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
    hols = get_holidays(year)
    print(f"\n{year} special days ({len(hols)} total)\n{'─'*60}")
    for d in sorted(hols):
        info = hols[d]
        dow  = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d.weekday()]
        ovr  = f"→ use {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][info.dow_override]}" \
               if info.dow_override is not None else ""
        cls  = " [CLOSED]" if info.is_closed else ""
        print(f"  {d}  {dow}  {info.name:<35}  ×{info.multiplier:.2f}  {ovr}{cls}")
