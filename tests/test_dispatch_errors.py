"""
What the dispatch pass does with an exception depends on where it came from.

One schedule's own failure, a task that will not enqueue or a row that
raises, is logged against that schedule and the rest of the pass continues.
A database error is not one schedule's: it ends the pass and reaches run(),
which logs `schedule_dispatch_failed`, drops a connection that is no longer
usable, and goes on to the claim.
"""

import logging
import threading
import time
from datetime import timedelta

import pytest
from django.db import DatabaseError, OperationalError, connection, transaction
from django.utils import timezone

from django_ox.compat import task_enqueued
from django_ox.models import OxScheduleTick, OxTask
from django_ox.schedules import lock_contention
from django_ox.worker import Worker

pytestmark = pytest.mark.django_db

TWO = {
    name: {"task": "tests.tasks.add", "cron": "* * * * *", "args": [1, 2]}
    for name in ("one", "two")
}


@pytest.fixture
def worker(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_ox.backend.OxBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"SCHEDULES": TWO},
        }
    }
    return Worker(
        backoff_initial=0, poll_interval=0.02, reap_interval=0.0, schedule_interval=0.0
    )


def with_history():
    # A tick of history each, so the pass fires rather than anchors.
    now = timezone.now()
    for name in TWO:
        OxScheduleTick.objects.create(
            schedule_name=name,
            scheduled_for=now.replace(second=0, microsecond=0) - timedelta(minutes=1),
            task_id=None,
            created_at=now,
        )


def raising_on_tick_insert(exc, times=None):
    """An execute wrapper that raises `exc` from the tick INSERT, `times` times."""
    raised = []

    def wrapper(execute, sql, params, many, context):
        is_tick_insert = sql.lstrip().upper().startswith("INSERT") and (
            "oxscheduletick" in sql
        )
        if is_tick_insert and (times is None or len(raised) < times):
            raised.append(exc)
            raise exc
        return execute(sql, params, many, context)

    return wrapper


def events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


class TestADatabaseErrorIsNotOneSchedules:
    def test_it_leaves_the_pass_rather_than_being_logged_against_a_schedule(
        self, worker, caplog
    ):
        with_history()
        gone = OperationalError("server closed the connection unexpectedly")
        with (
            caplog.at_level(logging.ERROR, logger="django_ox"),
            connection.execute_wrapper(raising_on_tick_insert(gone)),
            pytest.raises(DatabaseError),
        ):
            worker.dispatch_schedules()
        assert not events(caplog, "schedule_dispatch_error"), (
            "a database error was reported as one schedule's failure"
        )

    def test_one_schedules_own_failure_is_still_isolated(self, worker, caplog):
        with_history()
        own = RuntimeError("this schedule's task will not enqueue")
        with (
            caplog.at_level(logging.ERROR, logger="django_ox"),
            connection.execute_wrapper(raising_on_tick_insert(own, times=1)),
        ):
            assert worker.dispatch_schedules() == 1, "the other schedule must fire"
        assert len(events(caplog, "schedule_dispatch_error")) == 1
        assert OxTask.objects.count() == 1


@pytest.mark.django_db(transaction=True)
class TestRunReportsAFailedPassAndCarriesOn:
    def test_the_next_pass_dispatches(self, worker, caplog):
        # Transactional: run() polls on its own thread with its own
        # connection, and the wrapper has to be installed on that one.
        with_history()
        gone = OperationalError("server closed the connection unexpectedly")

        def run_with_one_broken_statement():
            with connection.execute_wrapper(raising_on_tick_insert(gone, times=1)):
                worker.run()

        thread = threading.Thread(target=run_with_one_broken_statement, daemon=True)
        with caplog.at_level(logging.INFO, logger="django_ox"):
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if events(caplog, "schedule_dispatch_failed") and (
                    len(events(caplog, "schedule_dispatched")) == len(TWO)
                ):
                    break
                time.sleep(0.02)
            worker.request_stop()
            thread.join(timeout=5)

        assert not thread.is_alive(), "run() did not return after the stop"
        assert events(caplog, "schedule_dispatch_failed"), "the pass was not reported"
        assert len(events(caplog, "schedule_dispatched")) == len(TWO), (
            "the pass after the failure did not dispatch"
        )
        assert not events(caplog, "schedule_dispatch_error")


class _PgLockNotAvailable(Exception):
    """The shape psycopg gives a lock_timeout: an SQLSTATE on the cause."""

    sqlstate = "55P03"


def _with_cause(exc, cause):
    exc.__cause__ = cause
    return exc


LOCK_CONTENTION = {
    "mysql-lock-wait": OperationalError(
        1205, "Lock wait timeout exceeded; try restarting transaction"
    ),
    "mysql-deadlock": OperationalError(
        1213, "Deadlock found when trying to get lock; try restarting transaction"
    ),
    "sqlite-busy": OperationalError("database is locked"),
    "postgres-lock-timeout": _with_cause(
        OperationalError("canceling statement due to lock timeout"),
        _PgLockNotAvailable(),
    ),
}


