from __future__ import annotations

import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png"}
ALLOWED_FORMATS = {"JPEG", "PNG"}
DEFAULT_MAX_BYTES = 15 * 1024 * 1024
DEFAULT_MAX_PIXELS = 40_000_000
SUPPORTED_OCR_ENGINES = {"tesseract", "paddle"}


class ReceiptInputError(ValueError):
    pass


class ReceiptConfigurationError(ValueError):
    pass


class ReceiptProcessingError(RuntimeError):
    pass


class ReceiptTimeoutError(ReceiptProcessingError):
    pass


class ReceiptBusyError(ReceiptProcessingError):
    pass


def configured_input_root() -> Path:
    configured = os.environ.get("RECEIPT_ALLOWED_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Documents" / "Receipt Splitter" / "Inputs").resolve()


def validate_receipt_path(
    value: str,
    *,
    root: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> Path:
    from PIL import Image, UnidentifiedImageError

    allowed_root = (root or configured_input_root()).resolve()
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(allowed_root):
        raise ReceiptInputError("The receipt is outside the configured input folder.")
    if path.suffix.lower() not in ALLOWED_SUFFIXES or not path.is_file():
        raise ReceiptInputError("Use an existing PNG or JPEG receipt image.")
    if path.stat().st_size > max_bytes:
        raise ReceiptInputError("The receipt image exceeds the configured size limit.")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format not in ALLOWED_FORMATS:
                raise ReceiptInputError("The file content is not PNG or JPEG.")
            if image.width * image.height > max_pixels:
                raise ReceiptInputError("The decoded receipt image is too large.")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as cause:
        raise ReceiptInputError(
            "The receipt image is unreadable or has an invalid signature."
        ) from cause
    return path


def _order_points(points: Any) -> Any:
    import numpy as np

    points = points.reshape(4, 2).astype("float32")
    ordered = np.zeros((4, 2), dtype="float32")
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered


def _perspective_crop(image: Any) -> Any:
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    scale = min(1.0, 1200.0 / max(height, width))
    preview = (
        cv2.resize(image, (int(width * scale), int(height * scale))) if scale < 1 else image.copy()
    )
    gray = cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)
    minimum_area = preview.shape[0] * preview.shape[1] * 0.15
    for contour in sorted(
        cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0],
        key=cv2.contourArea,
        reverse=True,
    )[:12]:
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(polygon) != 4 or cv2.contourArea(polygon) < minimum_area:
            continue
        points = _order_points(polygon / scale)
        top_left, top_right, bottom_right, bottom_left = points
        target_width = int(
            max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left))
        )
        target_height = int(
            max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left))
        )
        if target_width < 2 or target_height < 2:
            break
        destination = np.array(
            [
                [0, 0],
                [target_width - 1, 0],
                [target_width - 1, target_height - 1],
                [0, target_height - 1],
            ],
            dtype="float32",
        )
        transform = cv2.getPerspectiveTransform(points, destination)
        return cv2.warpPerspective(image, transform, (target_width, target_height))
    return image


def preprocess_receipt(input_path: Path, output_path: Path) -> Path:
    import cv2

    image = cv2.imread(str(input_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ReceiptInputError("The receipt image could not be decoded.")
    cropped = _perspective_crop(image)
    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
    enhanced = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    if max(enhanced.shape) < 2200:
        scale = 2200.0 / max(enhanced.shape)
        enhanced = cv2.resize(enhanced, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if not cv2.imwrite(str(output_path), enhanced):
        raise OSError("Could not create the private preprocessing image.")
    return output_path


@lru_cache(maxsize=1)
def _paddle_engine() -> Any:
    from paddleocr import PaddleOCR

    return PaddleOCR(use_textline_orientation=True, lang="en")


def _paddle_text(path: Path) -> str:
    result = _paddle_engine().predict(str(path))
    lines: list[str] = []
    for page in result or []:
        data = getattr(page, "json", None)
        if isinstance(data, dict):
            texts = data.get("res", {}).get("rec_texts", [])
            lines.extend(str(text).strip() for text in texts if str(text).strip())
    return "\n".join(lines)


def configured_ocr_engine() -> str:
    engine = os.environ.get("RECEIPT_OCR_ENGINE", "tesseract").strip().lower()
    if engine not in SUPPORTED_OCR_ENGINES:
        raise ReceiptConfigurationError("RECEIPT_OCR_ENGINE must be 'tesseract' or 'paddle'.")
    if engine == "paddle" and sys.version_info >= (3, 14):
        raise ReceiptConfigurationError(
            "Paddle OCR is unavailable on Python 3.14; use Python 3.13 or Tesseract."
        )
    return engine


def _tesseract_text(path: Path, timeout_seconds: float) -> str:
    import pytesseract

    try:
        return str(
            pytesseract.image_to_string(str(path), config="--psm 6", timeout=timeout_seconds)
        )
    except RuntimeError as cause:
        raise ReceiptTimeoutError("Tesseract did not finish within the OCR time limit.") from cause


def extract_receipt_lines(input_path: Path, *, timeout_seconds: float = 30.0) -> list[str]:
    if not 1.0 <= timeout_seconds <= 300.0:
        raise ReceiptConfigurationError("OCR timeout must be between 1 and 300 seconds.")
    engine = configured_ocr_engine()
    with tempfile.TemporaryDirectory(prefix="receipt-splitter-") as directory:
        private_output = Path(directory) / "preprocessed.png"
        preprocess_receipt(input_path, private_output)
        text = (
            _paddle_text(private_output)
            if engine == "paddle"
            else _tesseract_text(private_output, timeout_seconds)
        )
    return [line.strip() for line in text.splitlines() if line.strip()]
