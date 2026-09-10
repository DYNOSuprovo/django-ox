"""
Schedules across a daylight-saving transition.

Driven through `dispatch_schedules` with a frozen clock, not through the
trigger alone. Testing the trigger in isolation is how the fall-back defect
below reached a review: the arithmetic looked right and the dispatch path
was where it went wrong.
"""

import datetime as dt
from datetime import timedelta
from itertools import pairwise

import pytest
from django.utils import timezone

from django_ox.models import OxScheduleTick
from django_ox.worker import Worker

pytestmark = pytest.mark.django_db

LONDON_FALL_BACK = "2025-10-26"
LONDON_SPRING_FORWARD = "2025-03-30"


def tasks_setting(schedules):
    return {
        "default": {
            "BACKEND": "django_ox.backend.OxBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"SCHEDULES": schedules},
        }
    }


def run_across(monkeypatch, settings, schedules, day, start_h, end_h):
    """Step one UTC minute at a time and return every instant that fired."""
    settings.TIME_ZONE = "Europe/London"
    settings.USE_TZ = True
    settings.TASKS = tasks_setting(schedules)
    worker = Worker(backoff_initial=0)
    base = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    clock = {"now": base + timedelta(hours=start_h)}
    monkeypatch.setattr(timezone, "now", lambda: clock["now"])
    stop = base + timedelta(hours=end_h)
    while clock["now"] < stop:
        worker.dispatch_schedules()
        clock["now"] += timedelta(minutes=1)
    return sorted(
        OxScheduleTick.objects.exclude(task__isnull=True).values_list(
            "scheduled_for", flat=True
        )
    )


def biggest_gap(instants):
    return max(
        (b - a for a, b in pairwise(instants)),
        default=timedelta(0),
    )


class TestTheRepeatedHour:
    @pytest.mark.parametrize("every", [900, 1800, 3600])
    def test_an_interval_keeps_its_cadence_through_the_fall_back(
        self, every, monkeypatch, settings
    ):
        # The repeated hour has two distinct instants for one wall-clock
        # label. Deriving the tick by arithmetic dropped the fold, so both
        # passes produced the same instant and the second was suppressed as
        # already recorded: an interval fired nothing for a whole hour.
        fired = run_across(
            monkeypatch,
            settings,
            {"iv": {"task": "tests.tasks.add", "every": every}},
            LONDON_FALL_BACK,
            0,
            3,
        )
        assert fired, "nothing fired at all"
        assert biggest_gap(fired) == timedelta(seconds=every), (
            f"cadence broke across the fall-back: gaps up to {biggest_gap(fired)}"
        )

    def test_a_cron_still_fires_twice_on_the_repeated_hour(self, monkeypatch, settings):
        # Documented behaviour, and it must stay: a wall-clock schedule
        # inside the repeated hour genuinely happens twice.
        fired = run_across(
            monkeypatch,
            settings,
            {"c": {"task": "tests.tasks.add", "cron": "30 1 * * *"}},
            LONDON_FALL_BACK,
            0,
            3,
        )
        assert len(fired) == 2, f"expected both passes of 01:30, got {fired}"

    def test_a_cron_keeps_its_cadence_through_the_fall_back(
        self, monkeypatch, settings
    ):
        fired = run_across(
            monkeypatch,
            settings,
            {"c": {"task": "tests.tasks.add", "cron": "*/15 * * * *"}},
            LONDON_FALL_BACK,
            0,
            3,
        )
        assert biggest_gap(fired) == timedelta(minutes=15)


class TestTheMissingHour:
    @pytest.mark.parametrize("every", [900, 3600])
    def test_an_interval_loses_no_tick_across_the_spring_forward(
        self, every, monkeypatch, settings
    ):
        fired = run_across(
            monkeypatch,
            settings,
            {"iv": {"task": "tests.tasks.add", "every": every}},
            LONDON_SPRING_FORWARD,
            0,
            3,
        )
        assert fired
        assert biggest_gap(fired) == timedelta(seconds=every)

    def test_a_cron_loses_no_tick_across_the_spring_forward(
        self, monkeypatch, settings
    ):
        fired = run_across(
            monkeypatch,
            settings,
            {"c": {"task": "tests.tasks.add", "cron": "*/15 * * * *"}},
            LONDON_SPRING_FORWARD,
            0,
            3,
        )
        assert biggest_gap(fired) == timedelta(minutes=15)
