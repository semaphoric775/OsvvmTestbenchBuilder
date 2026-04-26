"""LLM node: generate a structured test plan before transaction generation.

Produces a concise plan (scenarios + corner cases) that the user can review
and edit before it is fed as context to the transaction generation node.
Only called when --llm is active.
"""

from __future__ import annotations

import sys

from langchain_core.messages import HumanMessage, SystemMessage

from src.llm_factory import get_llm

from src.models import DutModel
from src.vc_resolver import VcResolution

_SYSTEM = """\
You are an expert OSVVM verification engineer.
Given a DUT description and its resolved verification components, produce a
concise test plan for the generated OSVVM testbench.

Format — plain text, no markdown:
1. One sentence describing the DUT and test objective.
2. SCENARIOS: numbered list of 3-5 test scenarios (what to stimulate and check).
3. CORNER CASES: bullet list of 2-3 edge cases worth covering.

Keep it under 20 lines. No code, no VHDL.
"""

_USER_TEMPLATE = """\
DUT entity: {entity_name}
Clocks: {clocks}
Resets: {resets}
Verification components:
{vc_summary}
"""


def generate_plan(dut: DutModel, resolution: VcResolution) -> str:
    """Return a plain-text test plan string, or empty string on failure."""
    vc_lines = []
    for inst in resolution.instances:
        if inst.spec:
            vc_lines.append(f"  - {inst.vc_type} ({inst.role}) signal={inst.signal_name}")
        elif inst.vc_type == "plain":
            ports = ", ".join(p.name for p in inst.ports)
            vc_lines.append(f"  - plain signals: {ports}")

    prompt = _USER_TEMPLATE.format(
        entity_name=dut.entity_name,
        clocks=", ".join(dut.clocks) or "none",
        resets=", ".join(r.name for r in dut.resets) or "none",
        vc_summary="\n".join(vc_lines) or "  (none)",
    )

    llm = get_llm(temperature=0.3)
    try:
        response = llm.invoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=prompt),
        ])
        plan = response.content.strip()
        print("info: test plan generated", file=sys.stderr)
        return plan
    except Exception as e:
        print(f"warning: test plan generation failed: {e}", file=sys.stderr)
        return ""
