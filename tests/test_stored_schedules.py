"""Schedules that live in a database row."""

from datetime import timedelta

import pytest
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from django_ox.models import OxSchedule, OxScheduleChange
from django_ox.registry import ArgsForm, ScheduleKind, register
from django_ox.stored import boundary_digest, create_schedule, update_schedule

from . import tasks

pytestmark = pytest.mark.django_db


class _Args(ArgsForm):
    region = forms.CharField()


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.add))
    register(ScheduleKind(key="checked", task=tasks.add, form=_Args))


def a_cron(**over):
    fields = {
        "name": "nightly",
        "task_key": "report",
        "trigger": "cron",
        "cron": "0 2 * * *",
    }
    fields.update(over)
    return create_schedule(**fields)


class TestTheRegistryIsTheBoundary:
    def test_a_registered_key_is_accepted(self):
        assert a_cron().task_key == "report"

    def test_an_unregistered_key_is_refused_by_the_service_function(self):
        with pytest.raises(ValidationError) as caught:
            a_cron(task_key="os.system")
        assert "task_key" in caught.value.message_dict

    def test_an_unregistered_key_is_refused_through_full_clean(self):
        # The path the admin takes, via ModelForm._post_clean.
        row = OxSchedule(
            name="x",
            task_key="os.system",
            trigger="cron",
            cron="0 2 * * *",
            start_time=timezone.now(),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        with pytest.raises(ValidationError) as caught:
            row.full_clean()
        assert "task_key" in caught.value.message_dict

    def test_the_error_names_the_keys_that_would_work(self):
        with pytest.raises(ValidationError) as caught:
            a_cron(task_key="nope")
        assert "checked, report" in str(caught.value.message_dict["task_key"])

    def test_objects_create_bypasses_validation_and_that_is_expected(self):
        # save() does not call full_clean(); Django documents this. So an
        # unvalidated row can exist, and the dispatch path has to expect
        # one rather than trust the table. Asserted so the expectation is
        # recorded rather than assumed.
        row = OxSchedule.objects.create(
            name="raw",
            task_key="os.system",
            trigger="cron",
            cron="0 2 * * *",
            start_time=timezone.now(),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        assert row.pk is not None


class TestArgumentValidation:
    def test_arguments_are_validated_against_the_registered_form(self):
        with pytest.raises(ValidationError) as caught:
            a_cron(name="a", task_key="checked", arguments={"wrong": 1})
        assert "arguments" in caught.value.message_dict

    def test_valid_arguments_are_accepted(self):
        row = a_cron(name="b", task_key="checked", arguments={"region": "emea"})
        assert row.arguments == {"region": "emea"}

    def test_arguments_must_be_a_mapping(self):
        with pytest.raises(ValidationError) as caught:
            a_cron(arguments=[1, 2])
        assert "arguments" in caught.value.message_dict


class TestTriggerValidation:
    @pytest.mark.parametrize(
        ("over", "field"),
        [
            ({"cron": "banana"}, "cron"),
            ({"cron": ""}, "cron"),
            ({"trigger": "interval", "cron": "0 2 * * *"}, "cron"),
            (
                {"trigger": "interval", "cron": "", "every_seconds": None},
                "every_seconds",
            ),
            ({"trigger": "interval", "cron": "", "every_seconds": 0}, "every_seconds"),
            (
                {
                    "trigger": "interval",
                    "cron": "",
                    "every_seconds": 60,
                    "phase_seconds": 60,
                },
                "phase_seconds",
            ),
        ],
    )
    def test_bad_triggers_are_refused(self, over, field):
        with pytest.raises(ValidationError) as caught:
            a_cron(**over)
        assert field in caught.value.message_dict

    def test_end_time_must_follow_start_time(self):
        now = timezone.now()
        with pytest.raises(ValidationError) as caught:
            a_cron(start_time=now, end_time=now - timedelta(hours=1))
        assert "end_time" in caught.value.message_dict


class TestTheDigestSurvivesTheRoundTrip:
    """
    The digest has to name what the column holds, not what the caller passed.

    A boundary digest is written by one process and recomputed by another
    from a row it read. If the two disagree the schedule reads as
    permanently stale: dispatch declines its tick and the boundary is moved
    onto the current timing, so the schedule loses the run it was created
    for.
    """

    def test_the_model_s_own_enum_digests_the_same_as_the_column(self):
        row = a_cron(trigger=OxSchedule.Trigger.CRON)
        assert row.boundary_for == boundary_digest(OxSchedule.objects.get(pk=row.pk)), (
            "a schedule created with the enum reads as stale forever"
        )

    def test_an_interval_given_as_a_string_digests_the_same_as_the_column(self):
        row = a_cron(
            name="every-minute", trigger="interval", cron="", every_seconds="60"
        )
        assert row.boundary_for == boundary_digest(OxSchedule.objects.get(pk=row.pk))

    def test_a_no_op_string_edit_is_not_a_retime(self):
        # The shape a JSON body or an env var produces. Reading "60" as a
        # change from 60 moves the boundary past a tick that was already
        # due, and that tick never fires.
        row = a_cron(name="every-minute", trigger="interval", cron="", every_seconds=60)
        before = row.start_time
        update_schedule(row, every_seconds="60")
        row.refresh_from_db()
        assert row.start_time == before, "a no-op edit moved the activation boundary"


class TestTheBoundary:
    def test_creation_sets_the_boundary_to_now_not_to_first_observation(self):
        before = timezone.now()
        row = a_cron()
        assert before <= row.start_time <= timezone.now()

    def test_retiming_moves_the_boundary(self):
        row = a_cron()
        original = row.start_time
        update_schedule(row, cron="0 3 * * *")
        assert row.start_time > original

    def test_editing_what_it_runs_does_not_move_the_boundary(self):
        # A schedule edited more often than its own period would never fire
        # if any edit re-anchored it. That is a reported defect elsewhere,
        # not a hypothetical.
        row = a_cron(task_key="checked", arguments={"region": "emea"})
        original = row.start_time
        update_schedule(row, arguments={"region": "apac"})
        assert row.start_time == original

    def test_disabling_does_not_move_the_boundary(self):
        row = a_cron()
        original = row.start_time
        update_schedule(row, enabled=False)
        assert row.start_time == original

    def test_re_enabling_moves_the_boundary(self):
        # Otherwise a pause accumulates a backlog that fires at once on
        # resume, which is what an operator pausing something does not want.
        row = a_cron()
        update_schedule(row, enabled=False)
        paused_at = row.start_time
        update_schedule(row, enabled=True)
        assert row.start_time > paused_at


class TestARetimeGoingRoundTheWriteApi:
    def test_a_bulk_update_does_not_fire_retroactively(self):
        # queryset.update() runs no model code, so the boundary stays where
        # it was and the row now describes different ticks. Nothing detects
        # that at write time and nothing needs to: dispatch recomputes the
        # tick from the row inside the transaction that would record it, so
        # the tick a worker planned no longer matches and is not written.
        # Exercised end to end in tests/test_stored_dispatch.py.
        row = a_cron()
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 3 * * *")
        row.refresh_from_db()
        assert row.cron == "0 3 * * *"
        assert row.start_time < timezone.now()


class TestTheChangeRow:
    def test_creating_a_schedule_bumps_it(self):
        a_cron()
        assert OxScheduleChange.objects.get(id=1).changed_at is not None

    def test_updating_a_schedule_bumps_it(self):
        row = a_cron()
        first = OxScheduleChange.objects.get(id=1).changed_at
        update_schedule(row, enabled=False)
        assert OxScheduleChange.objects.get(id=1).changed_at > first

    def test_there_is_only_ever_one_row(self):
        a_cron()
        a_cron(name="another")
        assert OxScheduleChange.objects.count() == 1
