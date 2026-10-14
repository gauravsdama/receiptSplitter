from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from receiptsplitter import images
from receiptsplitter.images import ReceiptInputError


def _png(path: Path, size: tuple[int, int] = (40, 60)) -> Path:
    Image.new("RGB", size, "white").save(path)
    return path


def test_validates_folder_extension_signature_size_and_dimensions(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    valid = _png(allowed / "receipt.png")
    assert images.validate_receipt_path(str(valid), root=allowed) == valid.resolve()

    outside = _png(tmp_path / "outside.png")
    with pytest.raises(ReceiptInputError, match="outside"):
        images.validate_receipt_path(str(outside), root=allowed)

    disguised = allowed / "disguised.png"
    disguised.write_text("not an image", encoding="utf-8")
    with pytest.raises(ReceiptInputError, match=r"unreadable|signature"):
        images.validate_receipt_path(str(disguised), root=allowed)

    wrong_extension = _png(allowed / "receipt.gif")
    with pytest.raises(ReceiptInputError, match="PNG or JPEG"):
        images.validate_receipt_path(str(wrong_extension), root=allowed)

    with pytest.raises(ReceiptInputError, match="size limit"):
        images.validate_receipt_path(str(valid), root=allowed, max_bytes=1)

    with pytest.raises(ReceiptInputError, match="too large"):
        images.validate_receipt_path(str(valid), root=allowed, max_pixels=10)


def test_preprocessing_checks_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _png(tmp_path / "receipt.png")
    monkeypatch.setattr(cv2, "imwrite", lambda *_args, **_kwargs: False)
    with pytest.raises(OSError, match="preprocessing"):
        images.preprocess_receipt(source, tmp_path / "output.png")


def test_ocr_derivative_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _png(tmp_path / "receipt.png")
    observed: list[Path] = []

    def fake_ocr(path: Path, _timeout_seconds: float) -> str:
        observed.append(Path(path))
        assert Path(path).exists()
        return "Tea 2.00\nTOTAL 2.00"

    monkeypatch.setattr(images, "_tesseract_text", fake_ocr)
    assert images.extract_receipt_lines(source) == ["Tea 2.00", "TOTAL 2.00"]
    assert observed and not observed[0].exists()


def test_ocr_engine_and_timeout_settings_are_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _png(tmp_path / "receipt.png")
    monkeypatch.setenv("RECEIPT_OCR_ENGINE", "remote")
    with pytest.raises(images.ReceiptConfigurationError, match=r"tesseract.*paddle"):
        images.extract_receipt_lines(source)

    monkeypatch.setenv("RECEIPT_OCR_ENGINE", "tesseract")
    with pytest.raises(images.ReceiptConfigurationError, match="between 1 and 300"):
        images.extract_receipt_lines(source, timeout_seconds=0)


def test_tesseract_receives_a_supported_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "receipt.png"
    source.touch()
    observed: dict[str, object] = {}

    def fake_image_to_string(value: object, **kwargs: object) -> str:
        observed["value"] = value
        observed.update(kwargs)
        return "Tea 2.00"

    monkeypatch.setattr("pytesseract.image_to_string", fake_image_to_string)

    assert images._tesseract_text(source, 12) == "Tea 2.00"
    assert observed == {"value": str(source), "config": "--psm 6", "timeout": 12}


def test_perspective_preprocessing_produces_an_image(tmp_path: Path) -> None:
    source = tmp_path / "receipt.png"
    canvas = np.zeros((500, 400, 3), dtype=np.uint8)
    cv2.rectangle(canvas, (40, 30), (360, 470), (255, 255, 255), -1)
    assert cv2.imwrite(str(source), canvas)
    output = tmp_path / "output.png"

    assert images.preprocess_receipt(source, output) == output
    assert output.exists() and output.stat().st_size > 0
