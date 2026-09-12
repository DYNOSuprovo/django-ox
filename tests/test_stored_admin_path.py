"""
The admin path end to end, with the allow-list read from settings.

tests/test_schedule_admin.py patches a registry into place. Here the two
keys come from OPTIONS["SCHEDULABLE_TASKS"], discovered the way a deployment
discovers them, and the row a person creates is then dispatched by a real
pass and executed by a real worker, so what the admin wrote is what runs.
"""

import json

import pytest
from django import forms
from django.contrib.auth.models import Permission, User
from django.urls import reverse
from django.utils import timezone

from django_ox.models import OxSchedule, OxScheduleTick, OxTask
from django_ox.registry import ArgsForm
from django_ox.worker import Worker

pytestmark = pytest.mark.django_db


class ReportArgs(ArgsForm):
    a = forms.IntegerField()
    b = forms.IntegerField()


REPORT = "reports.daily"
PURGE = "ops.purge"
PURGE_PERMISSION = "auth.view_user"


def tasks_setting(*, database_source=True):
    options = {
        "SCHEDULABLE_TASKS": {
            REPORT: {
                "task": "tests.tasks.add",
                "form": "tests.test_stored_admin_path.ReportArgs",
            },
            PURGE: {"task": "tests.tasks.echo", "permission": PURGE_PERMISSION},
        },
    }
    if database_source:
        options["SCHEDULE_SOURCE"] = "django_ox.stored.DatabaseScheduleSource"
    return {
        "default": {
            "BACKEND": "django_ox.backend.OxBackend",
            "QUEUES": ["default", "emails"],
            "OPTIONS": options,
        }
    }


@pytest.fixture(autouse=True)
def _settings_registry(settings, monkeypatch):
    # An empty registry that has not been discovered yet, so the keys reach
    # it the way they reach a deployment: through settings, on first use.
    settings.TASKS = tasks_setting()
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", False)


@pytest.fixture
def staff(client):
    user = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
    for codename in (
        "add_oxschedule",
        "change_oxschedule",
        "delete_oxschedule",
        "view_oxschedule",
    ):
        user.user_permissions.add(Permission.objects.get(codename=codename))
    client.force_login(user)
    return user


ADD = "admin:django_ox_oxschedule_add"
CHANGE = "admin:django_ox_oxschedule_change"
CHANGELIST = "admin:django_ox_oxschedule_changelist"


def form_data(**over):
    data = {
        "name": "nightly-report",
        "task_key": REPORT,
        "trigger": "cron",
        "cron": "0 2 * * *",
        "arguments": json.dumps({"a": 1, "b": 2}),
        "phase_seconds": 0,
        "enabled": "on",
    }
    data.update(over)
    return data


def create_through_the_admin(client, **over):
    response = client.post(reverse(ADD), form_data(**over))
    assert response.status_code == 302, response.context["adminform"].form.errors
    return OxSchedule.objects.get(name=over.get("name", "nightly-report"))


