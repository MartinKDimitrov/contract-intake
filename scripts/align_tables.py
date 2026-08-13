"""Pad markdown tables so they read as tables without a renderer.

A markdown table is legal with ragged pipes, and unreadable that way anywhere it
is not rendered: a terminal, a diff, `git show`, a code review, an editor with
no preview. Most of the reasoning in this repository lives in those tables, and
most of the places it gets read are the ones without a renderer.

Padding is purely cosmetic to a renderer and load-bearing to a person.

    python scripts/align_tables.py            # rewrite in place
    python scripts/align_tables.py --check    # fail if anything is unaligned

Cells wider than MAX_CELL are left long rather than padded to their own width:
past that point every other column is pushed off the screen and alignment costs
more than it buys.
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Documents whose tables carry reasoning a reader needs.
TARGETS = ("README.md", "docs/*.md", "evals/documents/README.md", "evals/*.md")

#: Beyond this a column is prose, not a column.
MAX_CELL = 96


def width(text: str) -> int:
    """Display width, counting a double-width character as two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def is_divider(cells: list[str]) -> bool:
    return bool(cells) and all(set(c) <= set("-: ") and "-" in c for c in cells)


def split_row(line: str) -> list[str]:
    stripped = line.strip()
    return [c.strip() for c in stripped.strip("|").split("|")]


def align(block: list[str]) -> list[str]:
    rows = [split_row(line) for line in block]
    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]

    widths = [
        min(MAX_CELL, max(width(row[i]) for row in rows if not is_divider(row)))
        for i in range(columns)
    ]

    out: list[str] = []
    for row in rows:
        if is_divider(row):
            out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
            continue
        cells = [f" {c}{' ' * max(0, widths[i] - width(c))} " for i, c in enumerate(row)]
        out.append("|" + "|".join(cells) + "|")
    return out


def rewrite(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    block: list[str] = []

    for line in lines:
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            block.append(line)
            continue
        if block:
            out.extend(align(block) if len(block) >= 2 else block)
            block = []
        out.append(line)

    if block:
        out.extend(align(block) if len(block) >= 2 else block)
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report, do not rewrite")
    args = parser.parse_args()

    unaligned: list[str] = []
    for pattern in TARGETS:
        for path in sorted(ROOT.glob(pattern)):
            original = path.read_text(encoding="utf-8")
            aligned = rewrite(original)
            if aligned == original:
                continue
            unaligned.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.write_text(aligned, encoding="utf-8")

    if args.check and unaligned:
        print("unaligned tables in: " + ", ".join(unaligned), file=sys.stderr)
        print("run: make docs", file=sys.stderr)
        return 1

    if unaligned:
        print(f"aligned tables in {len(unaligned)} file(s): {', '.join(unaligned)}")
    else:
        print("tables already aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
