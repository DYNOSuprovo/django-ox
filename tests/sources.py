"""Schedule sources a test can name in OPTIONS["SCHEDULE_SOURCE"]."""

from django_ox.stored import DatabaseScheduleSource


class RowSource(DatabaseScheduleSource):
    """A project's own source, under a name that is not the base class's."""


class CountingSource(DatabaseScheduleSource):
    """Records that this class, and not its base, built the schedule."""

    built = 0

    def _to_schedule(self, row):
        type(self).built += 1
        return super()._to_schedule(row)


class NotASource:
    """Importable, and not a schedule source."""


class DuckSource:
    """
    A source built by composition rather than inheritance.

    The worker's loader accepts any class it can build that answers
    schedules(), so this is a supported configuration and its rows are
    dispatched. It is not a DatabaseScheduleSource and has no
    _to_schedule.
    """

    def __init__(self, options, backend_alias):
        self._inner = DatabaseScheduleSource(options, backend_alias)

    def schedules(self):
        return self._inner.schedules()


class NoSchedulesMethod:
    """Builds from the same two arguments and answers nothing."""

    def __init__(self, options, backend_alias):
        self.options = options
