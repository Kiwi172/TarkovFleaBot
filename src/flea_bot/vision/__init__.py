"""Screen capture, template matching, and OCR."""

from flea_bot.vision.capture import ScreenCapture
from flea_bot.vision.ocr import (
    OCRResult,
    OCRUnavailableError,
    TextReader,
    image_to_text,
    parse_number,
    parse_quantity,
    preprocess,
)
from flea_bot.vision.template import (
    Match,
    TemplateMatcher,
    TemplateNotFoundError,
    match_template,
    match_template_all,
)

__all__ = [
    "Match",
    "OCRResult",
    "OCRUnavailableError",
    "ScreenCapture",
    "TemplateMatcher",
    "TemplateNotFoundError",
    "TextReader",
    "image_to_text",
    "match_template",
    "match_template_all",
    "parse_number",
    "parse_quantity",
    "preprocess",
]