class TestTheAllowListComesFromSettings:
    def test_the_add_form_offers_exactly_the_exposed_keys(self, client, staff):
        response = client.get(reverse(ADD))
        assert response.status_code == 200
        field = response.context["adminform"].form.fields["task_key"]
        assert [key for key, _ in field.choices] == [PURGE, REPORT]
        assert "Nothing else can be scheduled" in response.content.decode()

    def test_creating_a_schedule_lands_on_the_changelist(self, client, staff):
        row = create_through_the_admin(client)
        assert row.task_key == REPORT
        assert row.arguments == {"a": 1, "b": 2}
        assert row.enabled
        body = client.get(reverse(CHANGELIST)).content.decode()
        assert "nightly-report" in body
        assert "0 2 * * *" in body
        assert "never" in body, "no tick has fired yet"

    @pytest.mark.parametrize("dotted", ["os.system", "tests.tasks.add"])
    def test_a_hand_made_post_naming_a_dotted_path_is_refused(
        self, client, staff, dotted
    ):
        # Neither an arbitrary callable nor the real import path of an
        # exposed task: the key is the identity, and the select's choices
        # are validated on the server, not only rendered in the widget.
        response = client.post(reverse(ADD), form_data(name="evil", task_key=dotted))
        assert response.status_code == 200
        errors = response.context["adminform"].form.errors
        # Two messages: the ChoiceField's, then the model's, which sees the
        # empty value the failed field left behind. Both are refusals.
        assert errors["task_key"] == [
            f"Select a valid choice. {dotted} is not one of the available choices.",
            "'' is not a schedulable task. Registered keys: ops.purge, reports.daily.",
        ]
        assert "is not one of the available choices" in response.content.decode()
        assert not OxSchedule.objects.filter(name="evil").exists()

    def test_arguments_the_form_rejects_are_a_field_error(self, client, staff):
        response = client.post(
            reverse(ADD), form_data(arguments=json.dumps({"a": "x", "b": 2}))
        )
        assert response.status_code == 200
        errors = response.context["adminform"].form.errors
        assert errors["arguments"] == ["a: Enter a whole number."]
        assert not OxSchedule.objects.exists()


class TestEditingThroughTheChangeForm:
    def test_cron_and_arguments_change_and_the_boundary_moves(self, client, staff):
        row = create_through_the_admin(client)
        original_boundary = row.start_time
        response = client.post(
            reverse(CHANGE, args=[row.pk]),
            form_data(cron="0 3 * * *", arguments=json.dumps({"a": 10, "b": 20})),
        )
        assert response.status_code == 302
        row.refresh_from_db()
        assert row.cron == "0 3 * * *"
        assert row.arguments == {"a": 10, "b": 20}
        assert row.start_time > original_boundary

    def test_disable_then_re_enable(self, client, staff):
        row = create_through_the_admin(client)

        def act(action):
            return client.post(
                reverse(CHANGELIST),
                {"action": action, "_selected_action": [str(row.pk)]},
                follow=True,
            )

        response = act("disable_selected")
        assert "Disabled 1 schedule(s)." in response.content.decode()
        row.refresh_from_db()
        assert not row.enabled
        paused_at = row.start_time
        response = act("enable_selected")
        assert "Enabled 1 schedule(s)." in response.content.decode()
        row.refresh_from_db()
        assert row.enabled
        assert row.start_time > paused_at


class TestPerKeyPermissionFromSettings:
    def test_refused_without_it_and_nothing_is_written(self, client, staff):
        response = client.post(
            reverse(ADD),
            form_data(
                name="purge", task_key=PURGE, arguments=json.dumps({"value": "x"})
            ),
        )
        # The form asks the same question the service layer answers, so the
        # refusal comes back on the field with the submission intact rather
        # than as a bare 403 out of save_model.
        assert response.status_code == 200
        errors = response.context["adminform"].form.errors
        assert PURGE_PERMISSION in str(errors["task_key"])
        assert not OxSchedule.objects.filter(name="purge").exists()

    def test_accepted_with_it(self, client, staff):
        staff.user_permissions.add(Permission.objects.get(codename="view_user"))
        client.force_login(User.objects.get(pk=staff.pk))  # drop the perm cache
        row = create_through_the_admin(
            client, name="purge", task_key=PURGE, arguments=json.dumps({"value": "x"})
        )
        assert row.task_key == PURGE

    def test_the_change_form_is_read_only_without_it(self, client, staff):
        staff.user_permissions.add(Permission.objects.get(codename="view_user"))
        client.force_login(User.objects.get(pk=staff.pk))
        row = create_through_the_admin(
            client, name="purge", task_key=PURGE, arguments=json.dumps({"value": "x"})
        )
        staff.user_permissions.remove(Permission.objects.get(codename="view_user"))
        client.force_login(User.objects.get(pk=staff.pk))
        response = client.get(reverse(CHANGE, args=[row.pk]))
        assert response.status_code == 200
        assert not response.context["has_change_permission"]
        response = client.post(
            reverse(CHANGE, args=[row.pk]),
            form_data(name="purge", task_key=PURGE, cron="0 4 * * *"),
        )
        assert response.status_code == 403
        row.refresh_from_db()
        assert row.cron == "0 2 * * *"


