"""Dispatching schedules that live in the database."""

from datetime import timedelta

import pytest
from django import forms
from django.utils import timezone

from django_ox.models import OxSchedule, OxScheduleTick, OxTask
from django_ox.registry import ArgsForm, ScheduleKind, register
from django_ox.stored import (
    boundary_digest,
    create_schedule,
    update_schedule,
)
from django_ox.worker import Worker

from . import tasks


class _Args(ArgsForm):
    region = forms.CharField()


def tasks_setting():
    return {
        "default": {
            "BACKEND": "django_ox.backend.OxBackend",
            "QUEUES": ["default"],
            "OPTIONS": {
                "SCHEDULE_SOURCE": "django_ox.stored.DatabaseScheduleSource",
            },
        }
    }


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.add))
    register(ScheduleKind(key="checked", task=tasks.add, form=_Args))


@pytest.fixture
def worker(settings):
    settings.TASKS = tasks_setting()
    return Worker(backoff_initial=0)


def a_minutely(**over):
    # The boundary is backdated by default so the current minute's tick is
    # at or after it. A schedule created at 14:37:41 has its 14:37:00 tick
    # *before* its boundary and correctly waits for 14:38:00, which is right
    # but makes for a slow test.
    fields = {
        "name": "minutely",
        "task_key": "report",
        "trigger": "cron",
        "cron": "* * * * *",
        "start_time": timezone.now() - timedelta(minutes=5),
    }
    fields.update(over)
    return create_schedule(**fields)


pytestmark = pytest.mark.django_db


class TestABoundaryReplacesTheAnchor:
    def test_a_row_fires_on_its_first_due_tick(self, worker):
        # The whole point of start_time. A settings schedule would record an
        # anchor here and enqueue nothing, so a row created while every
        # worker was down would lose its first run.
        a_minutely()
        assert worker.dispatch_schedules() == 1
        assert OxTask.objects.count() == 1

    def test_a_tick_before_the_boundary_does_not_fire(self, worker):
        a_minutely(start_time=timezone.now() + timedelta(hours=1))
        assert worker.dispatch_schedules() == 0
        assert OxScheduleTick.objects.count() == 0

    def test_a_tick_after_the_end_time_does_not_fire(self, worker):
        row = a_minutely()
        update_schedule(row, end_time=row.start_time + timedelta(seconds=1))
        assert worker.dispatch_schedules() == 0

    def test_the_same_tick_fires_only_once(self, worker):
        a_minutely()
        assert worker.dispatch_schedules() == 1
        assert worker.dispatch_schedules() == 0
        assert OxTask.objects.count() == 1


class TestTheDisableRace:
    """
    The window no poll interval can close.

    A worker reads a schedule while its row is enabled, the row changes,
    and the worker then reaches dispatch still holding what it read.
    Re-reading more often shrinks the window and never removes it, so the
    check has to happen under the row's own lock inside the dispatch
    transaction.

    Each test pins the worker's view to what it read *before* the change,
    by replacing the source's answer outright. Priming the cache instead
    would be read back through the freshness check and quietly refreshed,
    which makes the test pass for the wrong reason.
    """

    def _hold_stale_view(self, worker, monkeypatch):
        stale = worker._schedule_source.schedules()
        assert stale, "the worker should be holding a schedule"
        monkeypatch.setattr(worker._schedule_source, "schedules", lambda: stale)
        return stale

    def test_a_schedule_disabled_after_it_was_read_does_not_fire(
        self, worker, monkeypatch
    ):
        row = a_minutely()
        self._hold_stale_view(worker, monkeypatch)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=False)

        assert worker.dispatch_schedules() == 0
        assert OxTask.objects.count() == 0
        assert not OxScheduleTick.objects.exists(), (
            "a refused tick must stay unclaimed so a current worker can act"
        )

    def test_a_schedule_retimed_after_it_was_read_does_not_fire(
        self, worker, monkeypatch
    ):
        # The ticks this worker computed are no longer this schedule's ticks.
        row = a_minutely()
        self._hold_stale_view(worker, monkeypatch)
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 3 * * *")

        assert worker.dispatch_schedules() == 0
        assert OxTask.objects.count() == 0

    def test_a_deleted_schedule_does_not_fire(self, worker, monkeypatch):
        row = a_minutely()
        self._hold_stale_view(worker, monkeypatch)
        OxSchedule.objects.filter(pk=row.pk).delete()

        assert worker.dispatch_schedules() == 0
        assert OxTask.objects.count() == 0


@pytest.fixture
def half_past(monkeypatch):
    """
    A clock pinned well past the hour.

    An hourly tick with a sixty-second deadline is only late once the hour
    is a minute old. On the wall clock these tests would pass for
    fifty-nine minutes in sixty and fail in the first, which says nothing
    about the code.
    """
    from django.utils import timezone as tz

    pinned = tz.now().replace(minute=30, second=0, microsecond=0)
    monkeypatch.setattr(tz, "now", lambda: pinned)
    return pinned


class TestTheStartingDeadline:
    @pytest.mark.usefixtures("half_past")
    def test_a_tick_later_than_the_deadline_is_dropped(self, worker):
        row = create_schedule(
            name="hourly",
            task_key="report",
            trigger="cron",
            cron="0 * * * *",
            start_time=timezone.now() - timedelta(days=2),
            starting_deadline_seconds=60,
        )
        assert row.pk
        assert worker.dispatch_schedules() == 0

    @pytest.mark.usefixtures("half_past")
    def test_without_a_deadline_a_late_tick_still_fires(self, worker):
        create_schedule(
            name="hourly",
            task_key="report",
            trigger="cron",
            cron="0 * * * *",
            start_time=timezone.now() - timedelta(days=2),
        )
        assert worker.dispatch_schedules() == 1


