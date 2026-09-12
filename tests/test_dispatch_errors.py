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
from django.db import DatabaseError, OperationalError, connection
from django.utils import timezone

from django_ox.models import OxScheduleTick, OxTask
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
