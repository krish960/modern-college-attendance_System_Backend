"""Student photos: validate, fix orientation, shrink and re-encode as JPEG (which also drops EXIF/GPS data)."""
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SIDE = 480
MAX_PIXELS = 40_000_000  # refuse decompression bombs


def process_photo(upload):
    """Check an uploaded photo and return clean JPEG bytes. Raises ValidationError with a friendly message if it is bad."""
    if upload is None:
        raise ValidationError({"photo": "Choose a photo."})
    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError({"photo": "The photo is larger than 5 MB."})
    try:
        image = Image.open(upload)
        if image.width * image.height > MAX_PIXELS:
            raise ValidationError({"photo": "The photo has too many pixels."})
        if image.format not in ("JPEG", "PNG", "WEBP"):
            raise ValidationError({"photo": "Use a JPEG, PNG or WebP photo."})
        image.load()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise ValidationError({"photo": "This file is not a usable image."})
    # Rotate as the camera intended, drop transparency, and shrink so the database stays small.
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = BytesIO()
    image.save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue()
