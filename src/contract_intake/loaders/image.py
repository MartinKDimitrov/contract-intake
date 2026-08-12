"""Image normalisation for pages that must be read by the model.

Image tokens scale with area -- roughly ``(width * height) / 750`` -- so the
long edge is the price dial. An A4 page costs about 4.6k tokens at 1568px and
about 1.9k at 1000px. Legibility falls with it, which is why the setting lives
in config and is swept in evals/ rather than picked here.

Greyscale, not colour: contract pages carry no information in hue, and dropping
it makes the PNG markedly smaller without changing what the model reads.
"""

from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image, ImageOps


def normalise_image(source: Path, target: Path, *, max_px: int) -> tuple[int, int]:
    """Downsample, deskew orientation and write a PNG. Returns (width, height)."""
    with Image.open(source) as opened:
        page: Image.Image = ImageOps.exif_transpose(opened) or opened
        page = page.convert("L")

        longest = max(page.size)
        if longest > max_px:
            scale = max_px / longest
            page = page.resize(
                (max(1, round(page.width * scale)), max(1, round(page.height * scale))),
                Image.Resampling.LANCZOS,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        page.save(target, format="PNG", optimize=True)
        return page.width, page.height


def encode_page_image(path: Path) -> str:
    """Base64 for an Anthropic image content block."""
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


def estimate_image_tokens(width: int, height: int) -> int:
    return int(width * height / 750)
