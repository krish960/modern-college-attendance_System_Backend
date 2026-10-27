from django.db import migrations


def strip_zeros(apps, schema_editor):
    """Roll numbers that are plain numbers lose their leading zeros ("01" -> "1"), unless that would clash with another student of the class."""
    Student = apps.get_model("students", "Student")
    taken = {(c, r) for c, r in Student.objects.values_list("classroom_id", "roll_number")}
    for s in Student.objects.filter(roll_number__regex=r"^0+[0-9]*$").order_by("id"):
        new = s.roll_number.lstrip("0") or "0"
        if new != s.roll_number and (s.classroom_id, new) not in taken:
            taken.discard((s.classroom_id, s.roll_number))
            taken.add((s.classroom_id, new))
            Student.objects.filter(pk=s.pk).update(roll_number=new)


class Migration(migrations.Migration):
    dependencies = [("students", "0004_student_photo")]
    operations = [migrations.RunPython(strip_zeros, migrations.RunPython.noop)]
