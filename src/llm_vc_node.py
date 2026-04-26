"""LLM node for resolving ambiguous VC port groups.

Called only when --llm is set and the rule-based resolver produced ambiguous groups.
Uses Claude Haiku via structured output to pick a VC type from the known spec list,
or return "plain" if no match is appropriate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

from src.llm_factory import get_llm
from pydantic import BaseModel

from src.vc_resolver import AmbiguousGroup, VcInstance, VcSpec, _VC_SPECS, _infer_role

_SPECS_JSON = (Path(__file__).parent / "vc_specs.json").read_text()


class _LlmVcDecision(BaseModel):
    vc_type: str          # one of the vc_type values from vc_specs.json, or "plain"
    role: Literal["manager", "subordinate", "signal"]
    confidence: Literal["high", "medium", "low"]
    reasoning: str        # short explanation for the warning message


def _build_prompt(group: AmbiguousGroup) -> str:
    port_lines = "\n".join(
        f"  {p.name} : {p.direction.value} {p.type}"
        for p in group.ports
    )
    return f"""\
You are a VHDL verification engineer. Given a group of DUT ports that share the \
prefix "{group.prefix}", decide which OSVVM Verification Component (VC) type they \
represent, or "plain" if none fits.

Known VC specs (from vc_specs.json):
{_SPECS_JSON}

Port group (prefix="{group.prefix}"):
{port_lines}

Closest rule-based match: {group.closest_spec or "none"}
Missing required signals for that match: {", ".join(group.missing) or "n/a"}

Instructions:
- Pick a vc_type from the spec list only if you are confident the ports implement \
that protocol, even with the missing signals (e.g. a cut-down interface).
- Pick "plain" if you are not confident or if this is clearly not a standard protocol.
- role must be "manager" if the DUT drives the bus (most outputs), \
"subordinate" if the DUT receives it (most inputs), or "signal" for plain.
- Be conservative: when in doubt, choose "plain".
"""


def resolve_ambiguous_groups(groups: list[AmbiguousGroup]) -> list[VcInstance]:
    """Call the LLM for each ambiguous group and return resolved VcInstances."""
    if not groups:
        return []

    llm = get_llm(temperature=0)
    structured = llm.with_structured_output(_LlmVcDecision)

    results: list[VcInstance] = []
    spec_map: dict[str, VcSpec] = {s.vc_type: s for s in _VC_SPECS}

    for group in groups:
        prompt = _build_prompt(group)
        try:
            decision: _LlmVcDecision = structured.invoke(prompt)
        except Exception as e:
            print(
                f"warning: LLM call failed for prefix '{group.prefix}': {e}. "
                f"Falling back to plain signals.",
                file=sys.stderr,
            )
            continue

        print(
            f"info: LLM resolved '{group.prefix}' → {decision.vc_type} "
            f"({decision.role}, confidence={decision.confidence}): {decision.reasoning}",
            file=sys.stderr,
        )

        if decision.vc_type == "plain" or decision.vc_type not in spec_map:
            results.append(VcInstance(
                vc_type="plain",
                prefix="",
                role="signal",
                ports=group.ports,
                spec=None,
                llm_inferred=True,
            ))
            continue

        spec = spec_map[decision.vc_type]
        suffix_map = {p.name[len(group.prefix):].lower(): p for p in group.ports}
        present_required = [suffix_map[s] for s in spec.required if s in suffix_map]
        missing_required = [s for s in spec.required if s not in suffix_map]

        results.append(VcInstance(
            vc_type=decision.vc_type,
            prefix=group.prefix,
            role=decision.role,
            ports=present_required,
            spec=spec,
            missing=missing_required,
            llm_inferred=True,
        ))

    return results
