"""The auto mode's weekly schedule, as the app reads and writes it.

GET /auto/raw-temperatures returns, and POST /auto/raw-temperatures accepts
(the app's own save; a bare POST /auto is only "switch to auto" and ignores
any body):

    {"Time_prg": "0", "DayPrg_0": "2020...45452020", ..., "DayPrg_6": "..."}

Each DayPrg_n holds 24 two-digit temperatures (C), one per hour from 00:00.
The scheduled hours carry the scheduled temperature (the highest value,
reported in /status as Tmsch); the other hours carry the comfort temperature
(the lowest value, "T comfort auto"). The app changes either one by
replacing every slot that holds the highest (or lowest) value with the new
one and posting the whole schedule back; this module does the same and
nothing else, so the on/off pattern of the hours is never changed.
"""
from __future__ import annotations

import re
from typing import Any

DAY_KEYS = tuple(f"DayPrg_{day}" for day in range(7))
HOURS = 24
_DAY = re.compile(rf"^\d{{{2 * HOURS}}}$")


class ScheduleError(ValueError):
    """The schedule can't be changed as asked (bad format or bad value)."""


def hourly(schedule: dict[str, Any]) -> list[list[int]]:
    """The 7 x 24 temperatures; raises ScheduleError if the format is unexpected."""
    days = []
    for key in DAY_KEYS:
        value = schedule.get(key)
        if not isinstance(value, str) or not _DAY.match(value):
            raise ScheduleError(f"Unexpected schedule format in {key}")
        days.append([int(value[i : i + 2]) for i in range(0, 2 * HOURS, 2)])
    return days


def _replace(schedule: dict[str, Any], days: list[list[int]], old: int, new: int) -> dict[str, Any]:
    result = dict(schedule)
    for key, day in zip(DAY_KEYS, days):
        result[key] = "".join(f"{new if t == old else t:02d}" for t in day)
    return result


def _high_low(days: list[list[int]]) -> tuple[int, int]:
    values = [t for day in days for t in day]
    high, low = max(values), min(values)
    if high == low:
        raise ScheduleError("The schedule has no scheduled hours to change")
    return high, low


def with_scheduled_temperature(schedule: dict[str, Any], temperature: int) -> dict[str, Any]:
    """A copy of the schedule with its scheduled (highest) temperature replaced.

    Refuses a schedule without scheduled hours (every hour the same) and a
    temperature not above the comfort (lowest) temperature, which would erase
    the difference between scheduled and other hours.
    """
    days = hourly(schedule)
    high, low = _high_low(days)
    if not low < temperature <= 99:
        raise ScheduleError(
            f"The scheduled temperature must be above the comfort temperature ({low} °C)"
        )
    return _replace(schedule, days, high, temperature)


def with_comfort_temperature(schedule: dict[str, Any], temperature: int) -> dict[str, Any]:
    """A copy of the schedule with its comfort (lowest) temperature replaced.

    Refuses a schedule without scheduled hours and a temperature not below
    the scheduled (highest) temperature.
    """
    days = hourly(schedule)
    high, low = _high_low(days)
    if not 0 <= temperature < high:
        raise ScheduleError(
            f"The comfort temperature must be below the scheduled temperature ({high} °C)"
        )
    return _replace(schedule, days, low, temperature)
