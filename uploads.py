"""Upload handling and QR label rendering.

Uploads are stored under ``data/uploads`` with generated names. The original
filename is kept only as a display string, never used as a path, and the file
type is checked by magic bytes as well as extension.
"""

import io
import os
import uuid

from db import UPLOAD_DIR

IMAGE_TYPES = {
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"GIF87a": ("gif", "image/gif"),
    b"GIF89a": ("gif", "image/gif"),
}
PDF_MAGIC = b"%PDF-"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024


class UploadError(ValueError):
    pass


def _subdir(name):
    path = os.path.join(UPLOAD_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


def save_image(file_storage):
    """Validate and store an uploaded photograph. Returns the relative path."""
    data = file_storage.read()
    if not data:
        raise UploadError("The uploaded image is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadError("Images must be 8 MB or smaller.")
    ext, mime = None, None
    for magic, (e, m) in IMAGE_TYPES.items():
        if data.startswith(magic):
            ext, mime = e, m
            break
    if ext is None:
        raise UploadError("Only JPEG, PNG or GIF images are accepted.")
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(_subdir("images"), name), "wb") as fh:
        fh.write(data)
    return f"images/{name}", mime


def save_pdf(file_storage):
    """Validate and store an uploaded PDF. Returns (relative_path, size)."""
    data = file_storage.read()
    if not data:
        raise UploadError("The uploaded file is empty.")
    if len(data) > MAX_PDF_BYTES:
        raise UploadError("PDFs must be 20 MB or smaller.")
    if not data.startswith(PDF_MAGIC):
        raise UploadError("Only PDF documents are accepted for service manuals.")
    name = f"{uuid.uuid4().hex}.pdf"
    with open(os.path.join(_subdir("documents"), name), "wb") as fh:
        fh.write(data)
    return f"documents/{name}", len(data)


def absolute(relative_path):
    if not relative_path:
        return None
    safe = os.path.normpath(relative_path).lstrip("/\\")
    if safe.startswith(".."):
        return None
    return os.path.join(UPLOAD_DIR, safe)


def delete(relative_path):
    path = absolute(relative_path)
    if path and os.path.isfile(path):
        os.remove(path)
        return True
    return False


def qr_png(payload, box_size=8):
    """Render a PNG QR code. Returns bytes, or None if qrcode is not installed."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f2b46", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
