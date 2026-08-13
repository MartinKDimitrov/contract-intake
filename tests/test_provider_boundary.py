"""One module may reach the provider. This is the check that says so.

`llm/client.py` opens with the claim that it is the only path to the API, and
the architecture rests on it: the cost ledger, the per-document budget and the
refusal handling are all enforced there, so a second construction site would be
spending nobody watches.

An import graph cannot express this -- `agent/tools.py` legitimately imports a
decorator from the same package. What matters is who builds a client and who
issues a request, which is a question about call sites.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "contract_intake"

#: The one module allowed to construct a client or issue a request.
GATEWAY = SOURCE / "llm" / "client.py"

#: Attribute chains that mean "this code is talking to Anthropic".
PROVIDER_CALLS = ("AsyncAnthropic", "Anthropic")


def modules() -> list[Path]:
    return sorted(p for p in SOURCE.rglob("*.py") if p != GATEWAY)


@pytest.mark.parametrize("path", modules(), ids=lambda p: str(p.relative_to(SOURCE)))
def test_no_module_but_the_client_constructs_a_provider_client(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in PROVIDER_CALLS)
            or (isinstance(node.func, ast.Attribute) and node.func.attr in PROVIDER_CALLS)
        )
    ]

    assert not built, (
        f"{path.relative_to(SOURCE)} constructs a provider client at line "
        f"{built[0].lineno}. Every call must go through llm/client.py, which is "
        "what writes the cost ledger and enforces the per-document budget."
    )