class TestWhatTheAdminWroteIsWhatRuns:
    def _due_row(self, client):
        row = create_through_the_admin(client, cron="* * * * *")
        # Created this minute, so the current tick is before the boundary.
        # Backdate it as a schedule that has existed for a while would be.
        OxSchedule.objects.filter(pk=row.pk).update(
            start_time=timezone.now() - timezone.timedelta(minutes=5)
        )
        return row

    def test_run_now_enqueues_the_cleaned_arguments_and_no_tick(self, client, staff):
        row = self._due_row(client)
        client.post(
            reverse(CHANGE, args=[row.pk]),
            form_data(cron="* * * * *", arguments=json.dumps({"a": "10", "b": "20"})),
        )
        response = client.post(
            reverse(CHANGELIST),
            {"action": "run_once_now", "_selected_action": [str(row.pk)]},
            follow=True,
        )
        assert "Enqueued 1 task(s)." in response.content.decode()
        assert OxTask.objects.count() == 1
        assert OxTask.objects.get().kwargs == {"a": 10, "b": 20}
        assert not OxScheduleTick.objects.exists()

    def test_the_next_tick_still_fires_after_a_manual_run(self, client, staff):
        row = self._due_row(client)
        client.post(
            reverse(CHANGELIST),
            {"action": "run_once_now", "_selected_action": [str(row.pk)]},
            follow=True,
        )
        worker = Worker(backoff_initial=0, poll_interval=0.05)
        assert worker.dispatch_schedules() == 1
        assert OxTask.objects.count() == 2
        assert OxScheduleTick.objects.count() == 1

    def test_one_pass_fires_once_with_the_edited_arguments(self, client, staff):
        row = self._due_row(client)
        client.post(
            reverse(CHANGE, args=[row.pk]),
            form_data(cron="* * * * *", arguments=json.dumps({"a": 10, "b": 20})),
        )
        # Retimed? No: the cron is unchanged, so the boundary stayed put.
        row.refresh_from_db()
        worker = Worker(backoff_initial=0, poll_interval=0.05)
        assert worker.dispatch_schedules() == 1
        assert worker.dispatch_schedules() == 0, "the same tick must not fire twice"
        task = OxTask.objects.get()
        assert task.kwargs == {"a": 10, "b": 20}
        tick = OxScheduleTick.objects.get()
        assert tick.schedule_name == f"db:{row.pk}"
        assert tick.task_id == task.pk
        worker.run_once()
        task.refresh_from_db()
        assert task.status == OxTask.Status.SUCCESSFUL
        assert task.return_value == 30
        body = client.get(reverse(CHANGELIST)).content.decode()
        assert f"{tick.scheduled_for:%Y-%m-%d %H:%M}" in body


class TestTheAdminWithoutADatabaseSource:
    """
    The admin is registered whenever django.contrib.admin is installed. With
    SCHEDULE_SOURCE unset the rows it writes are never dispatched, while
    the run-now action still works. Recorded as it stands; whether to hide
    the section, warn on it or document it is an open decision.
    """

    def test_rows_exist_run_now_works_and_nothing_dispatches(
        self, client, staff, settings
    ):
        settings.TASKS = tasks_setting(database_source=False)
        assert "Ox schedules" in client.get(reverse("admin:index")).content.decode()
        row = create_through_the_admin(client, cron="* * * * *")
        OxSchedule.objects.filter(pk=row.pk).update(
            start_time=timezone.now() - timezone.timedelta(minutes=5)
        )
        assert Worker(backoff_initial=0).dispatch_schedules() == 0
        assert not OxScheduleTick.objects.exists()
        client.post(
            reverse(CHANGELIST),
            {"action": "run_once_now", "_selected_action": [str(row.pk)]},
            follow=True,
        )
        assert OxTask.objects.count() == 1
