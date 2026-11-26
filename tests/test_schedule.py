"""The weekly schedule: parsing, and changing only the scheduled temperature."""
from __future__ import annotations

import pytest

from custom_components.thermann.schedule import ScheduleError, hourly, with_scheduled_temperature

# The owner's real schedule (probe, 2026-11-26): 45 at 06:00 on weekdays and
# 12:00-17:59 every day, 20 otherwise.
WEEKDAY = "20" * 6 + "45" + "20" * 5 + "45" * 6 + "20" * 6
WEEKEND = "20" * 12 + "45" * 6 + "20" * 6
SCHEDULE = {"Time_prg": "0", **{f"DayPrg_{d}": WEEKDAY for d in range(5)},
            "DayPrg_5": WEEKEND, "DayPrg_6": WEEKEND}


def test_hourly_reads_24_values_per_day():
    days = hourly(SCHEDULE)
    assert len(days) == 7 and all(len(day) == 24 for day in days)
    assert days[0][6] == 45 and days[5][6] == 20 and days[6][12] == 45


def test_only_the_scheduled_hours_change():
    new = with_scheduled_temperature(SCHEDULE, 55)
    assert new["Time_prg"] == "0"
    assert new["DayPrg_0"] == WEEKDAY.replace("45", "55")
    assert new["DayPrg_6"] == WEEKEND.replace("45", "55")
    # Same hours on and off; nothing else touched.
    before, after = hourly(SCHEDULE), hourly(new)
    for b_day, a_day in zip(before, after):
        assert [t == 45 for t in b_day] == [t == 55 for t in a_day]
        assert [t for t in a_day if t != 55] == [t for t in b_day if t != 45]
    assert SCHEDULE["DayPrg_0"] == WEEKDAY  # input not modified


def test_single_digit_temperatures_keep_two_digits():
    low = {**SCHEDULE, **{k: v.replace("20", "08") for k, v in SCHEDULE.items() if k != "Time_prg"}}
    assert with_scheduled_temperature(low, 9)["DayPrg_5"] == ("08" * 12 + "09" * 6 + "08" * 6)


@pytest.mark.parametrize("temp", [20, 15])
def test_refuses_a_temperature_not_above_comfort(temp):
    with pytest.raises(ScheduleError, match="above the comfort"):
        with_scheduled_temperature(SCHEDULE, temp)


def test_refuses_a_schedule_without_scheduled_hours():
    flat = {**SCHEDULE, **{f"DayPrg_{d}": "20" * 24 for d in range(7)}}
    with pytest.raises(ScheduleError, match="no scheduled hours"):
        with_scheduled_temperature(flat, 50)


@pytest.mark.parametrize("bad", [None, "", "20" * 23, "20" * 25, "2x" * 24, 42])
def test_refuses_an_unexpected_format(bad):
    with pytest.raises(ScheduleError, match="format"):
        hourly({**SCHEDULE, "DayPrg_3": bad})
