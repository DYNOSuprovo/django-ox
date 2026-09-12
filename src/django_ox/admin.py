"""
Django admin for the task table.

Loaded by the admin's autodiscover, so it is only imported when
django.contrib.admin is installed; a project without the admin never sees
this module and needs nothing from it. The list shows what a worker would
see; the detail page is read-only, with every attempt's traceback; and two
actions call django_ox.actions.retry_many and discard_many on the selected
rows, one conditional UPDATE per thousand rows in one transaction, and
report counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import actions, registry, stored
from .compat import DEFAULT_TASK_BACKEND_ALIAS
from .models import OxSchedule, OxScheduleTick, OxTask
from .schedules import STORED_KEY_PREFIX

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[OxTask]
    _ScheduleAdmin = admin.ModelAdmin[OxSchedule]
    _ScheduleForm = forms.ModelForm[OxSchedule]
else:
    _ModelAdmin = admin.ModelAdmin
    _ScheduleAdmin = admin.ModelAdmin
    _ScheduleForm = forms.ModelForm

ERROR_TEMPLATE = (
    '<p><strong>Attempt {}: {}</strong></p><pre style="white-space: pre-wrap">{}</pre>'
)


@admin.register(OxTask)
class OxTaskAdmin(_ModelAdmin):
    list_display = (
        "id",
        "task_path",
        "queue_name",
        "status",
        "attempts",
        "enqueued_at",
        "finished_at",
    )
    list_filter = ("status", "queue_name")
    search_fields = ("id", "task_path")
    ordering = ("-enqueued_at",)
    date_hierarchy = "enqueued_at"
    actions = ("retry_selected", "discard_selected")
    readonly_fields = (
        "id",
        "task_path",
        "args",
        "kwargs",
        "queue_name",
        "priority",
        "takes_context",
        "backend_name",
        "status",
        "run_after",
        "attempts",
        "max_attempts",
        "return_value",
        "worker_ids",
        "enqueued_at",
        "started_at",
        "last_attempted_at",
        "finished_at",
        "locked_by",
        "locked_at",
        "lease_expires_at",
        "lease_epoch",
        "attempt_errors",
    )
    fieldsets = (
        (None, {"fields": ("id", "task_path", "args", "kwargs", "status")}),
        (
            "Queue",
            {
                "fields": (
                    "queue_name",
                    "priority",
                    "backend_name",
                    "takes_context",
                    "run_after",
                )
            },
        ),
        (
            "Attempts",
            {
                "fields": (
                    "attempts",
                    "max_attempts",
                    "worker_ids",
                    "return_value",
                    "attempt_errors",
                )
            },
        ),
        (
            "Timing",
            {
                "fields": (
                    "enqueued_at",
                    "started_at",
                    "last_attempted_at",
                    "finished_at",
                )
            },
        ),
        (
            "Lease",
            {
                "fields": (
                    "locked_by",
                    "locked_at",
                    "lease_expires_at",
                    "lease_epoch",
                )
            },
        ),
    )

    # Rows are written by workers and by django_ox.actions only. The admin
    # can read them and run the two actions; it cannot add, edit or delete
    # one, because a hand-edited status would bypass the lease and a delete
    # could take a row from under a running worker. ox_prune deletes.
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: OxTask | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: OxTask | None = None
    ) -> bool:
        return False

    @admin.display(description="Attempt errors")
    def attempt_errors(self, obj: OxTask) -> str:
        if not obj.errors:
            return "No errors recorded."
        return format_html_join(
            "",
            ERROR_TEMPLATE,
            (
                (index, error["exception_class_path"], error["traceback"])
                for index, error in enumerate(obj.errors, start=1)
            ),
        )

    def _apply(
        self,
        request: HttpRequest,
        queryset: QuerySet[OxTask],
        action: Any,
        verb: str,
    ) -> None:
        done, skipped = action(queryset)
        self.message_user(request, f"{verb} {done} task(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                format_html(
                    "Skipped {} task(s) whose status did not allow it.", skipped
                ),
                messages.WARNING,
            )

    @admin.action(description="Retry selected tasks", permissions=("retry_or_discard",))
    def retry_selected(self, request: HttpRequest, queryset: QuerySet[OxTask]) -> None:
        self._apply(request, queryset, actions.retry_many, "Retried")

    @admin.action(
        description="Discard selected tasks", permissions=("retry_or_discard",)
    )
    def discard_selected(
        self, request: HttpRequest, queryset: QuerySet[OxTask]
    ) -> None:
        self._apply(request, queryset, actions.discard_many, "Discarded")

    def has_retry_or_discard_permission(self, request: HttpRequest) -> bool:
        # The model's change permission, without the change form: the
        # actions are the only writes the admin offers.
        opts = self.opts
        return request.user.has_perm(f"{opts.app_label}.change_{opts.model_name}")


class OxScheduleForm(_ScheduleForm):
    """
    The change form for a stored schedule.

    `task_key` is a choice drawn from the registry rather than a text
    input. That is the difference between this admin and every other
    Django scheduling admin: the field cannot express a task the code did
    not expose, so holding the change permission here is not permission to
    run any importable callable.

    A ChoiceField validates membership on the server, so a hand-made POST
    naming an unexposed task is refused here and not merely absent from the
    rendered select. The model checks membership again in its own clean(),
    which is what covers the write paths that never build a form:
    objects.create(), a data migration, a fixture.

    The field is a choice rather than a text input because a text input
    would let anyone holding the change permission name any importable
    callable.
    """

    class Meta:
        model = OxSchedule
        fields = (
            "name",
            "task_key",
            "trigger",
            "cron",
            "every_seconds",
            "phase_seconds",
            "arguments",
            "enabled",
            "end_time",
            "starting_deadline_seconds",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        keys = sorted(registry.kinds())
        self.fields["task_key"] = forms.ChoiceField(
            choices=[(key, key) for key in keys],
            label="Task",
            help_text=(
                "Tasks the code has exposed with @schedulable or "
                "SCHEDULABLE_TASKS. Nothing else can be scheduled."
                if keys
                else "No tasks are exposed yet. Register one with @schedulable."
            ),
        )

    def _effective_start_time(self, cleaned: dict[str, Any]) -> Any:
        """
        The activation boundary this submission will end up with.

        `start_time` is not a form field. The service layer sets it: to now
        when a schedule is created, retimed or re-enabled, and to whatever
        the row already held otherwise. Django excludes a field the form
        does not carry from its own validation, so without this the pair is
        never compared on the way in.
        """
        if self.instance.pk is None:
            return timezone.now()
        moved = any(
            cleaned.get(field) != getattr(self.instance, field)
            for field in stored.TIMING_FIELDS
        ) or (cleaned.get("enabled") and not self.instance.enabled)
        return timezone.now() if moved else self.instance.start_time

    def clean(self) -> dict[str, Any]:
        cleaned = cast("dict[str, Any]", super().clean())
        end_time = cleaned.get("end_time")
        if end_time is not None and end_time <= self._effective_start_time(cleaned):
            # As a field error rather than an exception out of the save. The
            # same rule runs again in validate_schedule, which is the
            # authority for every write path; this is what puts it in front
            # of the person filling the form in.
            self.add_error(
                "end_time",
                "The end time must be after the start time, which this "
                "submission sets to now.",
            )
        return cleaned


@admin.register(OxSchedule)
class OxScheduleAdmin(_ScheduleAdmin):
    form = OxScheduleForm
    list_display = (
        "name",
        "task_key",
        "timing",
        "enabled",
        "start_time",
        # Here because "Run selected schedules once now" ignores it, so an
        # operator choosing rows has to be able to see which of them have
        # already ended.
        "end_time",
        "last_tick",
    )
    list_filter = ("enabled", "trigger")
    search_fields = ("name", "task_key")
    ordering = ("name",)
    actions = ("enable_selected", "disable_selected", "run_once_now")
    readonly_fields = ("start_time", "created_at", "updated_at")

    @admin.display(description="Timing")
    def timing(self, obj: OxSchedule) -> str:
        if obj.trigger == OxSchedule.Trigger.CRON:
            return obj.cron
        every = f"every {obj.every_seconds}s"
        return f"{every} +{obj.phase_seconds}s" if obj.phase_seconds else every

    @admin.display(description="Last tick")
    def last_tick(self, obj: OxSchedule) -> str:
        tick = (
            OxScheduleTick.objects.filter(schedule_name=f"{STORED_KEY_PREFIX}{obj.pk}")
            .order_by("-scheduled_for")
            .values_list("scheduled_for", flat=True)
            .first()
        )
        return "never" if tick is None else f"{tick:%Y-%m-%d %H:%M}"

    def save_model(
        self,
        request: HttpRequest,
        obj: OxSchedule,
        form: Any,
        change: bool,  # noqa: FBT001 - ModelAdmin's own signature
    ) -> None:
        """
        Route through the service layer rather than calling save().

        The default implementation saves the instance directly, which would
        leave the activation boundary and the change marker untouched: a
        retimed schedule would keep a boundary set for its old timing, and
        no worker would learn the row had moved.
        """
        if change:
            # The values this request submitted, not every field off the
            # instance. A form is built from a row read at the start of the
            # request, so saving all of it would write back whatever else has
            # changed since, including a boundary someone else just moved.
            submitted = {
                field: form.cleaned_data[field]
                for field in form.fields
                if field in form.cleaned_data
            }
            stored.update_schedule(obj, user=request.user, **submitted)
        else:
            created = stored.create_schedule(
                user=request.user,
                **{
                    field: getattr(obj, field)
                    for field in (
                        "name",
                        "task_key",
                        "trigger",
                        "cron",
                        "every_seconds",
                        "phase_seconds",
                        "arguments",
                        "enabled",
                        "end_time",
                        "starting_deadline_seconds",
                    )
                },
            )
            # The service function builds and saves its own instance, and
            # the admin goes on using the one it holds. Without this its pk
            # stays None: "save and continue editing" redirects to a URL
            # containing None, and the log entry records the row's id as
            # the string "None", so its history is attached to nothing.
            obj.pk = created.pk
            obj.refresh_from_db()

    def has_change_permission(
        self, request: HttpRequest, obj: OxSchedule | None = None
    ) -> bool:
        """
        The model permission, plus the registry entry's own if it has one.

        Checked here because this is the hook Django gives an object to.
        The per-action hook does not take one, so an action's own check
        cannot see which task the selected rows name.
        """
        if not super().has_change_permission(request, obj):
            return False
        return self._registry_permitted(request, obj)

    def has_delete_permission(
        self, request: HttpRequest, obj: OxSchedule | None = None
    ) -> bool:
        """
        The registry entry's permission gates deletion too.

        Stopping a production schedule by deleting it is the same authority
        the permission exists to gate, so checking it only on change would
        leave the obvious way round.
        """
        if not super().has_delete_permission(request, obj):
            return False
        return self._registry_permitted(request, obj)

    def _registry_permitted(self, request: HttpRequest, obj: OxSchedule | None) -> bool:
        if obj is None:
            return True
        kind = registry.kinds().get(obj.task_key)
        if kind is None or kind.permission is None:
            return True
        return bool(
            request.user.has_perm(kind.permission, obj)
            or request.user.has_perm(kind.permission)
        )

    def delete_model(self, request: HttpRequest, obj: OxSchedule) -> None:
        stored.delete_schedule(obj)

    def delete_queryset(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        # One marker bump for the batch rather than one per row: workers only
        # need to learn that something moved.
        alias = stored.schedule_db_alias()
        with transaction.atomic(using=alias):
            queryset.using(alias).delete()
            stored._touch_change_row(alias)

    def _set_enabled(
        self,
        request: HttpRequest,
        queryset: QuerySet[OxSchedule],
        *,
        enabled: bool,
    ) -> None:
        changed = 0
        for schedule in queryset:
            if not self.has_change_permission(request, schedule):
                continue
            stored.update_schedule(schedule, user=request.user, enabled=enabled)
            changed += 1
        verb = "Enabled" if enabled else "Disabled"
        self.message_user(request, f"{verb} {changed} schedule(s).", messages.SUCCESS)
        skipped = queryset.count() - changed
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} schedule(s) you do not have permission to change.",
                messages.WARNING,
            )

    @admin.action(description="Enable selected schedules", permissions=("change",))
    def enable_selected(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        self._set_enabled(request, queryset, enabled=True)

    @admin.action(description="Disable selected schedules", permissions=("change",))
    def disable_selected(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        self._set_enabled(request, queryset, enabled=False)

    @admin.action(
        description="Run selected schedules once now", permissions=("change",)
    )
    def run_once_now(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        """
        Enqueue each selected schedule's task immediately.

        No tick row is written, because this is not a tick: the schedule's
        own ticks are unaffected and the next one still fires. A manual run
        does not consume a scheduled one.

        Built through the same path a dispatched tick takes, so a manual run
        carries the same arguments and goes to the same backend. Enqueueing
        the task directly passed the row's raw values where a tick passes
        the form's cleaned ones, and skipped the backend binding, so the
        two could differ in both what they carried and where they landed.

        A disabled schedule and one past its end time both run: this is the
        one way to run a paused schedule, and a run is not a tick, so
        neither bound applies to it. Both are reported, because the
        changelist shows paused and running rows together, an admin action
        has no confirmation step, and a success count alone leaves an
        operator who mis-selected a paused schedule with nothing to read.
        """
        alias, options = self._stored_backend()
        source = stored.DatabaseScheduleSource(options, alias)
        now = timezone.now()
        run, skipped, overridden = 0, 0, 0
        for schedule in queryset:
            if not self.has_change_permission(request, schedule):
                continue
            try:
                built = source._to_schedule(schedule)
            except Exception:
                skipped += 1
                continue
            built.task.enqueue(*built.args, **built.kwargs)
            run += 1
            # Counted after the enqueue, so a row that could not be built is
            # reported as skipped rather than as a run that overrode a bound.
            if not schedule.enabled or (
                schedule.end_time is not None and schedule.end_time < now
            ):
                overridden += 1
        self.message_user(request, f"Enqueued {run} task(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} schedule(s) that cannot run as written.",
                messages.WARNING,
            )
        if overridden:
            self.message_user(
                request,
                f"Ran {overridden} schedule(s) that were disabled or past "
                "their end time. A manual run ignores both.",
                messages.WARNING,
            )

    @staticmethod
    def _stored_backend() -> tuple[str, dict[str, Any]]:
        """
        The backend whose workers dispatch the stored schedules.

        Read from settings rather than from the instantiated backends, the
        same way the system checks read it: a check must not construct
        every backend in a project to answer a question about a dictionary.
        """
        for alias, config in settings.TASKS.items():
            options = config.get("OPTIONS") if isinstance(config, dict) else None
            if not isinstance(options, dict):
                continue
            if "DatabaseScheduleSource" in str(options.get("SCHEDULE_SOURCE", "")):
                return str(alias), options
        return DEFAULT_TASK_BACKEND_ALIAS, {}
