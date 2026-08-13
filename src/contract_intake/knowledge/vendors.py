"""Counterparty resolution.

Deliberately *not* embeddings. Company names fail in ways that are lexical, not
semantic: a scan reads "NordWind Logistics Ltd." where the registry holds
"Nordwind Logistik GmbH", a subsidiary signs under a trading name, a legal form
changes from EOOD to AD. Token-sorted edit distance handles all of that; a dense
retriever would happily rank "Nordwind Marine Services AS" alongside "Nordwind
Logistik GmbH" because both are Nordic shipping companies -- which is exactly
the mistake that must not be made.

The registry is also small and closed. Embeddings earn their place over a corpus
you cannot enumerate; twenty rows is not that.

Where dense retrieval *does* earn its place is the policy playbook, which is
prose and genuinely semantic. See knowledge/policy.py.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data" / "vendors.json"

#: Legal forms carry no identifying information and differ between a contract
#: and a registry far more often than the name itself does.
LEGAL_FORMS: frozenset[str] = frozenset(
    {
        "gmbh",
        "ag",
        "ltd",
        "limited",
        "plc",
        "llc",
        "inc",
        "corp",
        "sa",
        "sal",
        "bv",
        "nv",
        "as",
        "ab",
        "oy",
        "aps",
        "srl",
        "spa",
        "sp",
        "zoo",
        "kft",
        "zrt",
        "doo",
        "dd",
        "ood",
        "eood",
        "ad",
        "ead",
        "uab",
        "sia",
        "oü",
        "ou",
        "kg",
        "ohg",
        "se",
        "scs",
        "sas",
        "sarl",
        "pte",
        "pty",
    }
)


@dataclass(frozen=True, slots=True)
class Vendor:
    id: str
    legal_name: str
    aliases: tuple[str, ...]
    registration_id: str
    country: str
    category: str
    risk_class: str
    status: str
    notes: str = ""

    @property
    def is_suspended(self) -> bool:
        return self.status != "approved"


@dataclass(frozen=True, slots=True)
class Match:
    vendor: Vendor | None
    score: float
    matched_on: str
    reason: str
    runners_up: tuple[tuple[str, float], ...] = ()

    @property
    def resolved(self) -> bool:
        return self.vendor is not None


def normalise_company(name: str) -> str:
    """Strip everything that varies without changing which company is meant."""
    text = name.casefold()
    text = re.sub(r"[.,;:()\"'&/\\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Single letters are never identifying. They are the debris of a punctuated
    # legal form -- "G.m.b.H." becomes "g m b h", "Sp. z o.o." becomes "sp z o o"
    # -- and keeping them would make an abbreviation look like a different company.
    words = [w for w in text.split() if w not in LEGAL_FORMS and len(w) > 1]
    return " ".join(words) or text


@lru_cache(maxsize=1)
def load_registry(path: Path | None = None) -> tuple[Vendor, ...]:
    raw = json.loads((path or DATA).read_text(encoding="utf-8"))
    return tuple(
        Vendor(
            id=v["id"],
            legal_name=v["legal_name"],
            aliases=tuple(v.get("aliases", ())),
            registration_id=v.get("registration_id", ""),
            country=v.get("country", ""),
            category=v.get("category", ""),
            risk_class=v.get("risk_class", "standard"),
            status=v.get("status", "approved"),
            notes=v.get("notes", ""),
        )
        for v in raw["vendors"]
    )


def name_agreement(name: str, vendor: Vendor) -> float:
    """How well a stated name matches one registry entry, on the same scale as resolve()."""
    labels = [normalise_company(label) for label in (vendor.legal_name, *vendor.aliases)]
    query = normalise_company(name)
    return max((fuzz.token_sort_ratio(query, label) / 100.0 for label in labels), default=0.0)


def resolve(
    name: str | None,
    *,
    registration_id: str | None = None,
    threshold: float = 0.85,
    registry: tuple[Vendor, ...] | None = None,
) -> Match:
    """Resolve a counterparty name to a registry entry.

    The registration number is an identifier rather than a description, so a
    match on it is strong evidence -- but it used to be decisive on its own,
    and that is a hole rather than a shortcut. Any name at all beside another
    supplier's registration number resolved to that supplier, which turned one
    mistyped or borrowed number into a path from a *suspended* counterparty to
    an approved one, silently and at score 1.00.

    So the two have to agree. When they do not, nothing is resolved: the
    disagreement is itself the finding, and §7.2 sends it to a person.
    """
    vendors = registry if registry is not None else load_registry()

    if not name or not name.strip():
        return Match(None, 0.0, "none", "no counterparty name was extracted")

    if registration_id:
        exact = _by_registration(registration_id, vendors)
        if exact is not None:
            agreement = name_agreement(name, exact)
            if agreement >= threshold:
                return Match(
                    exact,
                    1.0,
                    "registration_id",
                    f"registration {registration_id} is unique, and the name agrees "
                    f"({agreement:.2f})",
                )
            return Match(
                None,
                agreement,
                "conflict",
                f"registration {registration_id} belongs to {exact.legal_name!r}, but the "
                f"contract names {name.strip()!r} (agreement {agreement:.2f})",
            )

    candidates: dict[str, Vendor] = {}
    for vendor in vendors:
        for label in (vendor.legal_name, *vendor.aliases):
            candidates[normalise_company(label)] = vendor

    query = normalise_company(name)
    ranked = process.extract(query, list(candidates), scorer=fuzz.token_sort_ratio, limit=3)
    if not ranked:
        return Match(None, 0.0, "none", "registry is empty")

    best_key, best_score, _ = ranked[0]
    score = best_score / 100.0
    runners = tuple((candidates[k].legal_name, s / 100.0) for k, s, _ in ranked[1:])

    if score < threshold:
        near = f"; closest was {candidates[best_key].legal_name!r} at {score:.2f}" if score else ""
        return Match(None, score, "none", f"no registry entry above {threshold:.2f}{near}", runners)

    vendor = candidates[best_key]
    return Match(
        vendor,
        score,
        "name",
        f"matched {vendor.legal_name!r} at {score:.2f}",
        runners,
    )


def _by_registration(registration_id: str, vendors: tuple[Vendor, ...]) -> Vendor | None:
    wanted = re.sub(r"[\s.\-]", "", registration_id).casefold()
    if not wanted:
        return None
    for vendor in vendors:
        if re.sub(r"[\s.\-]", "", vendor.registration_id).casefold() == wanted:
            return vendor
    return None
