"""
Schedules across a daylight-saving transition.

Driven through `dispatch_schedules` with a frozen clock, not through the
trigger alone. The trigger's arithmetic and the dispatch path can disagree
about a repeated hour, and only the dispatch path is what runs.
"""

import datetime as dt
from datetime import timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

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


def run_across(
    monkeypatch, settings, schedules, day, start_h, end_h, zone="Europe/London"
):
    """Step one UTC minute at a time and return every instant that fired."""
    settings.TIME_ZONE = zone
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
        # label. Deriving the tick by arithmetic drops the fold, which would
        # give both passes the same instant, suppress the second as already
        # recorded, and fire nothing for the whole hour.
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
        # The count as well as the gap: an evenly spaced subset satisfies the
        # gap on its own, so two surviving ticks would pass it.
        assert len(fired) == 11, f"expected every quarter hour, got {fired}"
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
        assert len(fired) == 11, f"expected every quarter hour, got {fired}"
        assert biggest_gap(fired) == timedelta(minutes=15)


class TestALocalTimeThatDoesNotExist:
    """
    On the day a zone springs forward, an hour of wall-clock labels never
    happens. Attaching the zone to one of them resolves to an instant on
    the far side of the gap, which has not arrived.

    A tick is the latest instant at or before now, so one resolving into
    the future is refused. Enqueueing it would run the task early and stamp
    it with the instant of the next real tick, which the dispatch log would
    then read as already recorded.
    """

    def test_an_interval_whose_tick_lands_in_the_gap_does_not_fire_early(
        self, monkeypatch, settings
    ):
        settings.TIME_ZONE = "America/New_York"
        settings.USE_TZ = True
        settings.TASKS = tasks_setting(
            {"iv": {"task": "tests.tasks.add", "every": 3600, "phase": 1800}}
        )
        worker = Worker(backoff_initial=0)
        # 2025-03-09 in New York: 02:00 EST becomes 03:00 EDT, so every
        # wall clock from 02:00 to 02:59 is a time that never happens.
        base = dt.datetime(2025, 3, 9, 5, 0, tzinfo=dt.UTC)
        clock = {"now": base}
        monkeypatch.setattr(timezone, "now", lambda: clock["now"])
        stop = base + timedelta(hours=6)
        early = []
        while clock["now"] < stop:
            worker.dispatch_schedules()
            early += list(
                OxScheduleTick.objects.filter(
                    scheduled_for__gt=clock["now"], task__isnull=False
                )
            )
            clock["now"] += timedelta(minutes=1)
        assert not early, (
            "enqueued a tick before its instant: "
            f"{[(r.schedule_name, r.scheduled_for.isoformat()) for r in early]}"
        )

    def test_the_tick_on_the_far_side_of_the_gap_still_fires(
        self, monkeypatch, settings
    ):
        fired = run_across(
            monkeypatch,
            settings,
            {"iv": {"task": "tests.tasks.add", "every": 3600, "phase": 1800}},
            "2025-03-09",
            5,
            11,
            zone="America/New_York",
        )
        labels = [f.astimezone(ZoneInfo("America/New_York")) for f in fired]
        assert any(stamp.hour == 3 and stamp.minute == 30 for stamp in labels), (
            f"the first tick after the gap never fired: {labels}"
        )


class TestTheWallClockLimitWithoutTimeZoneSupport:
    """
    Under USE_TZ=False a tick's time is stored as a naive wall clock, and
    the repeated hour has one label for two instants. The second is read as
    a tick already recorded.

    Recorded here as a limit rather than half-guarded. Closing it means
    changing what the tick log stores, and that table's schema is a
    published promise. `django_ox.W001` reports the configuration, and the
    schedules page states the effect.
    """

    def test_an_interval_loses_the_repeated_hour_s_second_pass(
        self, monkeypatch, settings
    ):
        settings.TIME_ZONE = "Europe/London"
        settings.USE_TZ = False
        settings.TASKS = tasks_setting(
            {"iv": {"task": "tests.tasks.add", "every": 1800}}
        )
        worker = Worker(backoff_initial=0)
        base = dt.datetime(2025, 10, 26, tzinfo=dt.UTC)
        clock = {"utc": base}
        # USE_TZ=False makes timezone.now() naive local time. Stepping the
        # underlying UTC instant is what makes the repeated hour happen.
        monkeypatch.setattr(
            timezone,
            "now",
            lambda: (
                clock["utc"].astimezone(ZoneInfo("Europe/London")).replace(tzinfo=None)
            ),
        )
        stop = base + timedelta(hours=3)
        while clock["utc"] < stop:
            worker.dispatch_schedules()
            clock["utc"] += timedelta(minutes=1)
        fired = sorted(
            OxScheduleTick.objects.exclude(task__isnull=True).values_list(
                "scheduled_for", flat=True
            )
        )
        # Six half-hourly instants pass in three hours, four distinct wall
        # clock labels cover them, and the first is spent anchoring.
        assert len(fired) == 3, f"expected the documented loss, got {fired}"
        # The gap alone cannot see this, which is why the count is asserted.
        assert biggest_gap(fired) == timedelta(minutes=30)

    def test_the_check_reports_the_configuration(self, settings):
        from django_ox.compat import default_task_backend

        settings.TIME_ZONE = "Europe/London"
        settings.USE_TZ = False
        settings.TASKS = tasks_setting(
            {"iv": {"task": "tests.tasks.add", "every": 1800}}
        )
        assert "django_ox.W001" in [e.id for e in default_task_backend.check()]

    def test_a_zone_without_a_transition_is_not_reported(self, settings):
        from django_ox.compat import default_task_backend

        settings.TIME_ZONE = "UTC"
        settings.USE_TZ = False
        settings.TASKS = tasks_setting(
            {"iv": {"task": "tests.tasks.add", "every": 1800}}
        )
        assert "django_ox.W001" not in [e.id for e in default_task_backend.check()]
