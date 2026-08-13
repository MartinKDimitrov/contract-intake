"""Align annotated field declarations into columns, and keep the formatter off them.

A dataclass or a model is a small table: a name, a type, sometimes a default.
Read as a table it is scanned in one pass; read as ragged prose every field has
to be parsed individually. The difference matters most in the files a newcomer
opens first -- `db/models.py`, `config.py`, the stage protocols.

`ruff format` will not do this and will undo it, so each aligned run is wrapped
in `# fmt: off` / `# fmt: on`. That is a deliberate, narrow exemption: the rest
of the codebase stays automatically formatted, and only these blocks are hand
shaped.

    python scripts/align_fields.py            # rewrite in place
    python scripts/align_fields.py --check    # fail if anything is unaligned

Idempotent: running it twice changes nothing, which is what lets it be a gate.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (ROOT / "src", ROOT / "evals")

#: `    name: type` or `    name: type = default`, inside a class body.
FIELD = re.compile(r"^(?P<indent>\s+)(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<rest>\S.*)$")

#: Runs shorter than this are not a table and gain nothing from being one.
MIN_RUN = 3

OFF, ON = "# fmt: off", "# fmt: on"

#: Alignment that pushes a line past the project's limit costs more than it
#: buys -- and `ruff check` would reject it anyway. Such a run is left ragged.
MAX_LINE = 100


def split_default(rest: str) -> tuple[str, str]:
    """Separate the annotation from its default, ignoring `=` inside brackets."""
    depth = 0
    for i, char in enumerate(rest):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "=" and depth == 0 and rest[i : i + 2] != "==":
            return rest[:i].strip(), rest[i + 1 :].strip()
    return rest.strip(), ""


def alignable(line: str) -> re.Match[str] | None:
    """A field declaration that is safe to touch: one line, no comment, no call."""
    match = FIELD.match(line)
    if match is None or line.rstrip().endswith((",", "(", "[", "{")):
        return None
    if match["name"].startswith("__"):
        return None
    return match


def align_run(lines: list[str]) -> list[str]:
    parts = []
    for line in lines:
        match = alignable(line)
        assert match is not None
        rest, _, comment = match["rest"].partition("  #")
        annotation, default = split_default(rest.rstrip())
        parts.append((match["indent"], match["name"], annotation, default, comment.strip()))

    name_width = max(len(p[1]) for p in parts)
    type_width = max((len(p[2]) for p in parts if p[3]), default=0)
    body_width = 0

    rendered = []
    for indent, name, annotation, default, _comment in parts:
        if default:
            body = f"{name:<{name_width}} : {annotation:<{type_width}} = {default}"
        else:
            body = f"{name:<{name_width}} : {annotation}"
        rendered.append(body)
        body_width = max(body_width, len(body))

    out = []
    for (indent, *_rest, comment), body in zip(parts, rendered, strict=True):
        line = f"{indent}{body}"
        if comment:
            line = f"{indent}{body:<{body_width}}  # {comment}"
        out.append(line)
    return out


def rewrite(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        aligned = align_run(run) if len(run) >= MIN_RUN else []
        if aligned and max(len(line) for line in aligned) <= MAX_LINE:
            indent = FIELD.match(run[0])["indent"]  # type: ignore[index]
            out.extend([f"{indent}{OFF}", *aligned, f"{indent}{ON}"])
        else:
            out.extend(run)
        run.clear()

    for line in lines:
        if line.strip() in (OFF, ON):
            continue  # rebuilt below, so the pass is idempotent
        if alignable(line):
            run.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    changed: list[str] = []
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            original = path.read_text(encoding="utf-8")
            aligned = rewrite(original)
            if aligned == original:
                continue
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.write_text(aligned, encoding="utf-8")

    if args.check and changed:
        print("field blocks not aligned: " + ", ".join(changed), file=sys.stderr)
        print("run: make fields", file=sys.stderr)
        return 1
    print(f"aligned {len(changed)} file(s)" if changed else "field blocks already aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
