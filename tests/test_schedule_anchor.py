"""
A schedule anchors once, however many workers first see it at once.

The first worker to see a schedule with no history records the current tick
as an anchor and enqueues nothing, so the schedule does not fire for every
tick since the cron epoch. It fires at its next tick.

The decision has to come from the log inside the transaction. Taken from a
snapshot read before the loop, a second worker whose pass began a tick later
still believes it is the first sighting: it writes a second anchor, at a tick
whose boundary already existed and which should have fired, and the unique
constraint then suppresses that instant for good.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

import pytest
from django.db import connection
from django.utils import timezone

from django_ox.models import OxScheduleTick, OxTask
from django_ox.worker import Worker

pytestmark = pytest.mark.django_db

MINUTELY = {
    "minutely-add": {"task": "tests.tasks.add", "cron": "* * * * *", "args": [1, 2]}
}


def tasks_setting(schedules):
    return {
        "default": {
            "BACKEND": "django_ox.backend.OxBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"SCHEDULES": schedules},
        }
    }


@pytest.fixture
def two_workers(settings):
    settings.TASKS = tasks_setting(MINUTELY)
    return Worker(backoff_initial=0), Worker(backoff_initial=0)


def _at(worker, monkeypatch, moment):
    from django_ox import worker as worker_module

    monkeypatch.setattr(worker_module.timezone, "now", lambda: moment)
    return worker.dispatch_schedules()


class TestASecondAnchorDoesNotSwallowATick:
    def test_a_worker_arriving_a_tick_later_fires_rather_than_anchors(
        self, two_workers, monkeypatch
    ):
        # Worker B takes its snapshot while the schedule has no history, then
        # A completes a whole pass and commits the anchor at T0, and only then
        # does B reach its own transaction at T1. T1 must fire.
        a, b = two_workers
        t0 = timezone.now().replace(second=0, microsecond=0)
        t1 = t0 + timedelta(minutes=1)

        snapshot = b._latest_ticks(b.schedules, t0 - timedelta(days=1))
        assert snapshot == {}, "the snapshot must predate the anchor"

        _at(a, monkeypatch, t0)
        monkeypatch.undo()
        assert OxScheduleTick.objects.count() == 1, "A did not anchor"
        assert OxTask.objects.count() == 0, "an anchor must enqueue nothing"

        monkeypatch.setattr(b, "_latest_ticks", lambda schedules, since: snapshot)
        _at(b, monkeypatch, t1)
        monkeypatch.undo()

        rows = list(
            OxScheduleTick.objects.order_by("scheduled_for").values_list(
                "scheduled_for", "task_id"
            )
        )
        assert OxTask.objects.count() == 1, (
            f"the tick after the anchor was swallowed as a second anchor: {rows}"
        )

    def test_the_uncontended_sequence_is_unchanged(self, two_workers, monkeypatch):
        # The control: one worker, two ticks. Anchor then fire, as documented.
        a, _ = two_workers
        t0 = timezone.now().replace(second=0, microsecond=0)
        assert _at(a, monkeypatch, t0) == 0, "the first sighting must not fire"
        monkeypatch.undo()
        assert _at(a, monkeypatch, t0 + timedelta(minutes=1)) == 1
        monkeypatch.undo()
        assert OxTask.objects.count() == 1
        assert OxScheduleTick.objects.count() == 2

    def test_the_first_sighting_read_is_skipped_when_the_bounded_read_shows_history(
        self, settings, monkeypatch
    ):
        # Two schedules in one pass: a minutely one with a tick inside the
        # pass's bound, and an hourly one whose anchor sets the bound at the
        # top of the hour. The minutely schedule's newest tick is at or after
        # the bound, so the pre-loop read answers for it and the in-transaction
        # first-sighting read is not asked. Counted by its real shape, the
        # only SELECT on the tick table that excludes a row by id.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        settings.TASKS = tasks_setting(
            {
                "minutely": {"task": "tests.tasks.add", "cron": "* * * * *"},
                "hourly": {"task": "tests.tasks.add", "cron": "0 * * * *"},
            }
        )
        worker = Worker(backoff_initial=0)
        # Mid-hour, so the next minute is inside the same hour.
        t0 = timezone.now().replace(minute=29, second=30, microsecond=0)
        _at(worker, monkeypatch, t0)  # both anchor: minutely at :29, hourly at :00
        monkeypatch.undo()
        assert OxScheduleTick.objects.count() == 2

        from django_ox import worker as worker_module

        monkeypatch.setattr(
            worker_module.timezone, "now", lambda: t0 + timedelta(minutes=1)
        )
        with CaptureQueriesContext(connection) as captured:
            assert worker.dispatch_schedules() == 1, "the minutely tick fires"
        monkeypatch.undo()
        reads = [
            q["sql"]
            for q in captured.captured_queries
            if _is_first_sighting_read(q["sql"])
        ]
        assert reads == [], (
            f"the first-sighting read ran with history in the bound: {reads}"
        )

    def test_a_schedule_alone_in_its_pass_pays_one_read_per_dispatch(
        self, two_workers, monkeypatch
    ):
        # The other case, and the one a lone settings schedule is always in.
        # Its bound is its own due tick, so its newest tick, one period back,
        # predates the bound and the pre-loop read says nothing about it.
        # The first-sighting read is then asked, once, on every dispatch,
        # and answers from the log. Not "only until it has a tick".
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        a, _ = two_workers
        t0 = timezone.now().replace(second=0, microsecond=0)
        _at(a, monkeypatch, t0)
        monkeypatch.undo()

        from django_ox import worker as worker_module

        for minutes in (1, 2):
            moment = t0 + timedelta(minutes=minutes)
            monkeypatch.setattr(worker_module.timezone, "now", lambda m=moment: m)
            with CaptureQueriesContext(connection) as captured:
                assert a.dispatch_schedules() == 1
            monkeypatch.undo()
            reads = [
                q["sql"]
                for q in captured.captured_queries
                if _is_first_sighting_read(q["sql"])
            ]
            assert len(reads) == 1, f"pass {minutes}: {len(reads)} first-sighting reads"


class TestAScheduleWhoseFirstTickIsTheEpoch:
    """
    An interval trigger counts from the epoch, so an interval longer than
    the time since it puts the schedule's first and only tick exactly
    there. The latch used to be written at the epoch too, and this pass's
    own tick row then held the unique index against it: an IntegrityError
    on every pass, logged as a dispatch error, and a schedule that never
    anchored. In UTC, because the tick is derived in the project's zone and
    the collision needs the two instants to coincide.
    """

    def test_it_anchors_cleanly(self, settings, caplog):
        from django.utils import timezone as tz

        settings.TASKS = tasks_setting(
            {"century": {"task": "tests.tasks.add", "every": 3600 * 24 * 365 * 100}}
        )
        with tz.override("UTC"), caplog.at_level(logging.WARNING, logger="django_ox"):
            assert Worker(backoff_initial=0).dispatch_schedules() == 0
            assert Worker(backoff_initial=0).dispatch_schedules() == 0
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "the latch collided with the schedule's own tick row"
        )
        (row,) = OxScheduleTick.objects.all()
        assert row.task_id is None, "anchored, not fired"
        assert row.scheduled_for.replace(tzinfo=None) == datetime(1970, 1, 1)


class _ThreadClock:
    """timezone.now() answered per thread, so two workers run at once at
    different instants without a global patch that each would overwrite."""

    def __init__(self, real):
        self._local = threading.local()
        self._real = real

    def set(self, moment):
        self._local.now = moment

    def __call__(self):
        return getattr(self._local, "now", None) or self._real()


def _is_first_sighting_read(sql):
    # The read that decides first sighting: this schedule's log, this pass's
    # own row excluded. The only SELECT on the tick table with a NOT.
    return sql.lstrip().upper().startswith("SELECT") and (
        "oxscheduletick" in sql and "NOT (" in sql
    )


def _is_latch_insert(sql, params):
    # The latch row is the only tick ever written at the latch instant.
    return (
        sql.lstrip().upper().startswith("INSERT")
        and "oxscheduletick" in sql
        and any("1900-01-01" in str(param) for param in params or ())
    )


def _worker_thread(clock, moment, matches, before, after, out, key):
    def hook(execute, sql, params, many, context):
        hit = matches(sql, params)
        if hit:
            before()
        result = execute(sql, params, many, context)
        if hit:
            after()
        return result

    def body():
        clock.set(moment)
        try:
            with connection.execute_wrapper(hook):
                out[key] = Worker(backoff_initial=0).dispatch_schedules()
        except BaseException as exc:
            out[key] = exc
        finally:
            connection.close()

    return threading.Thread(target=body, name=f"anchor-{key}")


def _rows():
    return list(
        OxScheduleTick.objects.order_by("scheduled_for").values_list(
            "scheduled_for", "task_id"
        )
    )


@pytest.mark.django_db(transaction=True)
class TestFirstSightingsOnDifferentTicks:
    """
    Two workers first see a settings schedule either side of a minute
    boundary: A holds T0 as its tick, B holds T1.

    Their tick rows take different keys, so the unique constraint serialises
    neither, and neither's read sees the other's uncommitted row. Left to
    plain reads both anchor, and T1 is claimed with no task: a tick that had
    a boundary and never runs. The invariant is one anchor per schedule, and
    it is the earliest row; every row after it carries a task.

    Threads with their own connections, paused from inside their own
    statements, which is the interleaving two processes produce. On SQLite
    the single writer serialises the two, so the pause holds B at its
    INSERT and the scenario degrades to a sequence; the tests still hold
    there, and PostgreSQL and MySQL are where they can fail.
    """

    @pytest.fixture(autouse=True)
    def _schedule(self, settings, monkeypatch):
        settings.TASKS = tasks_setting(MINUTELY)
        from django_ox import worker as worker_module

        # worker_module.timezone is django.utils.timezone itself, so this
        # patches every caller; the clock answers the real time on a thread
        # that has not set one.
        self.clock = _ThreadClock(timezone.now)
        monkeypatch.setattr(worker_module.timezone, "now", self.clock)
        self.t0 = timezone.now().replace(second=0, microsecond=0)
        self.t1 = self.t0 + timedelta(minutes=1)

    def _overlap(self, a_matches, b_matches, b_signals_before):
        """Run A until its matched statement, then B alongside it."""
        a_paused, release_a, b_reached = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        out = {}
        a = _worker_thread(
            self.clock,
            self.t0 + timedelta(seconds=30),
            a_matches,
            before=lambda: None,
            after=lambda: (a_paused.set(), release_a.wait(timeout=30)),
            out=out,
            key="A",
        )
        b = _worker_thread(
            self.clock,
            self.t1 + timedelta(seconds=30),
            b_matches,
            before=b_reached.set if b_signals_before else (lambda: None),
            after=(lambda: None) if b_signals_before else b_reached.set,
            out=out,
            key="B",
        )
        a.start()
        assert a_paused.wait(timeout=10), "A never reached the statement"
        b.start()
        # B either reaches its statement or, on SQLite, blocks on the file
        # lock A holds; both are the concurrent state the tests are about.
        b_reached.wait(timeout=2)
        time.sleep(0.3)
        release_a.set()
        a.join(timeout=30)
        b.join(timeout=30)
        assert not a.is_alive() and not b.is_alive()
        raised = [v for v in out.values() if isinstance(v, BaseException)]
        assert not raised, raised
        return out

    def _assert_one_anchor_and_it_is_the_earliest(self):
        rows = _rows()
        anchors = [at for at, task_id in rows if task_id is None]
        assert len(anchors) == 1, f"the schedule anchored more than once: {rows}"
        assert anchors[0] == rows[0][0], f"the anchor is not the earliest: {rows}"
        assert OxTask.objects.count() == len(rows) - 1

    def test_a_first_sighting_on_the_next_tick_does_not_anchor_a_second_time(
        self,
    ):
        # A pauses after the read that told it there is no history, before it
        # has written anything else, while B runs its whole pass. Without a
        # latch B's read finds nothing either, and both commit anchors.
        self._overlap(
            a_matches=lambda sql, params: _is_first_sighting_read(sql),
            b_matches=lambda sql, params: _is_first_sighting_read(sql),
            b_signals_before=False,
        )
        self._assert_one_anchor_and_it_is_the_earliest()
        # The schedule keeps running: the tick after the anchor fires.
        self.clock.set(self.t1 + timedelta(minutes=1, seconds=30))
        assert Worker(backoff_initial=0).dispatch_schedules() == 1
        self._assert_one_anchor_and_it_is_the_earliest()

    def test_the_later_first_sighting_waits_on_the_latch_and_then_fires(self):
        # A pauses with its latch row written and uncommitted. B's own latch
        # INSERT has to wait for A's transaction to end, and the read B
        # repeats after it finds A's anchor, so B fires T1 rather than
        # anchoring it.
        out = self._overlap(
            a_matches=_is_latch_insert,
            b_matches=_is_latch_insert,
            b_signals_before=True,
        )
        assert out["A"] == 0, "the first sighting must not fire"
        assert out["B"] == 1, "the tick after the anchor must fire"
        rows = _rows()
        assert [at for at, _ in rows] == [self.t0, self.t1]
        assert rows[0][1] is None and rows[1][1] is not None, rows
        assert OxTask.objects.count() == 1
