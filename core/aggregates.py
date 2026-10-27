"""Counting helpers that stay fast with many rows."""
from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce


def count_by(queryset, fk_field):
    """
    Annotation expression: how many rows of `queryset` point at the outer row through `fk_field`.

    Several Count(..., distinct=True) annotations in one query join every table together and
    multiply the rows (users x students x classes); one small subquery per number does not.
    """
    inner = (
        queryset.filter(**{fk_field: OuterRef("pk")})
        .order_by().values(fk_field).annotate(c=Count("pk")).values("c")
    )
    return Coalesce(Subquery(inner, output_field=IntegerField()), 0)
