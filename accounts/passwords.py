"""
One place for the password rules, used when a password is chosen: change in Profile, reset by HOD/admin,
and registration of staff/HODs.

On top of Django's own checks (length, not too common, not all digits, not like the username) a password
must have an UPPERCASE letter, a lowercase letter, a digit and a symbol, and at least 10 characters.
The same rules are shown live in the browser (frontend/src/components/PasswordRules.jsx).
"""
import re

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

MIN_LENGTH = 10

# (message shown to the user, test that must be true)
RULES = [
    (f"At least {MIN_LENGTH} characters", lambda p: len(p) >= MIN_LENGTH),
    ("One capital letter (A-Z)", lambda p: re.search(r"[A-Z]", p) is not None),
    ("One small letter (a-z)", lambda p: re.search(r"[a-z]", p) is not None),
    ("One digit (0-9)", lambda p: re.search(r"[0-9]", p) is not None),
    ("One symbol such as ! @ # $ %", lambda p: re.search(r"[^A-Za-z0-9]", p) is not None),
]


def check_strong(value, user=None):
    """Raise a serializer error listing every rule the password breaks; return the password if it is fine."""
    problems = [text for text, ok in RULES if not ok(value)]
    if problems:
        raise serializers.ValidationError("The password needs: " + "; ".join(p.lower() for p in problems) + ".")
    validate_password(value, user)  # Django's checks: too common, all digits, similar to the username
    return value
