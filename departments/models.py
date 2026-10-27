from django.db import models
from django.db.models.functions import Lower


class Department(models.Model):
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=10)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            # Case-insensitive uniqueness: "Computer Science" == "computer science"
            models.UniqueConstraint(Lower("name"), name="uniq_department_name_ci"),
            models.UniqueConstraint(Lower("code"), name="uniq_department_code_ci"),
        ]

    def __str__(self):
        return self.name
