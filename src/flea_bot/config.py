"""Typed configuration loaded from ``config/config.toml``.

Every tunable in the project lives here: game window geometry, screen regions,
vision/OCR thresholds, rate limits, and profitability filters. Nothing else in
the codebase should hardcode a coordinate or a magic number.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

# Repo root: src/flea_bot/config.py -> src/flea_bot -> src -> <root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.toml"

# A screen region as (left, top, width, height) in absolute screen pixels.
Region = tuple[int, int, int, int]


class GeneralConfig(BaseModel):
    dry_run: bool = True
    data_dir: Path = Path("data")


class PricesConfig(BaseModel):
    # auto | spt | spt-mirror | tarkov.dev  (see flea_bot.scraper.base)
    source: str = "auto"
    # Path to your SPT folder (the one containing SPT_Data/). Empty = autodetect.
    spt_install_path: str = ""
    spt_mirror_url: str = (
        "https://raw.githubusercontent.com/sp-tarkov/server/master/project/assets/database"
    )
    api_url: str = "https://api.tarkov.dev/graphql"
    page_size: int = Field(default=200, ge=1, le=1000)
    max_pages: int | None = 50
    min_request_interval: float = Field(default=1.0, ge=0.0)
    request_timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=4, ge=0)
    backoff_factor: float = Field(default=0.75, ge=0.0)


class TradersConfig(BaseModel):
    reference_file: Path = Path("config/trader_prices.yaml")
    fall_back_to_api: bool = True


class ThresholdsConfig(BaseModel):
    min_margin: int = 5000
    min_margin_ratio: float = Field(default=0.15, ge=0.0)
    min_flea_price: int = Field(default=1000, ge=0)
    max_flea_price: int = Field(default=500_000, gt=0)
    top_n: int = Field(default=25, ge=1)
    max_volatility: float | None = 0.35
    history_window_hours: int = Field(default=24, ge=1)
    template_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    ocr_confidence: float = Field(default=60.0, ge=0.0, le=100.0)

    @field_validator("max_flea_price")
    @classmethod
    def _max_above_min(cls, v: int, info: Any) -> int:
        min_price = info.data.get("min_flea_price")
        if min_price is not None and v <= min_price:
            raise ValueError("max_flea_price must be greater than min_flea_price")
        return v


class InputConfig(BaseModel):
    min_action_delay: float = Field(default=0.08, ge=0.0)
    max_action_delay: float = Field(default=0.25, ge=0.0)
    post_click_delay: float = Field(default=0.15, ge=0.0)
    min_move_duration: float = Field(default=0.12, ge=0.0)
    max_move_duration: float = Field(default=0.40, ge=0.0)
    click_jitter_px: int = Field(default=3, ge=0)

    @field_validator("max_action_delay", "max_move_duration")
    @classmethod
    def _max_above_min(cls, v: float, info: Any) -> float:
        counterpart = "min_" + info.field_name.removeprefix("max_")
        low = info.data.get(counterpart)
        if low is not None and v < low:
            raise ValueError(f"{info.field_name} must be >= {counterpart}")
        return v


class WindowConfig(BaseModel):
    left: int = 0
    top: int = 0
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    monitor: int = Field(default=1, ge=0)
    regions: dict[str, Region] = Field(default_factory=dict)
    templates: dict[str, Path] = Field(default_factory=dict)

    @field_validator("regions", mode="before")
    @classmethod
    def _coerce_regions(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        out = {}
        for name, box in v.items():
            if len(box) != 4:
                raise ValueError(f"region {name!r} must be [left, top, width, height]")
            left, top, width, height = (int(n) for n in box)
            if width <= 0 or height <= 0:
                raise ValueError(f"region {name!r} has non-positive width/height")
            out[name] = (left, top, width, height)
        return out

    def region(self, name: str) -> Region:
        """Look up a named region, with a helpful error if it's missing."""
        try:
            return self.regions[name]
        except KeyError:
            known = ", ".join(sorted(self.regions)) or "<none>"
            raise KeyError(
                f"No region {name!r} in [window.regions]. Defined regions: {known}"
            ) from None

    def template(self, name: str) -> Path:
        try:
            path = self.templates[name]
        except KeyError:
            known = ", ".join(sorted(self.templates)) or "<none>"
            raise KeyError(
                f"No template {name!r} in [window.templates]. Defined: {known}"
            ) from None
        return path if path.is_absolute() else PROJECT_ROOT / path


class OCRConfig(BaseModel):
    tesseract_cmd: str | None = None
    upscale: int = Field(default=3, ge=1, le=10)
    invert: bool = True
    char_whitelist: str = "0123456789.,₽ "
    psm: int = Field(default=7, ge=0, le=13)


class SafetyConfig(BaseModel):
    pause_hotkey: str = "f9"
    kill_hotkey: str = "f10"
    max_actions_per_run: int = Field(default=500, ge=1)
    max_consecutive_failures: int = Field(default=5, ge=1)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "flea-bot.log"
    rotation: str = "10 MB"
    retention: str = "14 days"
    json_sink: bool = True


class ApiKeysConfig(BaseModel):
    tarkov_market: str = ""


class Config(BaseModel):
    """Root config object. Access via :func:`load_config`."""

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    prices: PricesConfig = Field(default_factory=PricesConfig)
    traders: TradersConfig = Field(default_factory=TradersConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    input: InputConfig = Field(default_factory=InputConfig)
    window: WindowConfig = Field(default_factory=WindowConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    api_keys: ApiKeysConfig = Field(default_factory=ApiKeysConfig)

    # Where this config was loaded from; None if defaults were used.
    source_path: Path | None = None

    @property
    def data_dir(self) -> Path:
        """Absolute data directory, created on first access."""
        d = self.general.data_dir
        d = d if d.is_absolute() else PROJECT_ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "prices.db"

    @property
    def log_dir(self) -> Path:
        d = self.data_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def trader_reference_path(self) -> Path:
        p = self.traders.reference_file
        return p if p.is_absolute() else PROJECT_ROOT / p


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(path: Path | str | None = None) -> Config:
    """Load configuration.

    Resolution order: explicit ``path`` -> ``$FLEA_BOT_CONFIG`` ->
    ``config/config.toml`` -> ``config/config.example.toml``.

    Falling back to the example file means a fresh clone runs (in dry-run) with
    placeholder coordinates rather than crashing on import.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    elif env := os.environ.get("FLEA_BOT_CONFIG"):
        candidates.append(Path(env))
    else:
        candidates.extend([DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH])

    for candidate in candidates:
        if candidate.is_file():
            data = _read_toml(candidate)
            cfg = Config.model_validate(data)
            cfg.source_path = candidate
            return cfg

    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"No config file found (tried: {tried}). "
        f"Copy {EXAMPLE_CONFIG_PATH.name} to config/config.toml to get started."
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide cached config. Call :func:`load_config` for a fresh read."""
    return load_config()


def reset_config_cache() -> None:
    """Drop the cached config — used by tests and by the CLI's --config flag."""
    get_config.cache_clear()