class TestABadRowDoesNotStopTheOthers:
    def test_an_unknown_key_is_skipped_and_the_tick_stays_unclaimed(self, worker):
        # The rolling-deploy case: an older worker meets a key only newer
        # code registers. If it claimed the tick, the worker that could run
        # it would be suppressed by the unique constraint and that tick would
        # silently never fire.
        unknown = OxSchedule.objects.create(
            name="future",
            task_key="only.in.new.code",
            trigger="cron",
            cron="* * * * *",
            start_time=timezone.now() - timedelta(minutes=5),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        a_minutely()
        assert worker.dispatch_schedules() == 1
        # The tick must be left unclaimed, or a worker that knows the key
        # would be suppressed by the unique constraint and it would never
        # fire. Asserted against the row's dispatch key: filtering on the
        # display name matches nothing and would pass however broken this is.
        assert not OxScheduleTick.objects.filter(
            schedule_name=f"db:{unknown.pk}"
        ).exists()

    def test_a_row_whose_arguments_no_longer_validate_is_skipped(self, worker):
        stale = OxSchedule.objects.create(
            name="stale-args",
            task_key="checked",
            trigger="cron",
            cron="* * * * *",
            arguments={"gone": 1},
            start_time=timezone.now() - timedelta(minutes=5),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        a_minutely()
        assert worker.dispatch_schedules() == 1
        assert not OxScheduleTick.objects.filter(
            schedule_name=f"db:{stale.pk}"
        ).exists()

    def test_a_row_with_an_unparseable_cron_is_skipped(self, worker):
        OxSchedule.objects.create(
            name="bad-cron",
            task_key="report",
            trigger="cron",
            cron="banana",
            start_time=timezone.now() - timedelta(minutes=5),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        a_minutely()
        assert worker.dispatch_schedules() == 1


class TestFreshness:
    def test_an_unchanged_marker_costs_one_query(
        self, worker, django_assert_num_queries
    ):
        a_minutely()
        worker._schedule_source.schedules()
        with django_assert_num_queries(1):
            worker._schedule_source.schedules()

    def test_a_change_is_noticed(self, worker):
        source = worker._schedule_source
        assert source.schedules() == []
        a_minutely()
        assert [s.name for s in source.schedules()] == ["minutely"]

    def test_a_disabled_schedule_leaves_the_set(self, worker):
        row = a_minutely()
        source = worker._schedule_source
        assert len(source.schedules()) == 1
        update_schedule(row, enabled=False)
        assert source.schedules() == []


def test_a_schedule_created_mid_minute_waits_for_the_next_tick(worker):
    # Created at 14:37:41 with a minutely cron, the 14:37:00 tick is before
    # the boundary: at that instant the schedule did not exist. It fires at
    # 14:38:00, not immediately.
    create_schedule(
        name="just-now", task_key="report", trigger="cron", cron="* * * * *"
    )
    assert worker.dispatch_schedules() == 0
    assert not OxScheduleTick.objects.exists()


class TestABadRowCannotStopTheWorker:
    def test_a_zero_interval_row_is_skipped_and_the_others_still_fire(self, worker):
        # IntervalTrigger(every=0) raises
        # ZeroDivisionError, which is not a ValueError, so the per-row guard
        # missed it and it reached the dispatch loop.
        zero = OxSchedule.objects.create(
            name="zero",
            task_key="report",
            trigger="interval",
            cron="",
            every_seconds=0,
            start_time=timezone.now() - timedelta(minutes=5),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        a_minutely()
        assert worker.dispatch_schedules() == 1
        assert not OxScheduleTick.objects.filter(schedule_name=f"db:{zero.pk}").exists()

    def test_a_null_interval_row_cannot_be_created_at_all(self):
        # The check constraint refuses it, so the runtime guard never has to see
        # this shape. Zero still gets through, because zero is not null, which
        # is why both defences exist.
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            OxSchedule.objects.create(
                name="null-interval",
                task_key="report",
                trigger="interval",
                cron="",
                every_seconds=None,
                start_time=timezone.now() - timedelta(minutes=5),
                created_at=timezone.now(),
                updated_at=timezone.now(),
            )

    def test_non_mapping_arguments_are_skipped(self, worker):
        OxSchedule.objects.create(
            name="listargs",
            task_key="report",
            trigger="cron",
            cron="* * * * *",
            arguments=[1, 2],
            start_time=timezone.now() - timedelta(minutes=5),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        a_minutely()
        assert worker.dispatch_schedules() == 1


class TestDeadlineValidation:
    def test_a_zero_deadline_is_refused(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as caught:
            a_minutely(name="d", starting_deadline_seconds=0)
        assert "starting_deadline_seconds" in caught.value.message_dict


class TestDeleteTellsTheWorkers:
    def test_deleting_a_schedule_bumps_the_change_row(self, worker):
        from django_ox.models import OxScheduleChange
        from django_ox.stored import delete_schedule

        row = a_minutely()
        before = OxScheduleChange.objects.get(id=1).changed_at
        delete_schedule(row)
        assert OxScheduleChange.objects.get(id=1).changed_at > before
        assert worker._schedule_source.schedules() == []


class TestRenamingCannotSplitTheCoordination:
    """
    A rename must not produce two tasks for one tick.

    Ticks are keyed on the dispatch log's name column; admission is keyed on
    the row. When the label a person edits was also the coordination key, two
    workers holding different labels for one row wrote two rows for the same
    instant, and the unique constraint saw nothing in common between them.
    """

    def _hold(self, worker, monkeypatch):
        held = worker._schedule_source.schedules()
        assert held
        monkeypatch.setattr(worker._schedule_source, "schedules", lambda: held)
        return held

    def test_a_rename_mid_flight_still_fires_once(self, worker, monkeypatch):
        row = a_minutely(name="old")
        self._hold(worker, monkeypatch)  # worker holds name="old"
        update_schedule(row, name="new")
        other = Worker(backoff_initial=0)  # reads name="new"

        worker.dispatch_schedules()
        other.dispatch_schedules()

        assert OxTask.objects.count() == 1, "one tick produced more than one task"
        assert OxScheduleTick.objects.count() == 1

    def test_the_tick_is_keyed_on_the_row_not_the_label(self, worker):
        row = a_minutely(name="labelled")
        worker.dispatch_schedules()
        assert OxScheduleTick.objects.get().schedule_name == f"db:{row.pk}"

    def test_renaming_preserves_tick_history(self, worker):
        row = a_minutely(name="before")
        worker.dispatch_schedules()
        before = set(OxScheduleTick.objects.values_list("schedule_name", flat=True))
        update_schedule(row, name="after")
        assert (
            set(OxScheduleTick.objects.values_list("schedule_name", flat=True))
            == before
        ), "a rename must not orphan the schedule's own history"

    def test_a_settings_schedule_may_not_use_the_reserved_prefix(self):
        from django.core.exceptions import ImproperlyConfigured

        from django_ox.schedules import schedules_from_options

        with pytest.raises(ImproperlyConfigured, match="reserved"):
            schedules_from_options(
                {
                    "SCHEDULES": {
                        "db:1": {"task": "tests.tasks.add", "cron": "* * * * *"}
                    }
                },
                "default",
            )


class TestTheDecisionComesFromTheRow:
    """
    A snapshot chooses candidates. The row decides.

    Each test here changes something after a worker has read the schedule
    and before it dispatches. None of them fires,
    because admission re-checked two columns and these are not those two.
    """

    def _hold(self, worker, monkeypatch):
        held = worker._schedule_source.schedules()
        assert held
        monkeypatch.setattr(worker._schedule_source, "schedules", lambda: held)
        return held

    def test_a_resumed_schedule_does_not_fire_a_pre_resume_tick(
        self, worker, monkeypatch
    ):
        # Pause and resume moves the boundary forward. A worker still holding
        # the pre-pause view would otherwise fire a tick from before it, which
        # is the retroactive run the boundary exists to prevent.
        row = a_minutely()
        self._hold(worker, monkeypatch)
        update_schedule(row, enabled=False)
        update_schedule(row, enabled=True)
        assert worker.dispatch_schedules() == 0
        assert not OxScheduleTick.objects.exists()

    def test_a_bulk_retime_does_not_fire_retroactively(self, worker, monkeypatch):
        # queryset.update() runs no model code at all, so nothing at write
        # time notices. The tick is recomputed from the row instead.
        row = a_minutely(
            cron="0 2 * * *", start_time=timezone.now() - timedelta(days=2)
        )
        self._hold(worker, monkeypatch)
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 3 * * *")
        assert worker.dispatch_schedules() == 0

    def test_a_tightened_deadline_drops_the_tick(self, worker, monkeypatch):
        # The clock is frozen well past the tick. Left to the wall clock,
        # the assertion would be that a minutely tick is more than a second old,
        # which is false for the first second of every minute: a one-in-
        # sixty failure that says nothing about the code.
        from django.utils import timezone as tz

        row = a_minutely(
            cron="0 * * * *", start_time=timezone.now() - timedelta(days=1)
        )
        self._hold(worker, monkeypatch)
        update_schedule(row, starting_deadline_seconds=60)
        frozen = tz.now().replace(minute=30, second=0, microsecond=0)
        monkeypatch.setattr(tz, "now", lambda: frozen)
        assert worker.dispatch_schedules() == 0

    def test_a_moved_end_time_stops_the_tick(self, worker, monkeypatch):
        row = a_minutely()
        self._hold(worker, monkeypatch)
        OxSchedule.objects.filter(pk=row.pk).update(
            end_time=row.start_time + timedelta(seconds=1)
        )
        assert worker.dispatch_schedules() == 0

    def test_the_task_that_runs_is_the_one_the_row_names_now(self, worker, monkeypatch):
        # The snapshot's task is not enqueued; the row's is.
        row = a_minutely()
        self._hold(worker, monkeypatch)
        update_schedule(row, task_key="checked", arguments={"region": "emea"})
        assert worker.dispatch_schedules() == 1
        assert OxTask.objects.count() == 1
        assert "emea" in str(OxTask.objects.get().kwargs)

    def test_a_due_tick_costs_a_bounded_number_of_queries(
        self, worker, django_assert_max_num_queries
    ):
        # Deciding from the row adds a statement on the due-tick path.
        # Measured rather than assumed, and a bound rather than a number:
        # PostgreSQL and MySQL take the lock with one locking read, SQLite
        # has no row locks and takes a no-op write and then a read, so the
        # count differs by database and SQLite is the expensive one.
        #
        # This is the due path, which runs at most once a minute per
        # schedule. The polling path is unchanged. One of the nine is the
        # UPDATE that attaches the task to a tick row written before the
        # enqueue, so a worker that loses the tick announces nothing.
        a_minutely()
        worker._schedule_source.schedules()
        with django_assert_max_num_queries(9):
            worker.dispatch_schedules()


class TestTheBoundaryMustMatchTheTiming:
    """
    A write that runs no model code leaves the boundary set for the old
    timing. Dispatch notices, refuses the tick, and moves the boundary so
    the schedule resumes rather than being refused forever.
    """

    def test_a_fresh_worker_does_not_fire_a_raw_retimed_tick(self, worker):
        # Nobody holds a stale snapshot here: the worker reads the row after
        # the change. Comparing two reads cannot catch this; comparing the
        # boundary against the timing can.
        a_minutely(cron="0 2 * * *", start_time=timezone.now() - timedelta(days=2))
        OxSchedule.objects.filter(name="minutely").update(cron="0 3 * * *")
        assert Worker(backoff_initial=0).dispatch_schedules() == 0

    def test_the_boundary_is_moved_onto_the_new_timing(self, worker):
        from django_ox.stored import boundary_digest

        row = a_minutely(
            cron="0 2 * * *", start_time=timezone.now() - timedelta(days=2)
        )
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 3 * * *")
        for _ in range(3):
            worker.dispatch_schedules()
        row.refresh_from_db()
        assert row.boundary_for == boundary_digest(row)

    def test_a_raw_pause_and_resume_with_no_read_between_is_not_detected(self, worker):
        """
        The documented limit, asserted so it cannot drift into a surprise.

        A digest cannot see a round trip. Disabling and re-enabling outside
        the write API with no read in between leaves every column it covers
        exactly as it found them, so the boundary still looks current and a
        tick from before the resume can fire. update_schedule moves the
        boundary at the moment of the change; queryset.update leaves it to
        the next read.
        """
        row = a_minutely()
        worker._schedule_source.schedules()
        OxSchedule.objects.filter(pk=row.pk).update(enabled=False)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=True)
        assert worker.dispatch_schedules() == 1

    def test_a_raw_pause_a_read_found_does_not_replay_on_a_raw_resume(
        self, worker, monkeypatch
    ):
        """
        Pause at T+30 with queryset.update, let the T+60 tick pass while
        paused, resume at T+80 the same way, and the pass at T+90 must not
        fire T+60. The pause was seen by a read at T+40, so the boundary
        moved then; the resume is seen at T+90 and moves it again.
        """
        from django.utils import timezone as tz

        base = tz.now().replace(second=0, microsecond=0)
        clock = {"now": base}
        monkeypatch.setattr(tz, "now", lambda: clock["now"])
        row = a_minutely(start_time=base - timedelta(minutes=5))
        worker._schedule_source._reconcile_interval = 0

        def at(seconds):
            clock["now"] = base + timedelta(seconds=seconds)

        at(10)
        assert worker.dispatch_schedules() == 1, "T fires"
        at(30)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=False)
        at(40)
        assert worker.dispatch_schedules() == 0, "paused"
        at(80)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=True)
        at(90)
        assert worker.dispatch_schedules() == 0, "T+60 came due inside the pause"
        fired = sorted(
            (t - base).total_seconds()
            for t in OxScheduleTick.objects.exclude(task_id=None).values_list(
                "scheduled_for", flat=True
            )
        )
        assert fired == [0], f"replayed a tick from inside the pause: {fired}"
        at(130)
        assert worker.dispatch_schedules() == 1, "the schedule has resumed"

    def test_a_raw_pause_met_at_dispatch_does_not_replay_on_a_raw_resume(
        self, worker, monkeypatch
    ):
        """
        The other way a pause is found: no full read falls between the
        pause and the tick, so the worker meets the disabled row under the
        lock at dispatch. That sighting has to move the boundary too, on the
        next pass, or a raw resume found by the next full read replays the
        tick that came due inside the pause.
        """
        from django.utils import timezone as tz

        base = tz.now().replace(second=0, microsecond=0)
        clock = {"now": base}
        monkeypatch.setattr(tz, "now", lambda: clock["now"])
        row = a_minutely(start_time=base - timedelta(minutes=5))
        source = worker._schedule_source

        def at(seconds):
            clock["now"] = base + timedelta(seconds=seconds)

        at(10)
        assert worker.dispatch_schedules() == 1, "T fires"
        at(30)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=False)
        # The reconcile interval is on the monotonic clock and has not
        # elapsed, and a bulk update bumps no marker, so the cached copy
        # stands and the pause is met under the lock.
        at(70)
        assert worker.dispatch_schedules() == 0, "refused under the lock"
        assert row.pk in source._needs_heal, "the pause seen at dispatch was not kept"
        at(75)
        assert worker.dispatch_schedules() == 0
        at(80)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=True)
        source._reconcile_interval = 0  # the next call is a full read
        at(90)
        assert worker.dispatch_schedules() == 0, "T+60 came due inside the pause"
        fired = sorted(
            (t - base).total_seconds()
            for t in OxScheduleTick.objects.exclude(task_id=None).values_list(
                "scheduled_for", flat=True
            )
        )
        assert fired == [0], f"replayed a tick from inside the pause: {fired}"
        at(130)
        assert worker.dispatch_schedules() == 1, "the schedule has resumed"

    def test_a_raw_resume_before_the_heal_does_not_cancel_it(self, worker, monkeypatch):
        """
        The pause is met under the lock at T+70 and queued for the next
        pass's heal. The raw resume lands at T+72, before that pass. At
        T+75 the row's digest matches its boundary again, because
        `enabled` is back to the value the boundary was set for, and a
        heal that asked only whether the digest matched would skip. The
        boundary must still move: the row was seen in a state it was not
        set for, and nothing else has moved it since.
        """
        from django.utils import timezone as tz

        base = tz.now().replace(second=0, microsecond=0)
        clock = {"now": base}
        monkeypatch.setattr(tz, "now", lambda: clock["now"])
        row = a_minutely(start_time=base - timedelta(minutes=5))
        source = worker._schedule_source

        def at(seconds):
            clock["now"] = base + timedelta(seconds=seconds)

        at(10)
        assert worker.dispatch_schedules() == 1, "T fires"
        at(30)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=False)
        at(70)
        assert worker.dispatch_schedules() == 0, "refused under the lock"
        assert row.pk in source._needs_heal, "the pause seen at dispatch was not kept"
        at(72)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=True)
        at(75)
        assert worker.dispatch_schedules() == 0, "the heal pass; not a full read"
        assert not source._needs_heal
        row.refresh_from_db()
        assert row.start_time == base + timedelta(seconds=75), (
            "the heal was cancelled by the resume restoring the digest"
        )
        source._reconcile_interval = 0  # the next call is a full read
        at(90)
        assert worker.dispatch_schedules() == 0, "T+60 came due inside the pause"
        fired = sorted(
            (t - base).total_seconds()
            for t in OxScheduleTick.objects.exclude(task_id=None).values_list(
                "scheduled_for", flat=True
            )
        )
        assert fired == [0], f"replayed a tick from inside the pause: {fired}"
        at(130)
        assert worker.dispatch_schedules() == 1, "the schedule has resumed"

    def test_a_boundary_someone_else_moved_is_left_alone(self, worker, monkeypatch):
        """
        The other half of the same check. A second worker healed the row,
        or the write API resumed it, between this worker's sighting and
        its heal: the boundary column and start time differ from the ones
        this worker saw, and their boundary stands rather than being
        moved again to this worker's later clock.
        """
        from django.utils import timezone as tz

        base = tz.now().replace(second=0, microsecond=0)
        clock = {"now": base}
        monkeypatch.setattr(tz, "now", lambda: clock["now"])
        row = a_minutely(start_time=base - timedelta(minutes=5))
        source = worker._schedule_source

        def at(seconds):
            clock["now"] = base + timedelta(seconds=seconds)

        at(10)
        assert worker.dispatch_schedules() == 1
        at(30)
        OxSchedule.objects.filter(pk=row.pk).update(enabled=False)
        at(70)
        assert worker.dispatch_schedules() == 0
        assert row.pk in source._needs_heal
        at(72)
        update_schedule(row, enabled=True)  # moves the boundary to T+72
        at(75)
        worker.dispatch_schedules()
        row.refresh_from_db()
        assert row.start_time == base + timedelta(seconds=72), (
            "a boundary the write API had already moved was moved again"
        )

    def test_the_same_pause_through_the_write_api_is_detected(self, worker):
        row = a_minutely()
        worker._schedule_source.schedules()
        update_schedule(row, enabled=False)
        update_schedule(row, enabled=True)
        assert worker.dispatch_schedules() == 0


