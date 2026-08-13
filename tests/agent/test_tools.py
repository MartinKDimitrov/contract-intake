"""The agent's tools, exercised directly.

The tools are the interesting half of stage 05: they are what makes a finding
checkable, and the trace they leave is the evidence that the knowledge base was
consulted at all. All hermetic -- no model involved.
"""

from __future__ import annotations

import json

import pytest

from contract_intake.agent.tools import MAX_CLAUSE_CHARS, Finding, ToolBox, _trim


@pytest.fixture
def toolbox(settings, tmp_path):
    box = ToolBox(settings=settings.model_copy(update={"data_dir": tmp_path}))
    box.settings.ensure_dirs()
    return box


@pytest.fixture
def tools(toolbox):
    """The three tools, keyed by name, unwrapped from the runner decorator."""
    built = {t.name: t for t in toolbox.build()}
    assert set(built) == {"resolve_counterparty", "search_policy", "record_finding"}
    return built


async def call(tools, tool_name: str, /, **kwargs):
    raw = await tools[tool_name].call(kwargs)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# -- resolve_counterparty ---------------------------------------------------


async def test_a_known_supplier_comes_back_with_its_registry_entry(tools, toolbox) -> None:
    result = await call(tools, "resolve_counterparty", name="Nordwind Logistik GmbH")

    assert result["resolved"] is True
    assert result["vendor_id"] == "VEN-0142"
    assert result["category"] == "freight_forwarding"
    assert toolbox.counterparty_id == "VEN-0142"


async def test_the_scanned_name_still_resolves(tools) -> None:
    """The whole point of trigram matching over embeddings."""
    result = await call(tools, "resolve_counterparty", name="NordWind Logistics Ltd.")
    assert result["resolved"] is True
    assert result["vendor_id"] == "VEN-0142"


async def test_an_unknown_supplier_returns_near_misses_not_a_guess(tools, toolbox) -> None:
    result = await call(tools, "resolve_counterparty", name="Totally Unknown Vendor SRL")

    assert result["resolved"] is False
    assert result["reason"]
    assert "near_misses" in result
    assert toolbox.counterparty_id is None, "a failed match must not be recorded as one"


async def test_registry_status_reaches_the_agent(tools) -> None:
    """A suspended supplier must not look like an approved one."""
    result = await call(tools, "resolve_counterparty", name="Levant Shipping Agency SAL")
    assert result["resolved"] is True
    assert result["status"] == "suspended"


async def test_the_registration_number_is_passed_through(tools) -> None:
    result = await call(
        tools, "resolve_counterparty", name="NordWind Logistics Ltd.", registration_id="HRB 84421"
    )
    assert result["matched_on"] == "registration_id"


async def test_a_registration_number_that_contradicts_the_name_resolves_to_nobody(tools) -> None:
    """The agent must see the disagreement rather than a confident wrong answer."""
    result = await call(
        tools,
        "resolve_counterparty",
        name="Levant Shipping Agency SAL",
        registration_id="HRB 84421",
    )
    assert result["matched_on"] == "conflict"
    assert not result["resolved"]


# -- search_policy ----------------------------------------------------------


async def test_a_contract_phrase_returns_clauses_with_their_sections(tools) -> None:
    hits = await call(tools, "search_policy", question="payment terms are 90 days")
    assert hits
    assert all(h["section"].startswith("§") for h in hits)
    assert "§1.1" in {h["section"] for h in hits}


async def test_every_hit_carries_its_rule_not_just_a_title(tools) -> None:
    """The regression that cost a real deviation.

    An earlier version returned the body of the top hit only. For "renews
    automatically", §2.1 scored 0.475 and §2.2 scored 0.474 -- so the agent got
    the wrong clause in full and the right one as a bare title, and missed the
    deviation. A title names the topic; only the body carries the rule.
    """
    hits = await call(
        tools, "search_policy", question="agreement shall renew automatically each year"
    )
    assert len(hits) > 1
    for hit in hits:
        assert hit.get("text"), f"{hit['section']} came back without its rule"


async def test_clause_bodies_are_trimmed_not_dropped(tools) -> None:
    hits = await call(tools, "search_policy", question="liability")
    for hit in hits:
        assert len(hit["text"]) <= MAX_CLAUSE_CHARS + 8  # allow the ellipsis marker


def test_trim_keeps_short_clauses_whole() -> None:
    assert _trim("short rule") == "short rule"


def test_trim_cuts_on_a_word_boundary() -> None:
    trimmed = _trim("word " * 400)
    assert trimmed.endswith("[...]")
    assert "  " not in trimmed


# -- record_finding ---------------------------------------------------------


async def test_a_finding_is_captured_with_its_citation(tools, toolbox) -> None:
    await call(
        tools,
        "record_finding",
        kind="policy_deviation",
        severity="high",
        field_name="governing_law",
        citation="§4.1",
        explanation="Cayman Islands is outside the approved list.",
    )

    assert len(toolbox.findings) == 1
    finding = toolbox.findings[0]
    assert isinstance(finding, Finding)
    assert finding.citation == "§4.1"
    assert finding.to_json()["field"] == "governing_law"


async def test_several_findings_accumulate_in_order(tools, toolbox) -> None:
    for section in ("§1.1", "§2.2", "§3.2"):
        await call(
            tools,
            "record_finding",
            kind="policy_deviation",
            severity="medium",
            field_name="x",
            citation=section,
            explanation="deviates",
        )
    assert [f.citation for f in toolbox.findings] == ["§1.1", "§2.2", "§3.2"]


# -- the trace --------------------------------------------------------------


async def test_every_call_is_traced_with_its_input_and_output(tools, toolbox) -> None:
    await call(tools, "resolve_counterparty", name="Nordwind Logistik GmbH")
    await call(tools, "search_policy", question="automatic renewal")

    assert [t["tool"] for t in toolbox.trace] == ["resolve_counterparty", "search_policy"]
    assert all("input" in t and "output" in t for t in toolbox.trace)
    assert toolbox.trace[0]["input"]["name"] == "Nordwind Logistik GmbH"


async def test_the_trace_shows_whether_the_knowledge_base_was_consulted(tools, toolbox) -> None:
    """A reviewer must be able to see findings that rest on nothing."""
    from contract_intake.agent.runner import AgentOutcome

    await call(
        tools,
        "record_finding",
        kind="policy_deviation",
        severity="high",
        field_name="x",
        citation="§4.1",
        explanation="asserted without looking anything up",
    )
    unsupported = AgentOutcome(
        findings=toolbox.findings,
        trace=toolbox.trace,
        counterparty_id=None,
        counterparty_score=None,
        summary="",
        usd=0.0,
        iterations=1,
    )
    assert not unsupported.used_knowledge_base

    await call(tools, "search_policy", question="governing law")
    supported = AgentOutcome(
        findings=toolbox.findings,
        trace=toolbox.trace,
        counterparty_id=None,
        counterparty_score=None,
        summary="",
        usd=0.0,
        iterations=2,
    )
    assert supported.used_knowledge_base