class TestALockTheDatabaseGaveUpOnIsContentionNotAFault:
    @pytest.mark.parametrize("shape", list(LOCK_CONTENTION), ids=list(LOCK_CONTENTION))
    def test_the_classifier_reads_each_databases_shape(self, shape):
        assert lock_contention(LOCK_CONTENTION[shape])

    @pytest.mark.parametrize(
        "other",
        [
            OperationalError("server closed the connection unexpectedly"),
            OperationalError(2006, "MySQL server has gone away"),
            OperationalError("no such table: django_ox_oxscheduletick"),
        ],
        ids=["postgres-gone", "mysql-gone", "sqlite-missing-table"],
    )
    def test_anything_else_is_not(self, other):
        assert not lock_contention(other)

    @pytest.mark.parametrize("shape", list(LOCK_CONTENTION), ids=list(LOCK_CONTENTION))
    def test_it_is_one_warning_without_a_traceback_and_the_pass_goes_on(
        self, worker, caplog, shape
    ):
        with_history()
        with (
            caplog.at_level(logging.WARNING, logger="django_ox"),
            connection.execute_wrapper(
                raising_on_tick_insert(LOCK_CONTENTION[shape], times=1)
            ),
        ):
            assert worker.dispatch_schedules() == 1, "the other schedule must fire"
        warned = events(caplog, "schedule_lock_unavailable")
        assert len(warned) == 1
        assert warned[0].exc_info is None, "contention is not a traceback"
        assert not events(caplog, "schedule_dispatch_error")
        assert not events(caplog, "schedule_dispatch_failed")


@pytest.mark.django_db(transaction=True)
class TestAMySQLLockWaitTimeoutOnTheTickRow:
    """
    The real thing on MySQL: the winner holds its transaction past the
    loser's lock-wait timeout, so the loser's unique INSERT ends in 1205
    rather than in the IntegrityError the loop reads as a lost race.
    """

    def test_the_loser_warns_once_and_the_winners_task_stands(self, worker, caplog):
        if connection.vendor != "mysql":
            pytest.skip("innodb_lock_wait_timeout is MySQL's")
        with_history()
        winner_inserted, release = threading.Event(), threading.Event()
        out = {}

        def hold_after_the_tick_insert(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.lstrip().upper().startswith("INSERT") and "oxscheduletick" in sql:
                winner_inserted.set()
                release.wait(timeout=10)
            return result

        def winner():
            try:
                with connection.execute_wrapper(hold_after_the_tick_insert):
                    out["winner"] = Worker(backoff_initial=0).dispatch_schedules()
            except BaseException as exc:
                out["winner"] = exc
            finally:
                connection.close()

        def loser():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
                out["loser"] = Worker(backoff_initial=0).dispatch_schedules()
            except BaseException as exc:
                out["loser"] = exc
            finally:
                connection.close()

        first = threading.Thread(target=winner)
        second = threading.Thread(target=loser)
        with caplog.at_level(logging.WARNING, logger="django_ox"):
            first.start()
            assert winner_inserted.wait(timeout=10)
            second.start()
            second.join(timeout=30)
            release.set()
            first.join(timeout=30)
        assert not first.is_alive() and not second.is_alive()
        raised = [v for v in out.values() if isinstance(v, BaseException)]
        assert not raised, raised
        # The loser timed out on the first schedule's tick and went on to
        # the second, which it won while the winner was still held; each
        # tick fired exactly once between them.
        assert out["winner"] + out["loser"] == len(TWO), out
        assert OxTask.objects.count() == len(TWO)
        warned = events(caplog, "schedule_lock_unavailable")
        assert [r.schedule for r in warned] == ["one"], [r.getMessage() for r in warned]
        assert warned[0].exc_info is None, "contention is not a traceback"
        assert not events(caplog, "schedule_dispatch_error")


@pytest.mark.django_db(transaction=True)
class TestAPostCommitCallbackThatRaises:
    """
    Django runs transaction.on_commit callbacks at the outermost exit, after
    the commit, and does not guard them by default. One registered by a
    task_enqueued receiver that raises therefore leaves the dispatch block
    after the task is committed. That is the callback's failure, not the
    dispatch's: the task exists, the tick is recorded, and both are counted.

    Transactional so the block is the outermost transaction and the callback
    actually runs; inside a test transaction it would be deferred.
    """

    def test_the_dispatch_is_counted_and_the_callback_reported(self, worker, caplog):
        with_history()

        def failing_after_commit():
            raise RuntimeError("the callback failed after the commit")

        def receiver(sender, task_result, **kwargs):
            transaction.on_commit(failing_after_commit)

        task_enqueued.connect(receiver)
        try:
            with caplog.at_level(logging.INFO, logger="django_ox"):
                dispatched = worker.dispatch_schedules()
        finally:
            task_enqueued.disconnect(receiver)

        assert dispatched == len(TWO), "a task that exists went uncounted"
        assert OxTask.objects.count() == len(TWO)
        assert OxScheduleTick.objects.exclude(task_id=None).count() == len(TWO)
        failed = events(caplog, "schedule_dispatch_callback_failed")
        assert len(failed) == len(TWO)
        assert all(r.exc_info is not None for r in failed), "the cause is wanted"
        assert {str(t) for t in OxTask.objects.values_list("id", flat=True)} == {
            r.task_id for r in failed
        }
        assert len(events(caplog, "schedule_dispatched")) == len(TWO)
        assert not events(caplog, "schedule_dispatch_error")
