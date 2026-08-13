"""Measure the system rather than describe it.

Three numbers, in order of how much they matter:

1. **Field accuracy.** Does extraction read what the document says, and does it
   return null where the document is silent rather than something plausible?

2. **Knowledge-base contribution.** The same agent, on the same contract, with
   the playbook and registry available and then without. The gap is what
   retrieval is worth; if it is zero, the knowledge base is decoration.

3. **Routing.** Does a compliant contract auto-approve, and does everything else
   reach a human?

Costs real money, so it is a script rather than a test. `make eval`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contract_intake.agent.runner import build_review_request
from contract_intake.agent.tools import ToolBox
from contract_intake.config import Settings, get_settings
from contract_intake.db.engine import init_db, session_scope
from contract_intake.extract.extractor import extract
from contract_intake.knowledge.vendors import resolve
from contract_intake.llm.client import LLMClient
from contract_intake.loaders.document import load
from contract_intake.policy.thresholds import evaluate

ROOT = Path(__file__).parent
DOCUMENTS = ROOT / "documents"
RENDERED = DOCUMENTS / "rendered"
EXPECTED = ROOT / "expected"

NO_KB_PROMPT = """\
You review vendor contracts. The commercial terms are given to you as structured \
fields. Identify anything a contracts team should look at before signing.

You have no access to the company's playbook or its vendor registry. Work from \
the contract alone. Record each concern with `record_finding`, citing what you \
based it on.\
"""


@dataclass
class FieldResult:
    # fmt: off
    name       : str
    expected   : Any
    actual     : Any
    correct    : bool
    provenance : str
    # fmt: on


@dataclass
class DocResult:
    # fmt: off
    name                  : str
    fields                : list[FieldResult] = field(default_factory=list)
    with_kb               : set[str]          = field(default_factory=set)
    without_kb            : set[str]          = field(default_factory=set)
    expected_deviations   : set[str]          = field(default_factory=set)
    counterparty          : str | None        = None
    expected_counterparty : str | None        = None
    usd                   : float             = 0.0
    # fmt: on

    @property
    def accuracy(self) -> float:
        return sum(f.correct for f in self.fields) / len(self.fields) if self.fields else 1.0


def matches(expected: Any, actual: Any) -> bool:
    """Compare leniently on text, exactly on numbers and booleans."""
    if expected is None:
        return actual is None
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(expected) - float(actual)) < 0.01
    if actual is None:
        return False
    return str(expected).casefold() in str(actual).casefold()


async def measure(name: str, *, llm: LLMClient, settings: Settings) -> DocResult:
    spec = json.loads((EXPECTED / f"{name}.json").read_text())
    result = DocResult(
        name=name,
        expected_deviations=set(spec.get("deviations", [])),
        expected_counterparty=spec.get("counterparty_id"),
    )

    document = load(
        RENDERED / f"{name}.pdf", settings=settings, into=settings.data_dir / "eval" / name
    )
    outcome = await extract(document, llm=llm, settings=settings)
    result.usd += outcome.usd

    fields = outcome.to_json()
    provenance = {p["field"]: p["status"] for p in fields.get("_provenance", [])}

    for key, want in spec.get("fields", {}).items():
        entry = fields.get(key) or {}
        got = entry.get("value") if isinstance(entry, dict) else None
        result.fields.append(
            FieldResult(key, want, got, matches(want, got), provenance.get(key, "-"))
        )

    # Resolve first, exactly as the pipeline does -- the category decides whether
    # the data-protection check applies at all.
    def value(key: str) -> str | None:
        entry = fields.get(key)
        raw = entry.get("value") if isinstance(entry, dict) else None
        return str(raw) if raw is not None else None

    match = resolve(
        value("counterparty_name"),
        registration_id=value("counterparty_registration_id"),
        threshold=settings.min_vendor_match,
    )
    result.counterparty = match.vendor.id if match.vendor else None

    rules = evaluate(fields, vendor_category=match.vendor.category if match.vendor else None)
    result.with_kb = {f["citation"] for f in rules}
    if not rules:
        box = ToolBox(settings=settings)
        run = await llm.run_agent(
            purpose="enrich",
            tools=box.build(),
            messages=[{"role": "user", "content": build_review_request(fields)}],
            effort=settings.enrich_effort,
            max_iterations=10,
        )
        result.usd += run.usd
        result.with_kb |= {f.citation for f in box.findings}

    # The ablation: the same model, the same contract, no playbook and no registry.
    blind = ToolBox(settings=settings)
    tools = [t for t in blind.build() if t.name == "record_finding"]
    run = await llm.run_agent(
        purpose="enrich_no_kb",
        tools=tools,
        system=[{"type": "text", "text": NO_KB_PROMPT}],
        messages=[{"role": "user", "content": build_review_request(fields)}],
        effort=settings.enrich_effort,
        max_iterations=10,
    )
    result.usd += run.usd
    result.without_kb = {f.citation for f in blind.findings}

    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="run a single fixture by name")
    parser.add_argument(
        "--triage",
        action="store_true",
        help="classify every fixture and every real corpus document, free of charge",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="measure extraction accuracy and cost at every effort level",
    )
    args = parser.parse_args()

    settings = get_settings()
    init_db(settings)

    if args.triage:
        return _triage_report()
    if args.sweep:
        return await _sweep(settings, only=args.only)

    names = sorted(p.stem for p in EXPECTED.glob("*.json"))
    if args.only:
        names = [n for n in names if args.only in n]
    names = [n for n in names if (RENDERED / f"{n}.pdf").exists()]

    results: list[DocResult] = []
    with session_scope() as session:
        llm = LLMClient(session, settings)
        for name in names:
            spec = json.loads((EXPECTED / f"{name}.json").read_text())
            if not spec.get("fields"):
                continue  # turned away before a model is involved
            print(f"  running {name} ...", flush=True)
            results.append(await measure(name, llm=llm, settings=settings))
            session.commit()

    _report(results)
    return 0