class TestTheDeadlineIsJudgedUnderTheLock:
    """
    The starting deadline is a promise about how late a run may begin.

    A pass samples the clock once before its loop and then waits for the
    row's lock inside the transaction: for another dispatcher's enqueue and
    its receivers, for an admin save, up to the lock-wait timeout on MySQL.
    A tick inside its deadline at that first sample can be past it by the
    time the lock is granted, and judged against the first sample it was
    enqueued late. The clock is read again under the lock.

    The wait is stood in for by a lock that moves the clock: what the test
    pins is the order, that the deadline is judged after the lock and not
    before it, which a real wait would only demonstrate more slowly.
    """

    def _clock_that_advances_inside_the_lock(self, monkeypatch, before, after):
        from django_ox import stored
        from django_ox import worker as worker_module

        moment = {"now": before}
        monkeypatch.setattr(worker_module.timezone, "now", lambda: moment["now"])
        lock_row = stored._lock_row

        def lock_row_after_a_wait(pk, alias):
            row = lock_row(pk, alias)
            moment["now"] = after
            return row

        monkeypatch.setattr(stored, "_lock_row", lock_row_after_a_wait)

    def test_a_tick_past_its_deadline_once_the_lock_is_granted_is_refused(
        self, worker, monkeypatch
    ):
        a_minutely(starting_deadline_seconds=30)
        tick = timezone.now().replace(second=0, microsecond=0)
        self._clock_that_advances_inside_the_lock(
            monkeypatch,
            before=tick + timedelta(seconds=10),
            after=tick + timedelta(seconds=45),
        )
        assert worker.dispatch_schedules() == 0
        assert OxTask.objects.count() == 0
        assert OxScheduleTick.objects.count() == 0, "a refused tick leaves no row"

    def test_a_wait_that_stays_inside_the_deadline_still_fires(
        self, worker, monkeypatch
    ):
        a_minutely(starting_deadline_seconds=30)
        tick = timezone.now().replace(second=0, microsecond=0)
        self._clock_that_advances_inside_the_lock(
            monkeypatch,
            before=tick + timedelta(seconds=10),
            after=tick + timedelta(seconds=20),
        )
        assert worker.dispatch_schedules() == 1
        assert OxTask.objects.count() == 1


