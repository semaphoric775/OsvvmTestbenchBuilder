"""LLM node: generate initial OSVVM test transactions for each resolved VC.

Produces a VHDL block that replaces the StallProc TODO stub in TbTest_*.vhd.
Only called when --llm is active.
"""

from __future__ import annotations

import re
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
- Use ONLY the procedure names listed in the API reference. Do not invent others.
- NEVER assign to the transaction record signal directly (no <= on the record).
  The record is an OSVVM opaque type — only procedures can interact with it.
- NEVER assign to any signal other than through the transaction API procedures.
  Do NOT drive clk, rst, or any DUT port directly. Reset is managed externally
  by OSVVM CreateReset infrastructure and must not be touched in the test process.
- Use the exact signal name provided as the TransactionRec argument.
- Data hex literals MUST use exactly the digit count shown in the user prompt.
  Passing a 32-bit literal to an 8-bit interface crashes at runtime.
- NEVER call to_stdlogicvector — it does not exist in VHDL. Use std_logic_vector(to_unsigned(val, width)) if a conversion is needed; prefer hex literals instead.
- Scoreboard Push/Check only accept std_logic_vector. NEVER pass integer, natural, or boolean variables to the scoreboard.
- GetTransactionCount and GetErrorCount return integers for diagnostic use only; do not feed their output to the scoreboard.
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
Testbench VC role: {role}   (manager/transmitter drives the DUT; subordinate/receiver accepts from the DUT)
Transaction record signal: {rec_signal}
Scoreboard variable: {sb_var}  (type: {sb_pkg}.ScoreboardPType)
Interface data width: {data_width} bits  → use {data_hex_digits}-digit hex literals, e.g. X"{data_example}"
Interface address width: {addr_width} bits  → use {addr_hex_digits}-digit hex literals, e.g. X"{addr_example}"

Available API procedures (from actual OSVVM source):
{api}

Output format:
-- SHARED DECLARATIONS:
shared variable {sb_var} : {sb_pkg}.ScoreboardPType ;
-- END SHARED DECLARATIONS
<process body statements>
"""


def _resolve_width(type_str: str, generics: list) -> int | None:
    """Parse a bit-width from a type string after substituting generic defaults."""
    from src.renderer import _substitute_generics
    t = _substitute_generics(type_str, generics)
    m = re.search(r'\((.+?)downto', t, re.IGNORECASE)
    if not m:
        return None
    expr = m.group(1).strip()
    # "N-1" → N
    nm = re.match(r'^(\d+)\s*-\s*1$', expr)
    if nm:
        return int(nm.group(1))
    try:
        return int(expr) + 1
    except ValueError:
        return None


def _data_width(inst: VcInstance, generics: list) -> int:
    for p in inst.ports:
        w = _resolve_width(p.type, generics)
        if w is not None:
            return w
    return 32


def _addr_width(inst: VcInstance, generics: list) -> int:
    for p in inst.ports:
        if "addr" in p.name.lower():
            w = _resolve_width(p.type, generics)
            if w is not None:
                return w
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


_HEX_LIT = re.compile(r'X"([0-9A-Fa-f_]+)"')
_RST_ASSIGN = re.compile(r'^\s*\w+\s*<=\s*[\'"][01UX-][\'"].*$')
# to_stdlogicvector(expr) → std_logic_vector(expr)
_TO_SLV = re.compile(r'\bto_stdlogicvector\s*\(', re.IGNORECASE)
# scoreboard.Check(non-slv) — integer/natural vars passed to Check are wrong
_SB_CHECK_INT = re.compile(r'(\w+_SB\s*\.\s*Check\s*\(\s*)(\w+)(\s*\))', re.IGNORECASE)


def _sanitize(body: str, data_hex_digits: int) -> str:
    """Fix common LLM mistakes in generated VHDL:

    1. Wrong-width hex literals — truncate to data_hex_digits significant digits.
    2. Direct signal assignments (rst <= ...) — strip them.
    3. to_stdlogicvector → std_logic_vector (hallucinated function name).
    4. Scoreboard.Check(integer_var) — integer vars can't go in slv scoreboards;
       replace with a TODO comment so the user sees it.
    """
    def fix_hex(m: re.Match) -> str:
        raw = m.group(1).replace("_", "")
        raw = raw[-data_hex_digits:].upper()
        if len(raw) > 4:
            parts = [raw[max(0, i-4):i] for i in range(len(raw), 0, -4)][::-1]
            raw = "_".join(parts)
        return f'X"{raw}"'

    # Known integer-typed variable names the LLM commonly declares
    _INT_VARS = re.compile(r'\b(Count|ErrorCount|count|error_count)\b')

    fixed_lines = []
    for line in body.splitlines():
        if _RST_ASSIGN.match(line):
            fixed_lines.append(f"-- [removed invalid signal assignment]: {line.strip()}")
            continue
        line = _TO_SLV.sub('std_logic_vector(', line)
        # Scoreboard.Check(integer_var) is a type error — comment it out
        def fix_sb_check(m: re.Match) -> str:
            if _INT_VARS.search(m.group(2)):
                return f"-- [TODO: {m.group(0).strip()} -- integer cannot be checked via slv scoreboard]"
            return m.group(0)
        line = _SB_CHECK_INT.sub(fix_sb_check, line)
        line = _HEX_LIT.sub(fix_hex, line)
        fixed_lines.append(line)
    return "\n".join(fixed_lines)


def generate_transactions(
    instances: list[VcInstance],
    entity_name: str,
    test_plan: str = "",
    generics: list | None = None,
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
        # The DUT role is what the resolver detected; the testbench VC takes the
        # opposite role (manager drives a subordinate DUT, and vice versa).
        tb_role = "manager" if inst.role == "subordinate" else "subordinate"
        api_text = get_api(inst.vc_type, tb_role)
        dw = _data_width(inst, generics or [])
        aw = _addr_width(inst, generics or [])
        prompt = _USER_TEMPLATE.format(
            entity_name=entity_name,
            vc_type=inst.vc_type,
            role=tb_role,
            rec_signal=inst.signal_name,
            sb_var=sb_var,
            sb_pkg=sb_pkg,
            data_width=dw,
            data_hex_digits=dw // 4,
            data_example="AB" if dw == 8 else "AB" * (dw // 8),
            addr_width=aw,
            addr_hex_digits=aw // 4,
            addr_example="0000_0000" if aw == 32 else "00" * (aw // 8),
            api=api_text,
        ) + plan_context

        try:
            response = llm.invoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=prompt),
            ])
            shared_decls, local_vars, body = _parse_block(response.content.strip())
            body = _sanitize(body, dw // 4)
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
