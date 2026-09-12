from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ox", "0006_lease_expiry"),
    ]

    operations = [
        migrations.CreateModel(
            name="OxScheduleChange",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, primary_key=True, serialize=False
                    ),
                ),
                ("changed_at", models.DateTimeField()),
            ],
        ),
        migrations.CreateModel(
            name="OxSchedule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=128, unique=True)),
                ("task_key", models.CharField(max_length=128)),
                (
                    "trigger",
                    models.CharField(
                        choices=[
                            ("cron", "Cron expression"),
                            ("interval", "Fixed interval"),
                        ],
                        max_length=16,
                    ),
                ),
                ("cron", models.CharField(blank=True, default="", max_length=128)),
                ("every_seconds", models.PositiveIntegerField(blank=True, null=True)),
                ("phase_seconds", models.PositiveIntegerField(default=0)),
                ("arguments", models.JSONField(blank=True, default=dict)),
                ("enabled", models.BooleanField(default=True)),
                ("start_time", models.DateTimeField()),
                (
                    "boundary_for",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("boundary_generation", models.PositiveIntegerField(default=0)),
                ("end_time", models.DateTimeField(blank=True, null=True)),
                (
                    "starting_deadline_seconds",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                ("created_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField()),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("every_seconds__isnull", True), ("trigger", "cron")
                            ),
                            models.Q(
                                ("cron", ""),
                                ("every_seconds__isnull", False),
                                ("trigger", "interval"),
                            ),
                            _connector="OR",
                        ),
                        name="ox_schedule_one_trigger",
                    )
                ],
            },
        ),
    ]
