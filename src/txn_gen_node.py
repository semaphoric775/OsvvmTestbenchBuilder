"""LLM node: generate initial OSVVM test transactions for each resolved VC.

Produces a VHDL block that replaces the StallProc TODO stub in TbTest_*.vhd.
Only called when --llm is active.
"""

from __future__ import annotations

import sys

from langchain_core.messages import HumanMessage, SystemMessage

from src.llm_factory import get_llm

from src.osvvm_api import get_api, get_scoreboard_pkg
from src.vc_resolver import VcInstance

_SYSTEM = """\
You are an expert OSVVM testbench engineer writing VHDL-2008.
You generate test transaction sequences for OSVVM verification components.

Output format — two sections, in this order:
1. A "-- SHARED DECLARATIONS:" block containing any architecture-level shared
   variable declarations (one per line, ending with " ;").
   If none needed, omit the block entirely.
2. The process body: VHDL statements only, no process/begin/end wrapper.

Rules:
- Output ONLY valid VHDL — no prose, no markdown fences.
- Use only the procedure names listed in the API reference.
- Use the exact signal name provided as the TransactionRec argument.
- Address values as hex literals: X"0000_0000".
- Data values as hex literals sized to match the interface width hint.
- Always call WaitForClock(<Rec>, 2) before the first transaction.
- End the process body with WaitForBarrier(TestDone) on its own line.
- Add a short -- comment before each logical group of transactions.
- Use the provided scoreboard variable for checking (push before send/write,
  check after receive/read). Do NOT use AffirmIfEqual — use the scoreboard.
- Process-local variables (e.g. for Read oData) go between "-- PROCESS VARS:"
  and "-- END PROCESS VARS:" markers, before the first statement.
"""

_USER_TEMPLATE = """\
Generate OSVVM test transactions for the following verification component.

Entity: {entity_name}
VC type: {vc_type}
Role: {role}   (manager drives bus; subordinate receives)
Transaction record signal: {rec_signal}
Scoreboard variable: {sb_var}  (type: {sb_pkg}.ScoreboardPType)
Interface data width hint: {data_width} bits
Interface address width hint: {addr_width} bits

Available API procedures (from actual OSVVM source):
{api}

Output format:
-- SHARED DECLARATIONS:
shared variable {sb_var} : {sb_pkg}.ScoreboardPType ;
-- END SHARED DECLARATIONS
<process body statements>
"""


def _data_width(inst: VcInstance) -> int:
    """Guess data width from port types in the instance."""
    for p in inst.ports:
        t = p.type.lower()
        if "downto" in t:
            try:
                hi = int(t.split("(")[1].split("downto")[0].strip())
                return hi + 1
            except (IndexError, ValueError):
                pass
    return 32


def _addr_width(inst: VcInstance) -> int:
    for p in inst.ports:
        n = p.name.lower()
        if "addr" in n and "downto" in p.type.lower():
            try:
                hi = int(p.type.lower().split("(")[1].split("downto")[0].strip())
                return hi + 1
            except (IndexError, ValueError):
                pass
    return 32


def _parse_block(raw: str) -> tuple[str, str, str]:
    """Split LLM output into (shared_decls, local_vars, process_body).

    Recognises:
      -- SHARED DECLARATIONS: ... -- END SHARED DECLARATIONS
      -- PROCESS VARS: ... -- END PROCESS VARS
    Everything else is treated as process body.
    """
    shared_decls: list[str] = []
    local_vars: list[str] = []
    body: list[str] = []

    in_shared = False
    in_local = False

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- SHARED DECLARATIONS"):
            in_shared = True
            continue
        if stripped.startswith("-- END SHARED DECLARATIONS"):
            in_shared = False
            continue
        if stripped.startswith("-- PROCESS VARS"):
            in_local = True
            continue
        if stripped.startswith("-- END PROCESS VARS"):
            in_local = False
            continue

        if in_shared:
            if stripped and not stripped.startswith("--"):
                shared_decls.append(stripped)
        elif in_local:
            if stripped and not stripped.startswith("--"):
                local_vars.append(stripped)
        else:
            body.append(line)

    return (
        "\n".join(shared_decls),
        "\n".join(local_vars),
        "\n".join(body).strip(),
    )


def generate_transactions(
    instances: list[VcInstance],
    entity_name: str,
    test_plan: str = "",
) -> dict[str, dict]:
    """Return {rec_signal: {"shared_decls": str, "local_vars": str, "body": str}}.

    Plain-signal instances are skipped.
    """
    vc_instances = [i for i in instances if i.spec and i.vc_type != "plain"]
    if not vc_instances:
        return {}

    llm = get_llm(temperature=0.2)
    results: dict[str, dict] = {}

    plan_context = f"\nTest plan to follow:\n{test_plan}\n" if test_plan else ""

    for inst in vc_instances:
        sb_pkg = get_scoreboard_pkg(inst.vc_type)
        sb_var = f"{inst.signal_name}_SB"
        api_text = get_api(inst.vc_type, inst.role)
        prompt = _USER_TEMPLATE.format(
            entity_name=entity_name,
            vc_type=inst.vc_type,
            role=inst.role,
            rec_signal=inst.signal_name,
            sb_var=sb_var,
            sb_pkg=sb_pkg,
            data_width=_data_width(inst),
            addr_width=_addr_width(inst),
            api=api_text,
        ) + plan_context

        try:
            response = llm.invoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=prompt),
            ])
            shared_decls, local_vars, body = _parse_block(response.content.strip())
        except Exception as e:
            print(
                f"warning: transaction generation failed for '{inst.signal_name}': {e}",
                file=sys.stderr,
            )
            shared_decls = ""
            local_vars = ""
            body = f"-- TODO: transactions for {inst.signal_name} ({inst.vc_type} {inst.role})"

        print(
            f"info: generated transactions for '{inst.signal_name}' ({inst.vc_type}/{inst.role})",
            file=sys.stderr,
        )
        results[inst.signal_name] = {
            "shared_decls": shared_decls,
            "local_vars": local_vars,
            "body": body,
        }

    return results
