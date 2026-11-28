import io,uuid
from ninja.errors import HttpError
from PIL import Image, UnidentifiedImageError
from django.core.files.base import ContentFile



ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
FORMAT_TO_SAVE_META = {
    "JPEG": ("JPEG", "jpg"),
    "PNG": ("PNG", "png"),
    "WEBP": ("WEBP", "webp"),
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_IMAGE_PIXELS = 4000 * 4000     # Guard against decompression bombs


def validate_uploaded_image(uploaded_file, max_bytes: int = MAX_IMAGE_BYTES) -> Image.Image:
    if not uploaded_file:
        raise HttpError(400, "Passport photograph is required.")

    # 1. File size check
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > max_bytes:
        raise HttpError(400, f"Image must not exceed {max_bytes // (1024 * 1024)}MB.")

    # 2. Reject SVG / non-raster extensions upfront
    filename = (getattr(uploaded_file, "name", "") or "").lower()
    if filename.endswith(".svg"):
        raise HttpError(400, "SVG files are not allowed. Upload a JPG, PNG, or WEBP photo.")

    # 3. Read bytes safely without losing stream position
    pos = uploaded_file.tell() if hasattr(uploaded_file, "tell") else 0
    try:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        raw_bytes = uploaded_file.read()
    finally:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(pos)

    if not raw_bytes or len(raw_bytes) > max_bytes:
        raise HttpError(400, "Invalid or oversized image file.")

    # 4. Verify actual image structure with Pillow
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()  # Validates file headers & integrity

        # Re-open after verify() to inspect format and dimensions
        with Image.open(io.BytesIO(raw_bytes)) as decoded:
            fmt = (decoded.format or "").upper()
            if fmt not in ALLOWED_IMAGE_FORMATS:
                raise HttpError(
                    400,
                    f"Unsupported image format ({fmt or 'unknown'}). Please upload a JPG, PNG, or WEBP image."
                )

            width, height = decoded.size
            if width <= 0 or height <= 0 or (width * height) > MAX_IMAGE_PIXELS:
                raise HttpError(400, "Image dimensions are invalid or too large.")

            decoded.load()  # Force full pixel decoding to catch truncated/corrupt files
            return decoded.copy()
    except HttpError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise HttpError(
            400,
            "The uploaded file is not a valid image even if its extension is .jpg or .png."
        )

def _pil_image_to_content_file(pil_img: Image.Image, prefix: str) -> ContentFile:
    fmt = (pil_img.format or "JPEG").upper()
    save_fmt, ext = FORMAT_TO_SAVE_META.get(fmt, ("JPEG", "jpg"))

    buf = io.BytesIO()
    if save_fmt == "JPEG" and pil_img.mode not in ("RGB", "L"):
        pil_img = pil_img.convert("RGB")
        pil_img.save(buf, format="JPEG", quality=88, optimize=True)
    elif save_fmt == "JPEG":
        pil_img.save(buf, format="JPEG", quality=88, optimize=True)
    elif save_fmt == "PNG":
        pil_img.save(buf, format="PNG", optimize=True)
    else:
        pil_img.save(buf, format="WEBP", quality=85)

    buf.seek(0)
    return ContentFile(buf.read(), name=f"{prefix}_{uuid.uuid4().hex[:10]}.{ext}")