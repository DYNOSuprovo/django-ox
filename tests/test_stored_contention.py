"""
Stored schedules under real contention, on whichever database is configured.

The admission check takes a row lock inside the dispatch transaction, and
until now this package had nothing that ran concurrent writers against a
real connection. Reasoning about lock behaviour is not the same as
observing it, and the mechanism differs by database: PostgreSQL and MySQL
grant a row lock and re-read the committed row, while SQLite has no row
locks at all and relies on the conditional UPDATE being the transaction's
first write to promote it to a writer.

Every test here therefore runs on SQLite, PostgreSQL and MySQL alike, and
the SQLite runs are the ones that matter most: `select_for_update()` is a
silent no-op there, so a guarantee written that way would have passed on
two databases and quietly done nothing on the third.
"""

import threading
from datetime import timedelta

import pytest
from django.db import connections
from django.utils import timezone

from django_ox.models import OxSchedule, OxScheduleTick, OxTask
from django_ox.registry import ScheduleKind, register
from django_ox.stored import create_schedule, update_schedule
from django_ox.worker import Worker

from . import tasks

pytestmark = pytest.mark.django_db(transaction=True)


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


@pytest.fixture(autouse=True)
def _tasks(settings):
    settings.TASKS = tasks_setting()


def a_minutely(name="minutely"):
    return create_schedule(
        name=name,
        task_key="report",
        trigger="cron",
        cron="* * * * *",
        start_time=timezone.now() - timedelta(minutes=5),
    )


def run_concurrently(bodies):
    """Run each callable on its own thread and return what they raised."""
    raised: list[BaseException] = []
    ready = threading.Barrier(len(bodies))

    def wrap(body):
        def inner():
            try:
                ready.wait(timeout=10)
                body()
            except BaseException as exc:
                raised.append(exc)
            finally:
                for conn in connections.all():
                    conn.close()

        return inner

    threads = [threading.Thread(target=wrap(b)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a thread did not finish"
    return raised


def test_many_workers_dispatch_one_tick_exactly_once():
    a_minutely()
    workers = [Worker(backoff_initial=0) for _ in range(6)]
    for w in workers:
        w._schedule_source.schedules()  # every worker reads before any writes

    raised = run_concurrently([w.dispatch_schedules for w in workers])
    assert not raised, raised
    assert OxTask.objects.count() == 1
    assert OxScheduleTick.objects.count() == 1


def test_dispatch_and_a_concurrent_disable_do_not_deadlock():
    # The interlock: dispatch takes the schedule row's lock after enqueueing,
    # and a disable takes the same row's lock. Papering over the ordering
    # shows up here as "database is locked" or a lock-wait timeout rather
    # than as a wait, so the assertion is that nothing raised.
    rows = [a_minutely(f"s{i}") for i in range(4)]
    workers = [Worker(backoff_initial=0) for _ in range(3)]
    for w in workers:
        w._schedule_source.schedules()

    def disable_all():
        for row in rows:
            OxSchedule.objects.filter(pk=row.pk).update(enabled=False)

    raised = run_concurrently(
        [*[w.dispatch_schedules for w in workers], disable_all, disable_all]
    )
    assert not raised, raised


def test_no_tick_is_claimed_without_being_run():
    # The invariant that survives every interleaving: a tick row with no
    # task attached, for a schedule that carries its own boundary, would
    # mean a tick was claimed and never run. Nothing may produce one.
    a_minutely()
    workers = [Worker(backoff_initial=0) for _ in range(4)]
    for w in workers:
        w._schedule_source.schedules()

    def disable_it():
        OxSchedule.objects.filter(name="minutely").update(enabled=False)

    raised = run_concurrently([*[w.dispatch_schedules for w in workers], disable_it])
    assert not raised, raised
    orphans = OxScheduleTick.objects.filter(task__isnull=True)
    assert not orphans.exists(), (
        f"{orphans.count()} tick(s) claimed but never run; a worker with a "
        "current view would then be suppressed by the unique constraint"
    )
    # A concurrent disable makes zero fires legitimate here, so the count is
    # not the invariant. This is: every recorded tick ran, and every run was
    # recorded.
    assert OxTask.objects.count() == OxScheduleTick.objects.count()
    assert OxTask.objects.count() <= 1


def test_a_rename_under_contention_still_fires_one_task():
    # The interleaving that produced two tasks. Workers read before the
    # rename, another reads after, and they all dispatch at once.
    row = a_minutely()
    early = [Worker(backoff_initial=0) for _ in range(3)]
    for w in early:
        w._schedule_source.schedules()

    def rename():
        OxSchedule.objects.filter(pk=row.pk).update(name="renamed")

    late = Worker(backoff_initial=0)
    raised = run_concurrently(
        [*[w.dispatch_schedules for w in early], rename, late.dispatch_schedules]
    )
    assert not raised, raised
    assert OxTask.objects.count() == 1, (
        f"one tick produced {OxTask.objects.count()} tasks under a rename; "
        "zero would mean nothing fired at all, which is also wrong"
    )
    assert OxScheduleTick.objects.values("schedule_name").distinct().count() == 1


def test_editing_rows_while_workers_dispatch_never_double_fires():
    # Workers hold snapshots, an editor changes the rows underneath them, and
    # everything runs at once. The invariant is unchanged: a tick either runs
    # and is recorded, or neither.
    rows = [a_minutely(f"s{i}") for i in range(3)]
    workers = [Worker(backoff_initial=0) for _ in range(4)]
    for w in workers:
        w._schedule_source.schedules()

    def retime():
        for row in rows:
            OxSchedule.objects.filter(pk=row.pk).update(cron="0 3 * * *")

    def pause_and_resume():
        # Through the write API, which moves the boundary. Doing it with
        # queryset.update() leaves the boundary alone and exercises nothing
        # that pause and resume is supposed to do.
        for row in rows:
            update_schedule(row, enabled=False)
            update_schedule(row, enabled=True)

    raised = run_concurrently(
        [*[w.dispatch_schedules for w in workers], retime, pause_and_resume]
    )
    assert not raised, raised

    orphans = OxScheduleTick.objects.filter(task__isnull=True)
    assert not orphans.exists(), f"{orphans.count()} tick(s) claimed but never run"
    for name in OxScheduleTick.objects.values_list("schedule_name", flat=True):
        assert OxScheduleTick.objects.filter(schedule_name=name).count() <= 1, (
            f"{name} recorded more than one tick for one instant"
        )
    assert OxTask.objects.count() == OxScheduleTick.objects.count()