class TestTheRowLockDistinguishesContentionFromTheDatabase:
    """
    The stored source skips a schedule whose row lock the database gave up
    waiting for, and only that. A connection gone away at the same
    statement is the database's failure, and it ends the pass so run()
    reports it and drops the connection.
    """

    def _lock_row_raising(self, monkeypatch, exc):
        from django_ox import stored

        def lock_row(pk, alias):
            raise exc

        monkeypatch.setattr(stored, "_lock_row", lock_row)

    def test_a_lock_the_database_gave_up_on_skips_the_schedule(
        self, worker, monkeypatch, caplog
    ):
        import logging

        from django.db import OperationalError

        a_minutely()
        self._lock_row_raising(monkeypatch, OperationalError("database is locked"))
        with caplog.at_level(logging.WARNING, logger="django_ox"):
            assert worker.dispatch_schedules() == 0
        warned = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_lock_unavailable"
        ]
        assert len(warned) == 1
        assert warned[0].exc_info is None, "contention is not a traceback"

    def test_a_connection_gone_away_at_the_lock_ends_the_pass(
        self, worker, monkeypatch, caplog
    ):
        import logging

        from django.db import DatabaseError, OperationalError

        a_minutely()
        self._lock_row_raising(
            monkeypatch, OperationalError("server closed the connection unexpectedly")
        )
        with (
            caplog.at_level(logging.WARNING, logger="django_ox"),
            pytest.raises(DatabaseError),
        ):
            worker.dispatch_schedules()
        assert not any(
            getattr(r, "event", None) == "schedule_lock_unavailable"
            for r in caplog.records
        ), "a dead connection was reported as lock contention"


