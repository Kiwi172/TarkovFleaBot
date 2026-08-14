"""OCR for in-game numeric text (prices, stack counts, balances).

Tarkov renders small light-on-dark UI text, which stock Tesseract handles
badly. The preprocessing pipeline that actually matters:

1. **upscale** (3-4x, cubic) — Tesseract wants ~30px glyph height,
2. **grayscale**,
3. **invert** — Tesseract expects dark text on light,
4. **Otsu threshold** — binarise without hand-tuning a cutoff,
5. **char whitelist + PSM 7** — constrain the search to a single line of digits.

Skipping step 1 is the single most common cause of garbage output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

from flea_bot.config import Config, Region, get_config
from flea_bot.logging_setup import get_logger
from flea_bot.vision.capture import ScreenCapture

log = get_logger("ocr")

# Digits with optional thousands separators; tolerates a trailing currency mark.
_NUMBER_RE = re.compile(r"\d[\d\s.,]*")
# "x12", "12x", "12 pcs" — stack-count forms.
_QUANTITY_RE = re.compile(r"(?:x\s*(\d+))|(?:(\d+)\s*(?:x|pcs?))", re.IGNORECASE)


class OCRUnavailableError(RuntimeError):
    """Tesseract is not installed or not on PATH."""


@dataclass(frozen=True, slots=True)
class OCRResult:
    text: str
    value: int | None
    confidence: float

    @property
    def ok(self) -> bool:
        return self.value is not None


def _configure_tesseract(config: Config) -> None:
    import pytesseract  # noqa: PLC0415

    if config.ocr.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = config.ocr.tesseract_cmd


def preprocess(image: np.ndarray, config: Config | None = None) -> np.ndarray:
    """Apply the upscale/gray/invert/threshold pipeline described above."""
    cfg = (config or get_config()).ocr

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image

    if cfg.upscale > 1:
        gray = cv2.resize(
            gray,
            None,
            fx=cfg.upscale,
            fy=cfg.upscale,
            interpolation=cv2.INTER_CUBIC,
        )

    # Mild denoise; Tarkov's UI has subtle gradient noise that Otsu will
    # otherwise happily binarise into speckle.
    gray = cv2.bilateralFilter(gray, 5, 40, 40)

    if cfg.invert:
        gray = cv2.bitwise_not(gray)

    _thresh_val, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )

    # Pad — Tesseract's layout analysis is unreliable when glyphs touch the
    # image border.
    return cv2.copyMakeBorder(binary, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


def parse_number(text: str) -> int | None:
    """Extract the first integer from OCR text.

    Strips thousands separators (space, comma, period — Tarkov uses spaces) and
    the rouble sign. Returns None when there's no digit run.
    """
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:  # pragma: no cover - guarded by the regex above
        return None


def parse_quantity(text: str) -> int:
    """Extract a stack count. Defaults to 1 when no count is present."""
    match = _QUANTITY_RE.search(text)
    if match:
        return int(match.group(1) or match.group(2))
    value = parse_number(text)
    return value if value is not None else 1


def image_to_text(
    image: np.ndarray,
    config: Config | None = None,
    *,
    preprocess_image: bool = True,
) -> OCRResult:
    """Run Tesseract on an image and return text + mean word confidence."""
    cfg = config or get_config()
    try:
        import pytesseract  # noqa: PLC0415
        from pytesseract import Output  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise OCRUnavailableError("pytesseract is not installed") from exc

    _configure_tesseract(cfg)
    prepared = preprocess(image, cfg) if preprocess_image else image

    tess_config = f"--psm {cfg.ocr.psm} --oem 3"
    if cfg.ocr.char_whitelist:
        tess_config += f" -c tessedit_char_whitelist={cfg.ocr.char_whitelist}"

    try:
        data = pytesseract.image_to_data(
            prepared, config=tess_config, output_type=Output.DICT
        )
    except Exception as exc:  # pytesseract raises TesseractNotFoundError et al.
        raise OCRUnavailableError(
            f"Tesseract failed ({exc}). Install the tesseract binary and/or set "
            f"[ocr].tesseract_cmd in config.toml."
        ) from exc

    words: list[str] = []
    confidences: list[float] = []
    # strict=False: Tesseract's parallel arrays should be equal length, but a
    # malformed page shouldn't raise out of an OCR read — we'd rather return
    # low confidence and let the caller's threshold reject it.
    for word, conf in zip(data.get("text", []), data.get("conf", []), strict=False):
        word = (word or "").strip()
        try:
            conf_val = float(conf)
        except (TypeError, ValueError):
            continue
        # Tesseract emits -1 for non-text blocks.
        if word and conf_val >= 0:
            words.append(word)
            confidences.append(conf_val)

    text = " ".join(words)
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return OCRResult(text=text, value=parse_number(text), confidence=mean_conf)


class TextReader:
    """Config-aware OCR over named screen regions."""

    def __init__(
        self,
        config: Config | None = None,
        capture: ScreenCapture | None = None,
    ) -> None:
        self.config = config or get_config()
        self.capture = capture or ScreenCapture(self.config)

    def read_region(
        self,
        region: str | Region,
        *,
        min_confidence: float | None = None,
        debug_label: str | None = None,
    ) -> OCRResult:
        """Crop a region and OCR it.

        On low confidence the raw capture is dumped to
        ``data/debug_captures/`` so you can see what Tesseract was given.
        """
        box = self.config.window.region(region) if isinstance(region, str) else region
        floor = (
            min_confidence
            if min_confidence is not None
            else self.config.thresholds.ocr_confidence
        )

        frame = self.capture.grab(box)
        result = image_to_text(frame, self.config)

        label = debug_label or (region if isinstance(region, str) else "region")
        if result.confidence < floor or result.value is None:
            log.warning(
                "Low-confidence OCR on {!r}: text={!r} value={} conf={:.1f} (floor {:.1f})",
                label,
                result.text,
                result.value,
                result.confidence,
                floor,
            )
            self.capture.debug_dump(frame, f"ocr-fail-{label}")
            return OCRResult(text=result.text, value=None, confidence=result.confidence)

        log.debug("OCR {!r}: {} (conf {:.1f})", label, result.value, result.confidence)
        return result

    def read_price(self, region: str | Region = "first_offer_price") -> int | None:
        """Read a rouble price. None when it can't be read confidently."""
        return self.read_region(region, debug_label="price").value

    def read_quantity(self, region: str | Region = "first_offer_quantity") -> int:
        """Read a stack count, defaulting to 1."""
        box = self.config.window.region(region) if isinstance(region, str) else region
        frame = self.capture.grab(box)
        result = image_to_text(frame, self.config)
        if result.confidence < self.config.thresholds.ocr_confidence:
            log.warning(
                "Low-confidence quantity OCR: {!r} conf={:.1f} — assuming 1",
                result.text,
                result.confidence,
            )
            self.capture.debug_dump(frame, "ocr-fail-quantity")
            return 1
        return parse_quantity(result.text)

    def read_balance(self) -> int | None:
        return self.read_region("player_balance", debug_label="balance").value
