"""OSVVM Testbench Builder — CLI entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.extractor import extract
from src.vc_resolver import resolve
from src.renderer import render_all, _tb_entity_name


def _print_report(dut, res, results, unfilled):
    tb_name = _tb_entity_name(dut.entity_name)
    print()
    print("=== OSVVM Testbench Generator ===")
    print()
    print(f"Entity:    {dut.entity_name}  (library: {dut.library})")
    print()

    # Clocks
    if dut.clocks:
        print("Clocks found:")
        for clk in dut.clocks:
            port = next((p for p in dut.ports if p.name == clk), None)
            ptype = port.type if port else "?"
            print(f"  \u2713 {clk:<20} {ptype}")
    else:
        print("  ! No clocks detected — set clock signal manually")
    print()

    # Resets
    if dut.resets:
        print("Resets found:")
        for rst in dut.resets:
            port = next((p for p in dut.ports if p.name == rst.name), None)
            ptype = port.type if port else "?"
            active = "(active low)" if rst.active_low else "(active high)"
            print(f"  \u2713 {rst.name:<20} {ptype}  {active}")
    else:
        print("  ! No resets detected — set reset signal manually")
    print()

    # Verification Components
    vc_instances = [i for i in res.instances if i.spec]
    plain_instances = [i for i in res.instances if not i.spec]
    print("Verification Components:")
    if vc_instances:
        for inst in vc_instances:
            matched = len(inst.ports)
            total   = len(inst.spec.required)
            status  = '\u2713' if inst.is_complete else '~'
            print(f"  {status} {inst.vc_type.upper():<12} {inst.prefix!r:<18} ({matched}/{total} signals matched)")
    if plain_instances:
        plain_count = sum(len(i.ports) for i in plain_instances)
        print(f"  \u2713 Plain signals: {plain_count} port(s) mapped directly")
    if not vc_instances and not plain_instances:
        print("  (none — all ports are clocks/resets)")
    print()

    # Generics
    if dut.generics:
        print("Generics:")
        for g in dut.generics:
            default = f"  default={g.default}" if g.default else ""
            print(f"  \u2713 {g.name:<20} {g.type}{default}")
        print()

    # Output files
    print(f"Output files written to: {results.get('_output_dir', '.')}")
    for fname, ok in results.items():
        if fname.startswith('_'):
            continue
        marker = '\u2713' if ok else '\u2717'
        print(f"  {marker} {fname}")
    print()

    # Action required
    actions = []
    if not dut.clocks:
        actions.append("Set clock signal in TestCtrl_e.vhd and TbToplevelTemplate")
    if not dut.resets:
        actions.append("Set reset signal in TbTestTemplate and TbToplevelTemplate")
    for fname, tokens in unfilled.items():
        for tok in tokens:
            actions.append(f"{fname}: {tok.strip()} — fill in manually")
    for fname, ok in results.items():
        if fname.startswith('_'):
            continue
        if not ok:
            actions.append(f"{fname}: rendering failed — check errors above")

    # Always note the test stub
    test_file = f"TbTest_{dut.entity_name}.vhd"
    actions.append(f"{test_file}: StallProc — fill in test transactions (search: TODO)")

    print("Action required:")
    if actions:
        for a in actions:
            print(f"  ! {a}")
    else:
        print("  (none — testbench should compile as-is)")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Generate an OSVVM testbench from a VHDL entity file."
    )
    parser.add_argument("dut_file", help="Path to the DUT .vhd file")
    parser.add_argument("--output-dir", default="./tb_out", help="Output directory (default: ./tb_out)")
    parser.add_argument("--library", default="work", help="VHDL work library name (default: work)")
    args = parser.parse_args()

    dut_path   = Path(args.dut_file)
    output_dir = Path(args.output_dir)
    osvvm_dir  = Path(os.environ.get("OSVVM_DIR", "../OsvvmLibraries"))

    if not dut_path.is_file():
        print(f"error: file not found: {dut_path}", file=sys.stderr)
        sys.exit(1)

    dut = extract(dut_path, library=args.library)
    res = resolve(dut)
    results, unfilled = render_all(dut, res, output_dir)
    results['_output_dir'] = str(output_dir)

    _print_report(dut, res, results, unfilled)


if __name__ == "__main__":
    main()