class TestTheWriteApiCannotLoseAnUpdate:
    def test_a_stale_instance_cannot_undo_a_resume(self):
        # Editor A holds a copy, editor B pauses and resumes, then A saves an
        # unrelated field. A's copy carries the old boundary, and writing
        # every field from it would put that boundary back.
        row = a_minutely()
        stale = OxSchedule.objects.get(pk=row.pk)
        update_schedule(row, enabled=False)
        update_schedule(row, enabled=True)
        resumed_boundary = OxSchedule.objects.get(pk=row.pk).start_time

        update_schedule(stale, name="renamed")

        after = OxSchedule.objects.get(pk=row.pk)
        assert after.name == "renamed"
        assert after.start_time == resumed_boundary, (
            "a stale instance wrote its old boundary over the resumed one"
        )


class TestNothingUnserialisableReachesTheEnqueue:
    """
    A task is enqueued as JSON. A form declares what its arguments clean to,
    and only some of those survive that. Checked at the write, at the read,
    and caught at dispatch, because a row can be written around all of it.
    """

    def _register(self, key, field):

        from django_ox.registry import ArgsForm, ScheduleKind, register

        ns = {"value": field}
        form = type("_F", (ArgsForm,), ns)
        register(ScheduleKind(key=key, task=tasks.add, form=form))
        return form

    @pytest.mark.parametrize(
        ("field_name", "raw"),
        [
            ("DateField", "2026-01-01"),
            ("DateTimeField", "2026-01-01 00:00"),
            ("DecimalField", "1.5"),
            ("DurationField", "1:00:00"),
            ("UUIDField", "8c8b0a5e-0a4e-4a6e-9b6a-3f7c1d2e5a90"),
        ],
    )
    def test_a_form_cleaning_to_a_non_json_value_is_refused_at_the_write(
        self, field_name, raw
    ):
        from django import forms
        from django.core.exceptions import ValidationError

        self._register("dated", getattr(forms, field_name)())
        with pytest.raises(ValidationError) as caught:
            a_minutely(name="dated", task_key="dated", arguments={"value": raw})
        assert "arguments" in caught.value.message_dict

    def test_such_a_row_written_around_validation_does_not_stop_the_others(
        self, worker
    ):
        # The failure this class exists to prevent: an error escaping the
        # enqueue stops the healthy schedule beside it from firing.
        from django import forms

        self._register("dated", forms.DateField())
        OxSchedule.objects.create(
            name="dated",
            task_key="dated",
            trigger="cron",
            cron="* * * * *",
            arguments={"value": "2026-01-01"},
            start_time=timezone.now() - timedelta(minutes=5),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        a_minutely()
        assert worker.dispatch_schedules() == 1
        assert OxTask.objects.count() == 1

    def test_an_unexpected_error_skips_one_schedule_not_the_pass(
        self, worker, monkeypatch
    ):
        # Not a value problem: whatever goes wrong for one schedule, the
        # others in the same pass still run.
        #
        # Injected at _to_schedule rather than into the snapshot, because
        # dispatch rebuilds the schedule from the row inside the transaction
        # and a stub placed on the snapshot is discarded before it is used.
        import dataclasses

        class _Explodes:
            backend = "default"

            def enqueue(self, *args, **kwargs):
                raise RuntimeError("something nobody predicted")

        a_minutely(name="first")
        a_minutely(name="second")

        source = worker._schedule_source
        build = source._to_schedule

        def sabotage(row):
            built = build(row)
            if row.name == "first":
                return dataclasses.replace(built, task=_Explodes())
            return built

        monkeypatch.setattr(source, "_to_schedule", sabotage)

        worker.dispatch_schedules()
        assert OxTask.objects.count() == 1, "the healthy schedule did not run"


class TestARowChangedOutsideTheWriteApiIsFound:
    """
    The change marker is bumped by this package's write functions and by
    nothing else, so a raw write moves nothing a worker watches. Detection
    sits where the rows are read, not inside the dispatch transaction. A row
    only reaches that transaction
    if the cached copy says a tick is due, so a row whose cached copy said
    otherwise was never examined at all.
    """

    def _source(self, worker, interval=0.0001):
        source = worker._schedule_source
        source._reconcile_interval = interval
        return source

    def test_a_passed_end_time_does_not_hide_a_retime(self, worker):
        # The cached copy says the schedule ended, so no tick is ever due
        # and the dispatch transaction is never entered. Reading the rows is
        # the only thing that can notice.
        row = a_minutely(
            cron="0 * * * *",
            start_time=timezone.now() - timedelta(days=2),
            end_time=timezone.now() - timedelta(hours=2),
        )
        source = self._source(worker)
        source.schedules()
        OxSchedule.objects.filter(pk=row.pk).update(
            trigger="interval", cron="", every_seconds=60, end_time=None
        )
        for _ in range(3):
            worker.dispatch_schedules()
        row.refresh_from_db()
        assert row.boundary_for == boundary_digest(row), (
            "the row changed and no worker ever noticed"
        )

    def test_a_row_re_enabled_outside_the_write_api_is_found(self, worker):
        # Disabled rows are not read at all, so this one is neither in the
        # cache to refresh nor in the query that builds one.
        row = a_minutely()
        update_schedule(row, enabled=False)
        source = self._source(worker)
        assert source.schedules() == []
        OxSchedule.objects.filter(pk=row.pk).update(enabled=True)
        assert [s.name for s in source.schedules()] == ["minutely"]

    def test_a_row_created_outside_the_write_api_is_found(self, worker):
        source = self._source(worker)
        assert source.schedules() == []
        OxSchedule.objects.create(
            name="raw",
            task_key="report",
            trigger="cron",
            cron="* * * * *",
            start_time=timezone.now() - timedelta(minutes=5),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        assert [s.name for s in source.schedules()] == ["raw"]

    def test_the_marker_still_short_circuits_between_reconciles(
        self, worker, django_assert_num_queries
    ):
        # The backstop must not turn every pass into a full read. Between
        # reconciles a pass costs exactly the one marker read.
        a_minutely()
        source = worker._schedule_source
        source._reconcile_interval = 3600
        source.schedules()
        with django_assert_num_queries(1):
            source.schedules()

    def test_the_reconcile_reads_the_rows_again(
        self, worker, django_assert_num_queries
    ):
        # And when it is due, it costs the marker read plus the row read.
        a_minutely()
        source = worker._schedule_source
        source._reconcile_interval = 0.0001
        source.schedules()
        with django_assert_num_queries(2):
            source.schedules()

    def test_a_disabled_row_leaves_the_cache_when_dispatch_finds_it_gone(
        self, worker, monkeypatch
    ):
        row = a_minutely()
        source = worker._schedule_source
        source._reconcile_interval = 3600
        held = source.schedules()
        assert len(held) == 1
        OxSchedule.objects.filter(pk=row.pk).update(enabled=False)
        worker.dispatch_schedules()
        assert source._cached == [], "a schedule that cannot fire stayed cached"

    def test_a_retime_is_found_without_waiting_for_the_next_tick(self, worker):
        # A yearly schedule retimed in June. No tick is due for months, so
        # the dispatch transaction is never entered and the check inside it
        # never runs. Reading the rows is the only thing that can notice.
        row = a_minutely(
            cron="0 3 1 1 *",  # 03:00 on 1 January
            start_time=timezone.now() - timedelta(days=2),
        )
        source = self._source(worker)
        source.schedules()
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 4 1 1 *")
        for _ in range(3):
            worker.dispatch_schedules()
        row.refresh_from_db()
        assert row.boundary_for == boundary_digest(row), (
            "a retime went unnoticed because no tick was due to carry it"
        )


class TestADroppedTickIsReportedOnce:
    def test_a_tick_already_recorded_is_not_reported_as_dropped(
        self, worker, caplog, monkeypatch
    ):
        # The deadline is checked after the already-fired suppression: a
        # tick that has run is not a tick that was dropped. Checking first
        # would re-report it on every later pass, which for a daily
        # schedule is a warning a second for a day on an event the docs
        # say can be alerted on.
        #
        # The clock has to move: the tick must be fresh when it fires and
        # stale when it is looked at again, which is the whole shape of
        # the case.
        import logging

        from django.utils import timezone as tz

        a_minutely(
            cron="0 * * * *",
            starting_deadline_seconds=120,
            start_time=tz.now() - timedelta(days=2),
        )
        real_now = tz.now().replace(minute=0, second=1, microsecond=0)
        clock = {"now": real_now}
        monkeypatch.setattr(tz, "now", lambda: clock["now"])

        assert worker.dispatch_schedules() == 1, "it should fire while fresh"

        clock["now"] = real_now + timedelta(minutes=30)  # same tick, now stale
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="django_ox"):
            for _ in range(5):
                worker.dispatch_schedules()
        dropped = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_tick_dropped"
        ]
        assert dropped == [], (
            f"a tick that already fired was reported dropped {len(dropped)} times"
        )

    @pytest.mark.usefixtures("half_past")
    def test_a_genuinely_late_tick_is_still_reported(self, worker, caplog):
        import logging

        a_minutely(
            cron="0 * * * *",
            start_time=timezone.now() - timedelta(days=2),
            starting_deadline_seconds=60,
        )
        with caplog.at_level(logging.WARNING, logger="django_ox"):
            worker.dispatch_schedules()
        assert any(
            getattr(r, "event", None) == "schedule_tick_dropped" for r in caplog.records
        )

    @pytest.mark.usefixtures("half_past")
    def test_the_same_dropped_tick_is_reported_once_not_once_a_pass(
        self, worker, caplog
    ):
        # A dropped tick writes no row, so nothing else stops it being
        # recomputed and reported again on every pass until its next tick
        # comes due. The docs tell operators to alert on this event, and an
        # alert that fires once a second for a day cannot be acted on.
        import logging

        a_minutely(
            cron="0 * * * *",
            start_time=timezone.now() - timedelta(days=2),
            starting_deadline_seconds=60,
        )
        with caplog.at_level(logging.WARNING, logger="django_ox"):
            for _ in range(20):
                worker.dispatch_schedules()
        dropped = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_tick_dropped"
        ]
        assert len(dropped) == 1, f"reported {len(dropped)} times over 20 passes"


