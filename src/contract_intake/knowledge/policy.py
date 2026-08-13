"""Policy retrieval over the contracting playbook.

This is where dense retrieval earns its place, and where the whole knowledge
base justifies itself: nothing in a contract states what *this company* accepts.
"Payment terms: 90 days" is not wrong on its face -- it is wrong against §1.1,
and no amount of model reasoning supplies that section.

Chunked by section so a hit carries its own citation. A finding that says
"deviates from §4.1" can be checked by a human in seconds; one that says
"unusual jurisdiction" cannot.

Chroma with its bundled ONNX embedder: file-based, no server to run, and no
PyTorch in the dependency tree.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data" / "playbook.md"
COLLECTION = "policy"


@dataclass(frozen=True, slots=True)
class Clause:
    section: str
    title: str
    body: str

    @property
    def citation(self) -> str:
        return f"{self.section} {self.title}"

    @property
    def text(self) -> str:
        return f"{self.section} {self.title}\n{self.body}"


@dataclass(frozen=True, slots=True)
class PolicyHit:
    clause: Clause
    score: float


def parse_playbook(path: Path | None = None) -> list[Clause]:
    """Split the playbook into one chunk per numbered section."""
    text = (path or DATA).read_text(encoding="utf-8")
    pattern = re.compile(r"^##\s+(§[\d.]+)\s+(.+)$", re.MULTILINE)

    matches = list(pattern.finditer(text))
    clauses: list[Clause] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].replace("---", "").strip()
        clauses.append(Clause(section=match.group(1), title=match.group(2).strip(), body=body))
    return clauses


class PolicyIndex:
    """Dense retrieval over playbook sections."""

    def __init__(self, persist_dir: Path) -> None:
        self._persist_dir = persist_dir
        self._collection: Any | None = None

    def _get(self) -> Any:
        if self._collection is not None:
            return self._collection

        import chromadb

        client = chromadb.PersistentClient(path=str(self._persist_dir))
        self._collection = client.get_or_create_collection(COLLECTION)
        return self._collection

    def build(self, clauses: list[Clause] | None = None) -> int:
        """(Re)build the index. Idempotent -- safe to run on every start."""
        clauses = clauses if clauses is not None else parse_playbook()
        collection = self._get()

        existing = collection.get(include=[])
        if existing["ids"]:
            collection.delete(ids=existing["ids"])

        collection.add(
            ids=[c.section for c in clauses],
            documents=[c.text for c in clauses],
            metadatas=[{"section": c.section, "title": c.title} for c in clauses],
        )
        log.info("policy index built: %d clause(s)", len(clauses))
        return len(clauses)

    def search(self, query: str, *, k: int = 3) -> list[PolicyHit]:
        collection = self._get()
        if collection.count() == 0:
            self.build()

        result = collection.query(query_texts=[query], n_results=k)
        hits: list[PolicyHit] = []
        by_section = {c.section: c for c in parse_playbook()}

        for section, distance in zip(result["ids"][0], result["distances"][0], strict=False):
            clause = by_section.get(section)
            if clause is None:
                continue
            # Chroma returns squared L2 distance; smaller is closer.
            hits.append(PolicyHit(clause=clause, score=1.0 / (1.0 + distance)))
        return hits


@lru_cache(maxsize=4)
def get_index(persist_dir: Path) -> PolicyIndex:
    return PolicyIndex(persist_dir)
