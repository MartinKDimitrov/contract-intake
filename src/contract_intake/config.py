"""Process configuration.

Everything tunable lives here so the pipeline stages stay free of magic numbers.
Values come from the environment (see .env.example); nothing is read from the
environment anywhere else in the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CI_",
        extra="ignore",
    )

    # --- LLM ---------------------------------------------------------------
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), alias="ANTHROPIC_API_KEY")
    model: str = "claude-opus-5"
    extract_model: str = ""
    enrich_model: str = ""
    """Per-stage model overrides; empty means fall back to `model`.

    The two stages do work of very different difficulty. Extraction reads
    degraded scans, decides whether a term is absent or merely missed, and
    copies quotes verbatim. Enrichment calls three tools and compares numbers to
    thresholds. Paying the same rate for both is a choice, not a requirement.
    """

    extract_effort: Effort = "medium"
    enrich_effort: Effort = "medium"
    max_usd_per_document: float = 0.75

    # --- Email intake ------------------------------------------------------
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: SecretStr = SecretStr("")
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = 15

    # --- Storage -----------------------------------------------------------
    data_dir: Path = Path("var")
    database_url: str = "sqlite+pysqlite:///var/contract_intake.db"

    # --- Behaviour ---------------------------------------------------------
    min_field_confidence: float = 0.80
    min_vendor_match: float = 0.85
    max_attachment_mb: int = 25

    # --- Cost levers (stage 03) --------------------------------------------
    page_image_max_px: int = 1400
    """Long edge for a page sent to vision.

    Image tokens scale with area, roughly (w*h)/750. At 1568px an A4 page costs
    about 4.6k tokens; at 1400px about 3.7k; at 1000px about 1.9k. Lower is
    cheaper and less legible -- the setting is swept in evals/ rather than
    guessed. This is the single largest knob on the bill.
    """

    min_text_chars_per_page: int = 60
    """Below this a page is treated as having no usable text layer.

    A scanned page often yields a few stray characters from a header stamp or a
    stray vector glyph; that is not a text layer, and trusting it would send the
    model an almost-empty page instead of the image.
    """

    def model_for(self, purpose: str) -> str:
        """The model a given stage should use."""
        override = {"extract": self.extract_model, "enrich": self.enrich_model}.get(purpose, "")
        return override or self.model

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.attachments_dir, self.chroma_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that the .env file is read once. Tests override by calling
    ``get_settings.cache_clear()`` after patching the environment.
    """
    return Settings()
