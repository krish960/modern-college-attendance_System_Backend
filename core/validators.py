"""
Shared input checks, so every form refuses the same kinds of wrong text.

- person_name: names of people. Letters of any language (Marathi, Hindi, English ...), spaces, . ' and -. No digits or symbols.
- title_text: names of things (department, class, subject, designation). Letters, digits, spaces and . , & ( ) - / ' only; must contain a letter.
- code_text: IDs and codes (staff ID). Letters, digits and - _ / only.
- reason_text: a reason written by a person. At least 3 letters, so "!!!!!" or "12345" is not a reason.
Characters like < and > are never allowed, so nobody can store HTML or script text.
"""
import unicodedata

from rest_framework import serializers


def _is_letter(ch):
    """A letter (L...) or a combining mark (M...) used inside Devanagari and other scripts."""
    return unicodedata.category(ch)[0] in ("L", "M")


def person_name(value, label="Name", minimum=1):
    """Collapse spaces and allow only letters, spaces and . ' -. Raises a clear error otherwise."""
    value = " ".join(str(value).split())
    if len(value) < minimum:
        raise serializers.ValidationError(f"Enter the {label.lower()}.")
    bad = [c for c in value if not (_is_letter(c) or c in " .'-")]
    if bad or not any(_is_letter(c) for c in value):
        raise serializers.ValidationError(f"{label} may contain only letters, spaces and . ' - (no numbers or symbols).")
    return value


def title_text(value, label="Name"):
    """Collapse spaces; allow letters, digits, spaces and . , & ( ) - / '; require at least one letter."""
    value = " ".join(str(value).split())
    bad = [c for c in value if not (_is_letter(c) or c.isdigit() or c in " .,&()-/_'")]
    if bad or not any(_is_letter(c) for c in value):
        raise serializers.ValidationError(f"{label} may contain letters, numbers and . , & ( ) - / _ only, and needs at least one letter.")
    return value


def code_text(value, label="Code"):
    """Letters, digits and - _ / only (no spaces). Blank stays blank."""
    value = "".join(str(value).split())
    if value and not all(c.isalnum() or c in "-_/" for c in value):
        raise serializers.ValidationError(f"{label} may contain only letters, numbers and - _ /.")
    return value


def reason_text(value, minimum=5):
    """A written reason: trimmed, long enough, with at least 3 letters and no < or >."""
    value = str(value).strip()
    if len(value) < minimum:
        raise serializers.ValidationError(f"Give the reason (at least {minimum} characters).")
    if sum(1 for c in value if _is_letter(c)) < 3 or "<" in value or ">" in value:
        raise serializers.ValidationError("Write the reason in words (at least 3 letters, and no < or > characters).")
    return value


def card_text(value):
    """An ID card (barcode) code: no spaces, capitals, and only letters, digits and - . _ / $ + % (what barcodes use)."""
    value = "".join(str(value or "").split()).upper()
    if value and not all(c.isalnum() or c in "-._/$+%" for c in value):
        raise serializers.ValidationError("The card code may contain only letters, numbers and - . _ / $ + %.")
    return value


def roll_text(value):
    """A roll number that is already cleaned (no spaces, capitals). Letters, digits and - / inside; starts with a letter or digit; not only zeros."""
    if not value or not value[0].isalnum() or not all(c.isalnum() or c in "-/" for c in value) or not value.strip("0-/"):
        raise serializers.ValidationError("Roll number may contain only letters, numbers and - /, for example 12 or A-12.")
    return value