class TestAnIntegrityErrorFromTheEnqueueIsNotALostRace:
    """
    The enqueue and the tick INSERT share one transaction, so an integrity
    failure raised by the task write surfaces the same way a lost race
    does. What separates them is how far the block had got: a lost race is
    the tick INSERT itself failing, and a failing enqueue comes after this
    pass's own tick row went in. Read as a lost race, a real failure would
    be retried silently for as long as it kept failing.
    """

    def test_a_failing_enqueue_is_reported(self, worker, caplog, monkeypatch):
        import logging

        from django.db import IntegrityError

        a_minutely()

        def boom(*args, **kwargs):
            raise IntegrityError("the task write failed")

        monkeypatch.setattr("django_ox.backend.OxBackend.enqueue", boom, raising=True)
        with caplog.at_level(logging.ERROR, logger="django_ox"):
            assert worker.dispatch_schedules() == 0
        assert [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_dispatch_error"
        ], "a failing enqueue was read as another worker winning the race"
        assert not OxScheduleTick.objects.exists(), "a tick was claimed anyway"

    @pytest.mark.django_db(transaction=True)
    def test_a_failing_enqueue_is_reported_even_as_another_worker_claims_the_tick(
        self, worker, caplog, monkeypatch
    ):
        # The log cannot tell the two apart: a winner whose INSERT was
        # waiting on this pass's uncommitted row lands the moment this pass
        # rolls back, so a read of the log after the rollback finds a tick
        # row and says "lost race" about a failure that was this worker's.
        import logging
        import threading
        import time

        from django.db import IntegrityError, connection
        from django.utils import timezone as tz

        row = a_minutely()
        key = f"db:{row.pk}"
        tick = tz.now().replace(second=0, microsecond=0)
        winner_done = threading.Event()

        def another_worker_claims_the_tick():
            try:
                OxScheduleTick.objects.create(
                    schedule_name=key, scheduled_for=tick, created_at=tz.now()
                )
            finally:
                connection.close()
                winner_done.set()

        winner = threading.Thread(target=another_worker_claims_the_tick)

        def boom(*args, **kwargs):
            # The other worker's INSERT waits on this pass's uncommitted row
            # and goes through the moment the failure below rolls it back.
            winner.start()
            time.sleep(0.2)
            raise IntegrityError("the task write failed")

        rollback = connection.rollback

        def rollback_then_let_the_winner_land():
            # The instant the log would be asked about: after this pass's
            # rollback, once the waiting INSERT has gone through.
            rollback()
            winner_done.wait(timeout=10)

        monkeypatch.setattr("django_ox.backend.OxBackend.enqueue", boom, raising=True)
        monkeypatch.setattr(connection, "rollback", rollback_then_let_the_winner_land)
        with caplog.at_level(logging.ERROR, logger="django_ox"):
            assert worker.dispatch_schedules() == 0
        winner.join(timeout=30)
        assert not winner.is_alive()
        assert OxScheduleTick.objects.filter(
            schedule_name=key, scheduled_for=tick
        ).exists(), "the other worker's claim should stand"
        assert [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_dispatch_error"
        ], "this worker's failing enqueue was read as the other worker winning"

    def test_a_genuine_lost_race_stays_silent(self, worker, caplog, monkeypatch):
        import logging

        from django.utils import timezone as tz

        row = a_minutely()
        # The tick another worker already committed.
        tick = OxScheduleTick.objects.create(
            schedule_name=f"db:{row.pk}",
            scheduled_for=tz.now().replace(second=0, microsecond=0),
            created_at=tz.now(),
        )
        assert tick.pk
        # With a current view the pass would skip the tick before the
        # INSERT and never race at all. The race is a stale view: the log
        # read before the loop says nothing is recorded, and the INSERT is
        # what finds out otherwise.
        monkeypatch.setattr(worker, "_latest_ticks", lambda schedules, since: {})
        with caplog.at_level(logging.ERROR, logger="django_ox"):
            assert worker.dispatch_schedules() == 0
        assert OxTask.objects.count() == 0, "the loser enqueued"
        assert not [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_dispatch_error"
        ], "a lost race was reported as a failure"


