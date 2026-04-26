"""OSVVM Testbench Builder — CLI entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.pipeline import resume_pipeline, run_pipeline
from src.renderer import _tb_entity_name


def _print_report(dut, res, results, unfilled):
    print()
    print("=== OSVVM Testbench Generator ===")
    print()
    print(f"Entity:    {dut.entity_name}  (library: {dut.library})")
    print()

    if dut.clocks:
        print("Clocks found:")
        for clk in dut.clocks:
            port = next((p for p in dut.ports if p.name == clk), None)
            print(f"  ✓ {clk:<20} {port.type if port else '?'}")
    else:
        print("  ! No clocks detected — set clock signal manually")
    print()

    if dut.resets:
        print("Resets found:")
        for rst in dut.resets:
            port = next((p for p in dut.ports if p.name == rst.name), None)
            active = "(active low)" if rst.active_low else "(active high)"
            print(f"  ✓ {rst.name:<20} {port.type if port else '?'}  {active}")
    else:
        print("  ! No resets detected — set reset signal manually")
    print()

    vc_instances    = [i for i in res.instances if i.spec]
    plain_instances = [i for i in res.instances if not i.spec]
    print("Verification Components:")
    for inst in vc_instances:
        matched = len(inst.ports)
        total   = len(inst.spec.required)
        status  = "✓" if inst.is_complete else "~"
        tag     = " (LLM-inferred — verify)" if inst.llm_inferred else ""
        print(f"  {status} {inst.vc_type.upper():<12} {inst.prefix!r:<18} ({matched}/{total} signals matched){tag}")
        for p in inst.ports:
            suffix = p.name[len(inst.prefix):]
            print(f"      {p.name:<28} → {suffix} ({p.direction.value})")
        if inst.missing:
            for m in inst.missing:
                print(f"      {'(no ' + inst.prefix + m + ')':<28} → {m} (not in DUT)")
    if plain_instances:
        plain_count = sum(len(i.ports) for i in plain_instances)
        print(f"  ✓ Plain signals: {plain_count} port(s) mapped directly")
        for inst in plain_instances:
            for p in inst.ports:
                print(f"      {p.name:<28} ({p.direction.value})")
    for amb in res.ambiguous:
        preview = ", ".join(amb.missing[:3]) + ("..." if len(amb.missing) > 3 else "")
        print(f"  ! UNRESOLVED     {amb.prefix!r:<18} (closest: {amb.closest_spec}, missing: {preview})")
    if not vc_instances and not plain_instances and not res.ambiguous:
        print("  (none — all ports are clocks/resets)")
    print()

    if dut.generics:
        print("Generics:")
        for g in dut.generics:
            default = f"  default={g.default}" if g.default else ""
            print(f"  ✓ {g.name:<20} {g.type}{default}")
        print()

    print(f"Output files written to: {results.get('_output_dir', '.')}")
    for fname, ok in results.items():
        if fname.startswith("_"):
            continue
        print(f"  {'✓' if ok else '✗'} {fname}")
    print()

    actions = []
    if not dut.clocks:
        actions.append("Set clock signal in TestCtrl_e.vhd and TbToplevelTemplate")
    if not dut.resets:
        actions.append("Set reset signal in TbTestTemplate and TbToplevelTemplate")
    for fname, tokens in unfilled.items():
        for tok in tokens:
            actions.append(f"{fname}: {tok.strip()} — fill in manually")
    for fname, ok in results.items():
        if fname.startswith("_"):
            continue
        if not ok:
            actions.append(f"{fname}: rendering failed — check errors above")
    for inst in vc_instances:
        if inst.llm_inferred:
            actions.append(f"Verify LLM-inferred VC '{inst.vc_type}' for prefix '{inst.prefix}' before simulating")
    for amb in res.ambiguous:
        actions.append(f"Unresolved port group '{amb.prefix}' — re-run with --llm or map manually")
    actions.append(f"TbTest_{dut.entity_name}.vhd: StallProc — fill in test transactions (search: TODO)")

    print("Action required:")
    for a in actions:
        print(f"  ! {a}")
    if not actions:
        print("  (none — testbench should compile as-is)")
    print()


def _confirm_test_plan(plan: str) -> bool:
    """Print the generated test plan and ask the user to confirm. Returns True to proceed."""
    print()
    print("── Generated test plan ──────────────────────────────────────")
    for line in plan.splitlines():
        print(f"  {line}")
    print("─────────────────────────────────────────────────────────────")
    print("Proceed with this plan? [Y/n] ", end="", flush=True)

    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    return answer in ("", "y", "yes")


def _confirm_llm_mappings(resolution) -> bool:
    """Print LLM-proposed VC mappings and ask the user to confirm. Returns True to proceed."""
    llm_instances = [i for i in resolution.instances if i.llm_inferred]
    if not llm_instances:
        return True

    print()
    print("── LLM-proposed VC mappings ─────────────────────────────────")
    for inst in llm_instances:
        missing_str = f", missing: {', '.join(inst.missing)}" if inst.missing else ""
        print(f"  {inst.vc_type.upper():<12} prefix={inst.prefix!r}  role={inst.role}{missing_str}")
    print("─────────────────────────────────────────────────────────────")
    print("Proceed with these mappings? [Y/n] ", end="", flush=True)

    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    return answer in ("", "y", "yes")


def main():
    parser = argparse.ArgumentParser(
        description="Generate an OSVVM testbench from a VHDL entity file."
    )
    parser.add_argument("dut_file", help="Path to the DUT .vhd file")
    parser.add_argument("--output-dir", default="./tb_out", help="Output directory (default: ./tb_out)")
    parser.add_argument("--library", default="work", help="VHDL work library name (default: work)")
    parser.add_argument("--llm", action="store_true", help="Use LLM to resolve ambiguous port groups (requires ANTHROPIC_API_KEY)")
    parser.add_argument("--plan", action="store_true", help="Generate and approve a test plan before transaction generation (implies --llm)")
    args = parser.parse_args()

    dut_path = Path(args.dut_file)
    if not dut_path.is_file():
        print(f"error: file not found: {dut_path}", file=sys.stderr)
        sys.exit(1)

    llm_enabled = args.llm or args.plan
    interrupt_render = llm_enabled       # always pause before render when LLM is on
    interrupt_txns   = args.plan         # pause before txns only with --plan

    state, graph = run_pipeline(
        dut_path=dut_path,
        library=args.library,
        output_dir=args.output_dir,
        llm_enabled=llm_enabled,
        interrupt_before_render=interrupt_render,
        interrupt_before_txns=interrupt_txns,
    )

    if interrupt_txns and graph is not None and state.get("test_plan"):
        accepted = _confirm_test_plan(state["test_plan"])
        if not accepted:
            state = resume_pipeline(graph, {"test_plan": ""})
        else:
            state = resume_pipeline(graph, None)
        # After resuming past generate_txns, graph may still be paused before render
        graph = graph if interrupt_render else None

    if interrupt_render and graph is not None:
        # Graph paused before render — show proposed mappings and ask to confirm.
        accepted = _confirm_llm_mappings(state["resolution"])
        if not accepted:
            # Strip LLM patches: revert inferred instances back to ambiguous plain signals.
            res = state["resolution"]
            non_llm = [i for i in res.instances if not i.llm_inferred]
            from src.vc_resolver import VcResolution
            patched_res = VcResolution(instances=non_llm, ambiguous=res.ambiguous)
            state = resume_pipeline(graph, {"resolution": patched_res, "llm_patches": []})
        else:
            state = resume_pipeline(graph, None)

    dut        = state["dut"]
    res        = state["resolution"]
    results    = dict(state["results"])
    unfilled   = state["unfilled"]
    results["_output_dir"] = args.output_dir

    _print_report(dut, res, results, unfilled)


if __name__ == "__main__":
    main()
