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
