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


def _form_datetime(at):
    """
    Split an instant the way the admin's two-part datetime widget reads it
    back: in the project's timezone.

    Formatting an aware value straight off a row splits its UTC wall clock
    instead, so the form reads back an instant a whole UTC offset from the
    one the row holds. That passes only where the project's zone is west of
    UTC by more than the margin the test left itself, and the smallest of
    those margins is an hour. America/Chicago is west by five, so these
    tests hold under the settings this suite runs; under any zone an hour
    or more east of UTC they would fail every time.
    """
    local = timezone.localtime(at) if timezone.is_aware(at) else at
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")


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
        client.post(
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
        # A dispatched tick carries what the form cleans to, so running one
        # from the admin must build it the same way: the row's raw values
        # would make the same schedule differ by how it was started.
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
        # form.instance, which _post_clean has already updated, so reading
        # the previous value off that instance would never show a transition.
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
        # and saves its own instance. Without binding the result back the
        # admin holds an object with no pk, and this redirect targets a URL
        # containing None.
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


class TestAnEndTimeInThePastIsAFieldError:
    """
    `start_time` is not a form field, so Django leaves it out of the form's
    own validation and the pair is never compared on the way in. The
    service layer then sets it to now and refuses the row, which reaches
    the person filling the form in as a server error rather than as an
    error on the field they got wrong.
    """

    def _post(self, client, **over):
        fields = {
            "name": "nightly",
            "task_key": "report",
            "trigger": "cron",
            "cron": "0 2 * * *",
            "arguments": "{}",
            "phase_seconds": 0,
            "enabled": "on",
        }
        fields.update(over)
        return client.post(reverse(ADD_URL), fields)

    def test_adding_one_reports_the_field_rather_than_failing(self, client, admin_user):
        client.force_login(admin_user)
        yesterday = timezone.now() - timedelta(days=1)
        day, clock = _form_datetime(yesterday)
        response = self._post(client, end_time_0=day, end_time_1=clock)
        assert response.status_code == 200, "the form should be redisplayed"
        assert "end_time" in response.context["adminform"].form.errors
        assert not OxSchedule.objects.filter(name="nightly").exists()

    def test_a_future_end_time_is_accepted(self, client, admin_user):
        client.force_login(admin_user)
        tomorrow = timezone.now() + timedelta(days=1)
        day, clock = _form_datetime(tomorrow)
        response = self._post(client, end_time_0=day, end_time_1=clock)
        assert response.status_code == 302
        assert OxSchedule.objects.filter(name="nightly").exists()

    def test_an_existing_schedule_may_keep_a_past_end_time(self, client, admin_user):
        # Not every past end time is wrong: a schedule that ran and has
        # since ended holds one legitimately. Only a submission that also
        # moves the boundary to now makes the pair impossible.
        client.force_login(admin_user)
        row = a_schedule(name="ended")
        past = row.start_time + timedelta(hours=1)
        OxSchedule.objects.filter(pk=row.pk).update(end_time=past)
        day, clock = _form_datetime(past)
        response = client.post(
            reverse(CHANGE_URL, args=[row.pk]),
            {
                "name": "ended",
                "task_key": "report",
                "trigger": "cron",
                "cron": row.cron,
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
                "end_time_0": day,
                "end_time_1": clock,
            },
        )
        assert response.status_code == 302, "an unchanged timing must still save"


class TestDeletingThroughTheAdminTellsTheWorkers:
    """
    Both delete hooks reach into the service layer to bump the change
    marker. Without it a deleted schedule stays in every running worker's
    cache, enqueueing and rolling back once a pass.
    """

    def test_deleting_one_row_bumps_the_marker(self, client, admin_user):
        from django_ox.models import OxScheduleChange

        client.force_login(admin_user)
        row = a_schedule()
        before = OxScheduleChange.objects.get(id=1).changed_at
        response = client.post(
            reverse("admin:django_ox_oxschedule_delete", args=[row.pk]),
            {"post": "yes"},
        )
        assert response.status_code == 302
        assert not OxSchedule.objects.filter(pk=row.pk).exists()
        assert OxScheduleChange.objects.get(id=1).changed_at > before

    def test_deleting_a_selection_bumps_the_marker_once(self, client, admin_user):
        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME

        from django_ox.models import OxScheduleChange

        client.force_login(admin_user)
        rows = [a_schedule(name=f"s{i}") for i in range(3)]
        before = OxScheduleChange.objects.get(id=1).changed_at
        response = client.post(
            reverse("admin:django_ox_oxschedule_changelist"),
            {
                "action": "delete_selected",
                ACTION_CHECKBOX_NAME: [str(r.pk) for r in rows],
                "post": "yes",
            },
        )
        assert response.status_code == 302
        assert not OxSchedule.objects.exists()
        assert OxScheduleChange.objects.get(id=1).changed_at > before
