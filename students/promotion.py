"""
The rules for moving a class to the next class, in one place.
`plan_promotion` only works out who would move; `carry_out` really moves them.
The request / approval steps live in views.py (ClassPromotionViewSet).
"""
from django.db import transaction
from django.utils import timezone

from core.ordering import roll_order

from .models import Student


def plan_promotion(source, target, leave_behind=()):
    """
    Returns (to_move, left_behind, clashes), each a list of students of `source`.
    Only ACTIVE students are considered. A student is a clash when the new class already has his roll number:
    he is not moved, so nobody is silently renumbered.
    """
    leave_behind = {int(i) for i in leave_behind}
    students = list(Student.objects.filter(classroom=source, is_active=True).defer("photo").order_by(*roll_order()))
    left = [s for s in students if s.pk in leave_behind]
    rest = [s for s in students if s.pk not in leave_behind]
    taken = set(Student.objects.filter(classroom=target).values_list("roll_number", flat=True))
    clashes = [s for s in rest if s.roll_number in taken]
    moving = [s for s in rest if s.roll_number not in taken]
    return moving, left, clashes


@transaction.atomic
def carry_out(source, target, leave_behind=()):
    """Move the students for real. Roll number and ID card stay the same; old attendance stays with the old class's lectures."""
    moving, left, clashes = plan_promotion(source, target, leave_behind)
    if moving:
        Student.objects.filter(pk__in=[s.pk for s in moving]).update(classroom=target, updated_at=timezone.now())
    return moving, left, clashes
