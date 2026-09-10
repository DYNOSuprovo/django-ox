"""The registry that decides what a schedule row may name."""

import pytest
from django import forms
from django.core.exceptions import ImproperlyConfigured

from django_ox.compat import default_task_backend
from django_ox.registry import (
    ArgsForm,
    ScheduleKind,
    get,
    kinds,
    kinds_from_options,
    register,
    schedulable,
)

from . import tasks


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    # Registration is process-global, exactly like Django's admin site, so
    # each test gets its own so one cannot leak a key into another.
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)


class TestRegistration:
    def test_a_registered_key_resolves_to_its_task(self):
        register(ScheduleKind(key="a.key", task=tasks.add))
        assert get("a.key").task is tasks.add

    def test_the_decorator_registers_the_task_object(self):
        schedulable("decorated")(tasks.add)
        assert get("decorated").task is tasks.add

    def test_a_duplicate_key_for_a_different_task_is_refused(self):
        # Two registrations under one key would make which task runs depend
        # on import order, and a row naming that key point at whichever won.
        register(ScheduleKind(key="dup", task=tasks.add))
        with pytest.raises(ImproperlyConfigured, match="already registered"):
            register(ScheduleKind(key="dup", task=tasks.echo))

    def test_registering_the_same_kind_twice_is_allowed(self):
        # A key declared by both the decorator and settings is not a clash.
        register(ScheduleKind(key="same", task=tasks.add))
        register(ScheduleKind(key="same", task=tasks.add))
        assert get("same").task is tasks.add

    def test_a_non_task_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match=r"not a django\.tasks Task"):
            register(ScheduleKind(key="raw", task=tasks._busy))

    def test_an_empty_key_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="non-empty string"):
            register(ScheduleKind(key="", task=tasks.add))

    def test_an_overlong_key_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="exceeds"):
            register(ScheduleKind(key="k" * 129, task=tasks.add))

    def test_an_unregistered_key_raises_keyerror(self):
        # Callers turn this into a validation error on the write path and a
        # skipped row on the dispatch path; during a rolling deploy an
        # unknown key may simply be one only newer code registers.
        with pytest.raises(KeyError):
            get("never.registered")

    def test_kinds_returns_a_copy(self):
        register(ScheduleKind(key="x", task=tasks.add))
        kinds()["x"] = None
        assert get("x").task is tasks.add


class _Args(ArgsForm):
    region = forms.CharField()
    count = forms.IntegerField(required=False)


class TestArgsForm:
    def test_valid_arguments_clean(self):
        form = _Args({"region": "emea", "count": 3})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["region"] == "emea"

    def test_an_unknown_argument_is_rejected_not_ignored(self):
        # Django iterates declared fields, so a typo or an argument left by
        # a refactor would validate here and fail inside the task later.
        form = _Args({"region": "emea", "reigon": "emea"})
        assert not form.is_valid()
        assert "reigon" in str(form.errors)

    def test_a_number_in_a_text_field_is_rejected_not_coerced(self):
        # A CharField given 5 returns "5", so the task receives a string
        # where the row meant a number and the task receives a string.
        form = _Args({"region": 5})
        assert not form.is_valid()
        assert "Expected a string" in str(form.errors["region"])

    def test_a_missing_required_argument_is_rejected(self):
        assert not _Args({}).is_valid()

    def test_no_data_is_not_the_same_as_unbound(self):
        # ArgsForm() with nothing must validate, not sit unbound and pass.
        assert not _Args().is_valid()


@pytest.mark.django_db
class TestSettingsChannel:
    def _options(self, entries):
        return {"SCHEDULABLE_TASKS": entries}

    def test_a_dotted_path_shorthand_registers(self):
        kinds_from_options(self._options({"k": "tests.tasks.add"}), "default")
        assert get("k").task is tasks.add

    def test_a_full_entry_registers_form_and_permission(self):
        kinds_from_options(
            self._options(
                {
                    "k": {
                        "task": "tests.tasks.add",
                        "form": "tests.test_registry._Args",
                        "permission": "app.run_it",
                    }
                }
            ),
            "default",
        )
        kind = get("k")
        assert kind.form is _Args
        assert kind.permission == "app.run_it"

    @pytest.mark.parametrize(
        ("entry", "match"),
        [
            ({"k": {}}, "missing 'task'"),
            ({"k": {"task": "tests.tasks.nope"}}, "cannot import task"),
            ({"k": {"task": "tests.tasks.add", "banana": 1}}, "unknown key"),
            (
                {"k": {"task": "tests.tasks.add", "form": "tests.tasks.nope"}},
                "cannot import form",
            ),
            (
                {"k": {"task": "tests.tasks.add", "form": "tests.tasks.add"}},
                "not a django_ox.registry.ArgsForm",
            ),
            (
                {"k": {"task": "tests.tasks.add", "permission": 1}},
                "'permission' must be a string",
            ),
            ({"k": 5}, "must be a mapping or a dotted path"),
        ],
    )
    def test_bad_entries_are_refused(self, entry, match):
        with pytest.raises(ImproperlyConfigured, match=match):
            kinds_from_options(self._options(entry), "default")

    def test_a_non_mapping_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="must be a mapping"):
            kinds_from_options({"SCHEDULABLE_TASKS": []}, "default")


@pytest.mark.django_db
def test_check_reports_a_bad_schedulable_entry(settings):
    # Through the real backend check, not the resolver.
    settings.TASKS = {
        "default": {
            "BACKEND": "django_ox.backend.OxBackend",
            "OPTIONS": {"SCHEDULABLE_TASKS": {"k": {"task": "tests.tasks.nope"}}},
        }
    }
    assert [e.id for e in default_task_backend.check()] == ["django_ox.E007"]
