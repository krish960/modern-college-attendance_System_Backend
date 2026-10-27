from django.db import migrations


def tidy(apps, schema_editor):
    """Make old subject codes look the same way new ones do ("cs 101" -> "CS101") unless that would clash with another code."""
    Subject = apps.get_model("academics", "Subject")
    key = lambda c: "".join(ch for ch in c.upper() if ch.isalnum())
    seen = {}
    for s in Subject.objects.order_by("id"):
        seen.setdefault((s.department_id, key(s.code)), []).append(s)
    for group in seen.values():
        if len(group) != 1:
            continue  # two codes that now count as the same: leave both, the HOD merges them by hand
        s = group[0]
        clean = "".join(s.code.split()).upper()
        if clean and clean != s.code and not Subject.objects.filter(department_id=s.department_id, code=clean).exclude(pk=s.pk).exists():
            s.code = clean
            s.save(update_fields=["code"])


class Migration(migrations.Migration):
    dependencies = [("academics", "0002_initial")]
    operations = [migrations.RunPython(tidy, migrations.RunPython.noop)]