COLLECTED = DOCUMENTS / "collected"

#: The documents that are contracts. Everything else must be turned away.
CONTRACTS = frozenset(
    {
        "01-clean-known-vendor",
        "03-policy-deviations",
        "10-addendum-to-msa",
        "11-lease-bilingual-de-en",
        "16-sla-agreement",
        "17-mutual-nda",
        "20-addendum-baltic",
        "21-lease-alpine-de-en",
        "38-lease-swiss-de-en",
        "39-addendum-danube",
        "40-contrato-servicios-es",
        "41-contrat-prestations-fr",
    }
)


def _provenance() -> dict[str, str]:
    """Map each rendered document back to the folder it came from.

    Where a document came from decides what a result on it is worth, so the
    report says it rather than averaging four provenances into one number.
    """
    where: dict[str, str] = {}
    for path in DOCUMENTS.rglob("*.txt"):
        folder = path.relative_to(DOCUMENTS).parent.as_posix()
        where[path.stem.removesuffix(".scan")] = folder
    return where


def _triage_report() -> int:
    """Classify everything, spending nothing.

    Documents with opposite jobs. The authored and generated ones carry
    contracts that must get through and lookalikes that must not. The collected
    corpus is entirely negative -- real EU procurement notices in five
    languages, none of them a contract -- and it is where a false positive would
    be expensive, since it buys an extraction on a document that could never
    produce a contract record.
    """
    from collections import Counter

    from contract_intake.loaders.pdf import probe
    from contract_intake.pipeline.stage_02_triage import classify_text

    print("=" * 78)
    print("TRIAGE  (zero tokens)")
    print("=" * 78)

    where = _provenance()
    wrong: list[str] = []
    scans = 0
    seen: Counter[str] = Counter()
    hits: Counter[str] = Counter()
    for path in sorted(RENDERED.glob("*.pdf")):
        result = probe(path)
        if not result.has_text_layer:
            scans += 1
            continue
        folder = where.get(path.stem, "unknown")
        seen[folder] += 1
        verdict = classify_text(result.first_page_text)
        expected = path.stem in CONTRACTS
        if expected != (verdict.kind == "contract"):
            wrong.append(f"{path.stem} -> {verdict.kind}")
        else:
            hits[folder] += 1
        if expected:
            hits["#contracts"] += 1

    print("\nauthored and generated -- contracts that must pass, lookalikes that must not")
    for folder in sorted(seen):
        print(f"  {folder + ':':<22} {hits[folder]:>3}/{seen[folder]:<3} correct")
    kept = sum(seen.values())
    print(
        f"  {'total:':<22} {kept - len(wrong):>3}/{kept:<3} correct"
        + (f"   wrong: {wrong}" if wrong else "")
    )
    if scans:
        print(f"  ({scans} scanned, no text layer -- classified by stage 04 instead)")

    if not COLLECTED.exists() or not any(COLLECTED.glob("*.pdf")):
        print("\nno real corpus downloaded; run make corpus")
        return 1 if wrong else 0

    print("\ncollected -- real EU procurement notices, none of them contracts")
    by_language: dict[str, Counter[str]] = {}
    false_positives: list[str] = []
    for path in sorted(COLLECTED.glob("*.pdf")):
        language = path.stem.split("-")[1]
        result = probe(path)
        verdict = classify_text(result.first_page_text)
        counter = by_language.setdefault(language, Counter())
        counter[verdict.kind] += 1
        counter["pages"] += result.page_count
        if verdict.kind == "contract":
            false_positives.append(path.stem)

    total = 0
    for language, counter in sorted(by_language.items()):
        n = counter["unknown"] + counter["invoice"] + counter["contract"]
        total += n
        print(
            f"  {language}: {n:>3} documents, {counter['pages']:>4} pages"
            f"  ->  turned away {n - counter['contract']}/{n}"
        )
    print(f"  {total - len(false_positives)}/{total} correct")
    if false_positives:
        print(f"  false positives (each would buy an extraction): {false_positives}")

    return 1 if wrong or false_positives else 0


