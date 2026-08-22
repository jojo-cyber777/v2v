"""Validated private image storage for recharge screenshots and WeChat QR codes."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 5 * 1024 * 1024
FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


class BillingFileError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def store_validated_image(content: bytes, directory: Path, *, prefix: str) -> Path:
    if not content:
        raise BillingFileError("IMAGE_EMPTY", "The image file is empty.")
    if len(content) > MAX_IMAGE_BYTES:
        raise BillingFileError("IMAGE_TOO_LARGE", "The image must be 5 MB or smaller.")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
            image_format = image.format
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise BillingFileError("IMAGE_INVALID", "Upload a valid PNG, JPEG, or WebP image.") from error
    extension = FORMAT_EXTENSIONS.get(str(image_format).upper())
    if not extension:
        raise BillingFileError("IMAGE_FORMAT_UNSUPPORTED", "Only PNG, JPEG, and WebP images are supported.")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{prefix}-{uuid.uuid4().hex}{extension}"
    target.write_bytes(content)
    return target


def resolve_private_file(value: str | None, allowed_root: Path) -> Path | None:
    if not value:
        return None
    try:
        target = Path(value).resolve()
        target.relative_to(allowed_root.resolve())
    except (OSError, ValueError):
        return None
    return target if target.is_file() else None
