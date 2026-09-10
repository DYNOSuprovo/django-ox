"""The schedule admin, which is this package's first write surface."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from django_ox.models import OxSchedule, OxTask
from django_ox.registry import ScheduleKind, register
from django_ox.stored import create_schedule, update_schedule

from . import tasks

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.add))
    register(
        ScheduleKind(key="restricted", task=tasks.add, permission="auth.view_user")
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


@pytest.fixture
def staff_user(db):
    user = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
    for codename in (
        "add_oxschedule",
        "change_oxschedule",
        "delete_oxschedule",
        "view_oxschedule",
    ):
        user.user_permissions.add(Permission.objects.get(codename=codename))
    return user


def a_schedule(**over):
    fields = {
        "name": "nightly",
        "task_key": "report",
        "trigger": "cron",
        "cron": "0 2 * * *",
        "start_time": timezone.now() - timedelta(days=1),
    }
    fields.update(over)
    return create_schedule(**fields)


ADD_URL = "admin:django_ox_oxschedule_add"
CHANGE_URL = "admin:django_ox_oxschedule_change"


class TestTheRegistryIsEnforcedOnThePostNotJustTheWidget:
    def test_a_post_naming_an_unregistered_task_is_rejected(self, client, admin_user):
        # A ChoiceField validates membership server-side, so this is refused
        # by the form rather than only missing from the rendered select. The
        # model's own check covers the paths that build no form at all, which
        # tests/test_stored_schedules.py exercises.
        client.force_login(admin_user)
        response = client.post(
            reverse(ADD_URL),
            {
                "name": "evil",
                "task_key": "os.system",
                "trigger": "cron",
                "cron": "0 2 * * *",
                "arguments": "{}",
                "phase_seconds": 0,
            },
        )
        assert response.status_code == 200  # redisplayed with errors
        assert not OxSchedule.objects.filter(name="evil").exists()

    def test_a_valid_post_creates_a_schedule_with_a_boundary(self, client, admin_user):
        client.force_login(admin_user)
        before = timezone.now()
        response = client.post(
            reverse(ADD_URL),
            {
                "name": "nightly",
                "task_key": "report",
                "trigger": "cron",
                "cron": "0 2 * * *",
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
            },
        )
        assert response.status_code == 302, response.context["errors"]
        row = OxSchedule.objects.get(name="nightly")
        # The service layer ran, so the boundary was written by the creator
        # rather than left for a worker to discover.
        assert row.start_time >= before

    def test_the_task_field_offers_only_registered_keys(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(reverse(ADD_URL))
        field = response.context["adminform"].form.fields["task_key"]
        assert [key for key, _ in field.choices] == ["report", "restricted"]


class TestTheAdminGoesThroughTheServiceLayer:
    def test_retiming_through_the_admin_moves_the_boundary(self, client, admin_user):
        row = a_schedule()
        original = row.start_time
        client.force_login(admin_user)
        response = client.post(
            reverse(CHANGE_URL, args=[row.pk]),
            {
                "name": row.name,
                "task_key": row.task_key,
                "trigger": "cron",
                "cron": "0 3 * * *",
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
            },
        )
        assert response.status_code == 302
        row.refresh_from_db()
        assert row.cron == "0 3 * * *"
        assert row.start_time > original, (
            "a retimed schedule must not keep a boundary set for its old timing"
        )


class TestPerEntryPermission:
    def test_the_service_layer_refuses_without_the_permission(self, staff_user):
        with pytest.raises(PermissionDenied):
            create_schedule(
                name="r",
                task_key="restricted",
                trigger="cron",
                cron="0 2 * * *",
                user=staff_user,
            )

    def test_the_service_layer_allows_with_the_permission(self, staff_user):
        staff_user.user_permissions.add(Permission.objects.get(codename="view_user"))
        staff_user = User.objects.get(pk=staff_user.pk)  # drop the perm cache
        row = create_schedule(
            name="r",
            task_key="restricted",
            trigger="cron",
            cron="0 2 * * *",
            user=staff_user,
        )
        assert row.pk

    def test_an_unrestricted_entry_needs_no_extra_permission(self, staff_user):
        assert create_schedule(
            name="ok",
            task_key="report",
            trigger="cron",
            cron="0 2 * * *",
            user=staff_user,
        ).pk

    def test_the_admin_will_not_save_a_restricted_row_without_the_permission(
        self, client, staff_user
    ):
        # With view permission but not change, Django renders the form
        # read-only rather than refusing outright, so a 200 proves nothing.
        # What matters is whether a POST can write.
        row = a_schedule(name="r", task_key="restricted")
        client.force_login(staff_user)
        response = client.post(
            reverse(CHANGE_URL, args=[row.pk]),
            {
                "name": row.name,
                "task_key": "restricted",
                "trigger": "cron",
                "cron": "0 4 * * *",
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
            },
        )
        assert response.status_code in (200, 302, 403)
        row.refresh_from_db()
        assert row.cron == "0 2 * * *", "the restricted row must be unchanged"

    def test_the_admin_reports_the_row_as_unchangeable(self, client, staff_user):
        row = a_schedule(name="r", task_key="restricted")
        client.force_login(staff_user)
        from django.contrib.admin.sites import site

        from django_ox.admin import OxScheduleAdmin

        model_admin = site._registry[OxSchedule]
        assert isinstance(model_admin, OxScheduleAdmin)
        request = client.request().wsgi_request
        request.user = staff_user
        assert not model_admin.has_change_permission(request, row)
        assert model_admin.has_change_permission(request, a_schedule(name="plain"))


class TestRunOnceNowMatchesADispatchedTick:
    def test_it_passes_the_cleaned_arguments(self, client, admin_user):
        # A dispatched tick carries what the form cleans to; running one
        # from the admin enqueued the row's raw values, so the same
        # schedule could carry different arguments depending on how it ran.
        from django import forms

        from django_ox.registry import ArgsForm, ScheduleKind, register

        class _Args(ArgsForm):
            count = forms.IntegerField()

        register(ScheduleKind(key="counted", task=tasks.add, form=_Args))
        row = a_schedule(name="counted", task_key="counted", arguments={"count": "5"})
        client.force_login(admin_user)
        client.post(
            reverse("admin:django_ox_oxschedule_changelist"),
            {"action": "run_once_now", "_selected_action": [str(row.pk)]},
            follow=True,
        )
        assert OxTask.objects.count() == 1
        assert OxTask.objects.get().kwargs == {"count": 5}, (
            "a manual run carried the raw string where a tick carries the int"
        )

    def test_a_row_that_cannot_run_is_reported_not_raised(self, client, admin_user):
        row = OxSchedule.objects.create(
            name="broken",
            task_key="report",
            trigger="cron",
            cron="banana",
            start_time=timezone.now(),
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("admin:django_ox_oxschedule_changelist"),
            {"action": "run_once_now", "_selected_action": [str(row.pk)]},
            follow=True,
        )
        assert response.status_code == 200
        assert OxTask.objects.count() == 0


class TestActions:
    def _post_action(self, client, action, pks):
        return client.post(
            reverse("admin:django_ox_oxschedule_changelist"),
            {"action": action, "_selected_action": [str(pk) for pk in pks]},
            follow=True,
        )

    def test_disable_then_enable(self, client, admin_user):
        row = a_schedule()
        client.force_login(admin_user)
        self._post_action(client, "disable_selected", [row.pk])
        row.refresh_from_db()
        assert not row.enabled
        paused_boundary = row.start_time
        self._post_action(client, "enable_selected", [row.pk])
        row.refresh_from_db()
        assert row.enabled
        assert row.start_time > paused_boundary, (
            "re-enabling must move the boundary, or a pause accumulates a "
            "backlog that fires all at once"
        )

    def test_run_once_now_enqueues_without_consuming_a_tick(self, client, admin_user):
        from django_ox.models import OxScheduleTick

        row = a_schedule()
        client.force_login(admin_user)
        self._post_action(client, "run_once_now", [row.pk])
        assert OxTask.objects.count() == 1
        assert not OxScheduleTick.objects.exists(), (
            "a manual run is not a tick and must not suppress the scheduled one"
        )

    def test_an_action_skips_rows_the_user_may_not_change(self, client, staff_user):
        allowed = a_schedule(name="allowed")
        restricted = a_schedule(name="restricted-row", task_key="restricted")
        client.force_login(staff_user)
        self._post_action(client, "disable_selected", [allowed.pk, restricted.pk])
        allowed.refresh_from_db()
        restricted.refresh_from_db()
        assert not allowed.enabled
        assert restricted.enabled, "the restricted row must be left alone"


class TestReEnableThroughTheChangeForm:
    def test_the_boundary_moves(self, client, admin_user):
        # Through the form, not the action. save_model hands update_schedule
        # form.instance, which _post_clean has already updated, so reading the
        # previous value off that instance never showed a transition.
        row = a_schedule()
        update_schedule(row, enabled=False)
        paused_boundary = row.start_time
        client.force_login(admin_user)
        response = client.post(
            reverse(CHANGE_URL, args=[row.pk]),
            {
                "name": row.name,
                "task_key": row.task_key,
                "trigger": "cron",
                "cron": row.cron,
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
            },
        )
        assert response.status_code == 302
        row.refresh_from_db()
        assert row.enabled
        assert row.start_time > paused_boundary, (
            "re-enabling through the change form must move the boundary too"
        )


class TestDeletePermission:
    def test_delete_consults_the_registry_permission(self, client, staff_user):
        from django.contrib.admin.sites import site

        restricted = a_schedule(name="r", task_key="restricted")
        plain = a_schedule(name="plain")
        model_admin = site._registry[OxSchedule]
        request = client.request().wsgi_request
        request.user = staff_user
        assert not model_admin.has_delete_permission(request, restricted)
        assert model_admin.has_delete_permission(request, plain)


class TestTheAddFlowLeavesASavedObject:
    def test_save_and_continue_editing_goes_to_the_row(self, client, admin_user):
        # save_model routes creation through the service layer, which builds
        # and saves its own instance. Without binding the result back, the
        # admin holds an object with no pk: this redirect went to a URL
        # containing None and 404ed.
        client.force_login(admin_user)
        response = client.post(
            reverse(ADD_URL),
            {
                "name": "nightly",
                "task_key": "report",
                "trigger": "cron",
                "cron": "0 2 * * *",
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
                "_continue": "Save and continue editing",
            },
        )
        row = OxSchedule.objects.get(name="nightly")
        assert response.status_code == 302
        assert "None" not in response["Location"]
        assert str(row.pk) in response["Location"]

    def test_the_admin_log_records_the_real_row(self, client, admin_user):
        from django.contrib.admin.models import LogEntry

        client.force_login(admin_user)
        client.post(
            reverse(ADD_URL),
            {
                "name": "nightly",
                "task_key": "report",
                "trigger": "cron",
                "cron": "0 2 * * *",
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
            },
        )
        row = OxSchedule.objects.get(name="nightly")
        entry = LogEntry.objects.latest("id")
        assert entry.object_id == str(row.pk), (
            "the row's admin history is attached to nothing"
        )
