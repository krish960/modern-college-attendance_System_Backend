"""Recompute each submitted lecture's stored present/absent totals from its attendance records.

    python manage.py recount_attendance

The totals are kept up to date automatically (on submit and on approved corrections). Run this
only if the database was edited by hand or restored from an old backup.
"""
from django.core.management.base import BaseCommand
from django.db import models
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce

from attendance.models import AttendanceRecord, Lecture


class Command(BaseCommand):
    """The command itself: Django runs handle() when you type the command name."""

    help = "Recompute present/absent totals of submitted lectures from their attendance records."

    def handle(self, *args, **options):
        """Run the recount for every submitted lecture and print how many were updated."""
        def counted(status):
            """Subquery that counts this lecture's records with the given status (0 if none)."""
            return Coalesce(
                Subquery(
                    AttendanceRecord.objects.filter(lecture=OuterRef("pk"), status=status)
                    .order_by().values("lecture").annotate(c=Count("pk")).values("c"),
                    output_field=models.IntegerField(),
                ),
                0,
            )

        # Only SUBMITTED lectures have final totals; drafts are not counted yet.
        updated = Lecture.objects.filter(status=Lecture.Status.SUBMITTED).update(
            present_total=counted("PRESENT"), absent_total=counted("ABSENT"))
        self.stdout.write(self.style.SUCCESS(f"Recounted {updated} submitted lecture(s)."))