async def _sweep(settings: Settings, *, only: str | None) -> int:
    """Accuracy against cost, per effort level.

    The point is to find the cheapest setting that still reads the documents
    correctly, rather than assuming the most expensive one is required.
    """
    from sqlalchemy import desc, select

    from contract_intake.db.models import LLMCall

    names = sorted(p.stem for p in EXPECTED.glob("*.json"))
    names = [n for n in names if (RENDERED / f"{n}.pdf").exists()]
    if only:
        names = [n for n in names if only in n]

    print(f"{'effort':<9}{'correct':>9}{'accuracy':>10}{'USD':>10}{'sec/doc':>9}")
    with session_scope() as session:
        llm = LLMClient(session, settings)
        for effort in ("low", "medium", "high"):
            correct = total = 0
            usd = 0.0
            seconds = 0.0
            for name in names:
                spec = json.loads((EXPECTED / f"{name}.json").read_text())
                if not spec.get("fields"):
                    continue
                document = load(
                    RENDERED / f"{name}.pdf",
                    settings=settings,
                    into=settings.data_dir / "eval" / name,
                )
                outcome = await extract(
                    document,
                    llm=llm,
                    settings=settings,
                    effort=effort,  # type: ignore[arg-type]
                )
                session.commit()
                call = session.scalars(select(LLMCall).order_by(desc(LLMCall.id)).limit(1)).one()
                usd += call.usd
                seconds += call.latency_ms / 1000

                fields = outcome.to_json()
                for key, want in spec["fields"].items():
                    entry = fields.get(key) or {}
                    got = entry.get("value") if isinstance(entry, dict) else None
                    total += 1
                    correct += matches(want, got)
            print(
                f"{effort:<9}{f'{correct}/{total}':>9}{correct / total:>10.1%}"
                f"{usd:>10.4f}{seconds / max(1, len(names)):>9.1f}"
            )
    return 0


def _report(results: list[DocResult]) -> None:
    print("\n" + "=" * 78)
    print("FIELD ACCURACY")
    print("=" * 78)
    total = correct = 0
    for r in results:
        print(
            f"\n{r.name}   {r.accuracy:.0%}  ({sum(f.correct for f in r.fields)}/{len(r.fields)})"
        )
        for f in r.fields:
            if f.correct:
                continue
            print(f"    MISS {f.name:<28} want {f.expected!r:<28} got {f.actual!r}")
        total += len(r.fields)
        correct += sum(f.correct for f in r.fields)
    print(f"\noverall  {correct}/{total} = {correct / total:.1%}" if total else "")

    print("\n" + "=" * 78)
    print("KNOWLEDGE-BASE CONTRIBUTION")
    print("=" * 78)
    print("The same model on the same contract, with the playbook and registry, then")
    print("without. Recall is against the deviations the fixture actually contains.\n")
    print(f"{'document':<28}{'expected':>9}{'with KB':>9}{'blind':>7}{'recall':>9}{'blind':>8}")
    for r in results:
        want = r.expected_deviations
        if not want:
            print(f"{r.name:<28}{0:>9}{len(r.with_kb):>9}{len(r.without_kb):>7}{'-':>9}{'-':>8}")
            continue
        with_recall = len(want & r.with_kb) / len(want)
        blind_recall = len(want & r.without_kb) / len(want)
        print(
            f"{r.name:<28}{len(want):>9}{len(r.with_kb):>9}{len(r.without_kb):>7}"
            f"{with_recall:>9.0%}{blind_recall:>8.0%}"
        )
        missed = want - r.without_kb
        if missed:
            print(f"{'':28}blind missed: {', '.join(sorted(missed))}")

    print("\n" + "=" * 78)
    print("COUNTERPARTY RESOLUTION")
    print("=" * 78)
    for r in results:
        mark = "ok " if r.counterparty == r.expected_counterparty else "MISS"
        print(f"{mark} {r.name:<28} want {r.expected_counterparty}  got {r.counterparty}")

    print(f"\ntotal spent: ${sum(r.usd for r in results):.4f}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
