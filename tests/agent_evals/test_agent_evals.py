"""
Agent eval test suite — 32 scenarios, deterministic (scripted mock LLM).

Run with:  pytest tests/agent_evals/ -v
Pass rate is reported at the end of the session.
"""

import asyncio

import pytest

from tests.agent_evals.harness import EvalResult, EvalScenario, run_scenario
from tests.agent_evals.scenarios import ALL_SCENARIOS

# ── parametrised test ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
@pytest.mark.asyncio
async def test_scenario(scenario: EvalScenario) -> None:
    result = await run_scenario(scenario)
    assert result.passed, f"[{scenario.name}] FAIL — {result.error}\nReply: {result.reply!r}"


# ── pass-rate report ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pass_rate_report(capsys: pytest.CaptureFixture) -> None:
    """
    Run every scenario and print a pass/fail table + pass rate.

    This test always "passes" from pytest's perspective; the assertion is only
    that the report is generated. Individual failures are caught in test_scenario.
    """
    results: list[EvalResult] = list(
        await asyncio.gather(*[run_scenario(s) for s in ALL_SCENARIOS])
    )

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    rate = passed / total * 100 if total else 0.0

    lines = [
        "",
        "=" * 60,
        f"Agent Eval Pass Rate: {passed}/{total} ({rate:.0f}%)",
        "=" * 60,
    ]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        detail = "" if r.passed else f"  -> {r.error}"
        lines.append(f"  {mark} {r.scenario}{detail}")
    lines.append("=" * 60)

    with capsys.disabled():
        print("\n".join(lines))

    # Fail the test if pass rate drops below 80%
    assert rate >= 80, f"Pass rate {rate:.0f}% is below threshold of 80%"