class TestSettingsSchedulesKeepWorkingBesideTheRows:
    """
    docs/stored-schedules.md: "`SCHEDULES` entries keep working if you use
    both." Naming the database source must add the rows to the settings
    schedules, not replace them, or the switch silently stops every
    schedule a project already had.
    """

    def _both(self, settings):
        config = tasks_setting()
        config["default"]["OPTIONS"]["SCHEDULES"] = {
            "from-settings": {
                "task": "tests.tasks.add",
                "cron": "* * * * *",
                "args": [1, 2],
            }
        }
        settings.TASKS = config
        a_minutely()
        return Worker(backoff_initial=0)

    def test_the_worker_sees_the_settings_schedule_and_the_row(self, settings):
        worker = self._both(settings)
        keys = sorted(s.key for s in worker._schedule_source.schedules())
        assert len(keys) == 2, keys
        assert "from-settings" in keys, "the settings schedule was dropped"
        assert keys[0].startswith("db:"), keys

    def test_one_pass_serves_both(self, settings):
        worker = self._both(settings)
        # The row fires on its first due tick; the settings schedule is
        # anchored on first sight and fires on the next. Both leave a row.
        assert worker.dispatch_schedules() == 1
        names = set(OxScheduleTick.objects.values_list("schedule_name", flat=True))
        assert "from-settings" in names, "the settings schedule was dropped"
        assert any(n.startswith("db:") for n in names)
        assert OxScheduleTick.objects.get(schedule_name="from-settings").task is None
