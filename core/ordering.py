"""Natural ordering for roll numbers, so 2 comes before 10 (plain text sorting gives 1, 10, 11, 2)."""
from django.db.models.functions import Length


def roll_order(prefix=""):
    """Use as qs.order_by(*roll_order()) or qs.order_by(*roll_order("student__"))."""
    return (Length(f"{prefix}roll_number"), f"{prefix}roll_number")
