"""Rules for uploaded note files: allowed types, safe names and size text."""
import os
from io import BytesIO

from PIL import Image
from rest_framework import serializers

# Allow-list of file types. Each extension maps to (content type, check on the first bytes).
# The first-bytes check stops someone renaming a program to .pdf. Office files are zip files (start with PK).
ALLOWED = {
    ".pdf": ("application/pdf", lambda h: h.startswith(b"%PDF")),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", lambda h: h.startswith(b"PK")),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", lambda h: h.startswith(b"PK")),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", lambda h: h.startswith(b"PK")),
    ".doc": ("application/msword", lambda h: h.startswith(b"\xd0\xcf\x11\xe0")),
    ".ppt": ("application/vnd.ms-powerpoint", lambda h: h.startswith(b"\xd0\xcf\x11\xe0")),
    ".xls": ("application/vnd.ms-excel", lambda h: h.startswith(b"\xd0\xcf\x11\xe0")),
    ".txt": ("text/plain; charset=utf-8", lambda h: b"\x00" not in h),
    ".png": ("image/png", lambda h: h.startswith(b"\x89PNG")),
    ".jpg": ("image/jpeg", lambda h: h.startswith(b"\xff\xd8")),
    ".jpeg": ("image/jpeg", lambda h: h.startswith(b"\xff\xd8")),
}


def clean_name(name):
    """Make an uploaded file name safe: no folders, no odd characters, at most 150 letters."""
    base = os.path.basename(str(name or "")).replace("\x00", "")
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '\\/:*?"<>|')
    return base.strip()[:150] or "file"


def check_upload(upload, max_mb):
    """Only a short list of document / image types, within the size limit, and the content must match the extension."""
    name = clean_name(upload.name)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED:
        raise serializers.ValidationError("This file type is not allowed. Use PDF, Word, PowerPoint, Excel, text or an image (PNG, JPG).")
    if upload.size > max_mb * 1024 * 1024:
        raise serializers.ValidationError(f"The file is too big. The limit is {max_mb} MB.")
    if upload.size == 0:
        raise serializers.ValidationError("The file is empty.")
    head = upload.read(2048)
    upload.seek(0)
    if not ALLOWED[ext][1](head):
        raise serializers.ValidationError("The file content does not match its type. Choose the original file.")
    if ext in (".png", ".jpg", ".jpeg"):
        try:
            Image.open(BytesIO(upload.read())).verify()
        except Exception:
            raise serializers.ValidationError("This image is damaged.")
        finally:
            upload.seek(0)
    return name, ext


def content_type_for(file_name):
    """Return the browser content type for a file name (unknown types are treated as raw bytes)."""
    return ALLOWED.get(os.path.splitext(file_name)[1].lower(), ("application/octet-stream", None))[0]


def human_size(n):
    """Turn a byte count into text like 12 KB or 1.4 MB."""
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
