"""Renderer — builds Jinja2 context from DutModel + VcResolution and renders templates."""

from __future__ import annotations

import re
import sys
import tempfile
import textwrap
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateError

from src.models import DutModel, Reset
from src.vc_resolver import VcInstance, VcResolution

_TEMPLATE_DIR = Path(__file__).parent.parent / "testbenchTemplate"
_UNFILLED = re.compile(r'\{\{[^}]+\}\}')

# Indentation helpers
_IND4   = '    '
_IND8   = '        '
_IND12  = '            '  # matches template port map indentation

# Direction-suffix stripping — ordered longest-first to avoid partial matches
_DIR_SUFFIXES = ['_input', '_output', '_inout', '_in', '_out', '_io', '_i', '_o']

_OPPOSITE_SUFFIX: dict[str, str] = {
    'i': 'o',         'o': 'i',
    'in': 'out',      'out': 'in',
    'input': 'output','output': 'input',
    'inout': 'inout', 'io': 'io',
}

_INVERT_DIR: dict[str, str] = {'in': 'out', 'out': 'in', 'inout': 'inout'}


def _parse_dir_suffix(name: str) -> tuple[str, str | None]:
    """Return (base, suffix) for a recognized direction suffix, else (name, None).

    Suffix is returned without the leading underscore (e.g. 'i', 'out').
    """
    lower = name.lower()
    for s in _DIR_SUFFIXES:
        if lower.endswith(s):
            return name[:-len(s)], s[1:]
    return name, None


def _signal_name_for(port_name: str) -> str:
    base, _ = _parse_dir_suffix(port_name)
    return base


def _tc_port_name_for(port_name: str) -> str:
    base, suffix = _parse_dir_suffix(port_name)
    if suffix:
        return f"{base}_{_OPPOSITE_SUFFIX.get(suffix.lower(), suffix)}"
    return base


def _tc_dir_for(vhdl_dir: str) -> str:
    return _INVERT_DIR.get(vhdl_dir, vhdl_dir)


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _tb_entity_name(entity_name: str) -> str:
    return f"Tb{entity_name[0].upper()}{entity_name[1:]}"


def _dut_libraries_block(libs: list[str]) -> str:
    return '\n'.join(libs)


def _project_libraries(instances: list[VcInstance]) -> str:
    libs: list[str] = []
    seen: set[str] = set()
    for inst in instances:
        if inst.spec and inst.spec.osvvm_library not in seen:
            seen.add(inst.spec.osvvm_library)
            libs.append(f"library {inst.spec.osvvm_library} ;")
            libs.append(f"  context {inst.spec.osvvm_context} ;")
    return '\n'.join(libs)


def _clk_period_constants(clocks: list[str]) -> str:
    lines = []
    for clk in clocks:
        const_name = f"CLK_PERIOD_{clk.upper()}"
        lines.append(f"constant {const_name} : time := 10 ns ;  -- TODO: set clock period")
    return '\n'.join(f"{_IND4}{l}" for l in lines)


def _clock_definitions(clocks: list[str]) -> str:
    return '\n'.join(f"{_IND4}signal {c} : std_logic := '0' ;" for c in clocks)


def _reset_definitions(resets: list[Reset]) -> str:
    return '\n'.join(f"{_IND4}signal {r.name} : std_logic := '0' ;" for r in resets)


def _signal_definitions(instances: list[VcInstance]) -> str:
    seen: set[str] = set()
    lines = []
    for inst in instances:
        if inst.vc_type == "plain":
            for p in inst.ports:
                sig = _signal_name_for(p.name)
                if sig not in seen:
                    seen.add(sig)
                    lines.append(f"{_IND4}signal {sig} : {p.type} ;")
    return '\n'.join(lines)


def _vc_record_signals(instances: list[VcInstance]) -> str:
    lines = []
    for inst in instances:
        if inst.spec:
            rec_type = (
                inst.spec.subordinate_rec_type
                if inst.role == "subordinate"
                else inst.spec.manager_rec_type
            )
            lines.append(f"{_IND4}signal {inst.signal_name} : {rec_type} ;")
    return '\n'.join(lines)


def _reindent(rendered: str, indent: str) -> str:
    """Dedent a rendered sub-template then reindent every line uniformly."""
    block = textwrap.dedent(rendered).strip()
    return '\n'.join(indent + line for line in block.splitlines())


def _clock_instantiations(clocks: list[str], tmpl) -> str:
    if tmpl is None:
        return ""
    blocks = []
    for clk in clocks:
        rendered = tmpl.render(
            clock=clk,
            clock_period=f"CLK_PERIOD_{clk.upper()}",
        )
        blocks.append(_reindent(rendered, _IND4))
    return '\n'.join(blocks)


def _reset_instantiations(resets: list[Reset], clocks: list[str], tmpl) -> str:
    if tmpl is None:
        return ""
    fallback_clk = clocks[0] if clocks else "clk"
    blocks = []
    for rst in resets:
        clk = rst.clock if rst.clock else fallback_clk
        rendered = tmpl.render(
            reset=rst.name,
            reset_active="'0'" if rst.active_low else "'1'",
            clock=clk,
            clock_period=f"CLK_PERIOD_{clk.upper()}",
            reset_tpd="2 ns",
        )
        blocks.append(_reindent(rendered, _IND4))
    return '\n'.join(blocks)


def _vc_instantiations(instances: list[VcInstance]) -> str:
    lines = []
    for inst in instances:
        if not inst.spec:
            continue
        lines.append(
            f"{_IND4}{inst.signal_name}_VC : entity osvvm_{inst.vc_type}.{inst.spec.component_name}\n"
            f"{_IND8}port map ( {inst.vc_type.upper()}Rec => {inst.signal_name} ) ;"
        )
    return '\n'.join(lines)


def _dut_port_mappings(dut: DutModel, instances: list[VcInstance]) -> str:
    plain_names = {p.name for inst in instances if inst.vc_type == "plain" for p in inst.ports}
    lines = []
    for p in dut.ports:
        sig = _signal_name_for(p.name) if p.name in plain_names else p.name
        lines.append(f"{p.name:<30} => {sig}")
    return (',\n' + _IND12).join(lines)


def _testctrl_ports_section(instances: list[VcInstance], indent: str = _IND8) -> str:
    """Port declarations for TestCtrl entity (plain signal ports + VC record ports).

    No trailing semicolon — the caller is responsible for the semicolon on the
    preceding port and the closing ) ;.
    """
    lines = []
    for inst in instances:
        if inst.vc_type == "plain":
            for p in inst.ports:
                tc_name = _tc_port_name_for(p.name)
                tc_dir  = _tc_dir_for(p.direction.value).capitalize()
                lines.append(f"{indent}{tc_name:<20} : {tc_dir:<6}{p.type}")
    for inst in instances:
        if inst.spec:
            rec_type = (
                inst.spec.subordinate_rec_type
                if inst.role == "subordinate"
                else inst.spec.manager_rec_type
            )
            lines.append(f"{indent}{inst.signal_name:<20} : inout {rec_type}")
    return ' ;\n'.join(lines)


def _testctrl_port_mappings(clocks: list[str], resets: list[Reset], instances: list[VcInstance]) -> str:
    lines = [f"{c:<30} => {c}" for c in clocks]
    lines += [f"{r.name:<30} => {r.name}" for r in resets]
    for inst in instances:
        if inst.vc_type == "plain":
            for p in inst.ports:
                lines.append(f"{_tc_port_name_for(p.name):<30} => {_signal_name_for(p.name)}")
    lines += [f"{i.signal_name:<30} => {i.signal_name}" for i in instances if i.spec]
    return (',\n' + _IND12).join(lines)


def _generic_section(dut: DutModel) -> str:
    if not dut.generics:
        return ''
    lines = []
    for g in dut.generics:
        default = f" := {g.default}" if g.default else ''
        lines.append(f"{_IND8}{g.name} : {g.type}{default}")
    inner = ' ;\n'.join(lines)
    return f"{_IND4}generic (\n{inner}\n{_IND4}) ;"


def _testctrl_component_decl(clk: str, rst: str, all_instances: list, has_extra_ports: bool) -> str:
    semicolon = " ;" if has_extra_ports else ""
    lines = [
        f"{_IND4}component TestCtrl is",
        f"{_IND8}port (",
        f"{_IND8}    {clk:<20} : In    std_logic ;",
        f"{_IND8}    {rst:<20} : In    std_logic{semicolon}",
    ]
    ports = _testctrl_ports_section(all_instances, indent=_IND12)
    if ports:
        lines.append(ports)
    lines += [
        f"{_IND8}) ;",
        f"{_IND4}end component TestCtrl ;",
    ]
    return '\n'.join(lines)


def _test_time(dut: DutModel) -> str:
    return "10 ms"


def _reset_signal(dut: DutModel) -> str:
    return dut.resets[0].name if dut.resets else "rst"


def _reset_active_level(dut: DutModel) -> str:
    return "'0'" if (dut.resets and dut.resets[0].active_low) else "'1'"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def build_context(dut: DutModel, res: VcResolution, env=None) -> dict:
    tb_name = _tb_entity_name(dut.entity_name)
    all_instances = res.instances
    vc_instances  = [i for i in all_instances if i.spec]

    clk = dut.clocks[0] if dut.clocks else "clk"
    rst = dut.resets[0].name if dut.resets else "rst"

    has_extra_ports = bool(vc_instances or any(i.vc_type == "plain" for i in all_instances))

    return {
        # Toplevel template
        "TbTopLevelTemplate":           tb_name,
        "project_libraries":            _project_libraries(all_instances),
        "dut_libraries":                _dut_libraries_block(dut.dut_libraries),
        "constant_clk_per_definitions": _clk_period_constants(dut.clocks),
        "clock_definitions":            _clock_definitions(dut.clocks),
        "reset_definitions":            _reset_definitions(dut.resets),
        "signal_definitions":           _signal_definitions(all_instances),
        "verification_component_records": _vc_record_signals(all_instances),
        "component_declarations":       "",  # no extra component decls needed
        "TestCtrl_definition":          _testctrl_component_decl(clk, rst, all_instances, has_extra_ports),
        "clock_instantiations":         _clock_instantiations(
            dut.clocks,
            env.get_template("OsvvmClockTemplate.vhd") if env else None,
        ),
        "reset_instantiations":         _reset_instantiations(
            dut.resets, dut.clocks,
            env.get_template("OsvvmResetTemplate.vhd") if env else None,
        ),
        "osvvm_vc_instantiations":      _vc_instantiations(all_instances),
        "DUT_entity_name":              dut.entity_name,
        "DUT_port_mappings":            _dut_port_mappings(dut, all_instances),
        "TestCtrl_instantiation":       "TestCtrl",
        "TestCtrl_port_mappings":       _testctrl_port_mappings(dut.clocks, dut.resets, all_instances),

        # TestCtrl entity
        "generic_section":     _generic_section(dut),
        "clock_signal":        clk,
        "resetn_signal":       rst,
        "semicolon_needed":    ";" if has_extra_ports else "",
        "ports_section":       _testctrl_ports_section(all_instances),
        "generic_calculations": "",
        "fifo_aliases":        "",

        # Test template
        "test_time":           _test_time(dut),
        "reset_signal":        rst,
        "reset_active_level":  _reset_active_level(dut),
        "tb_toplevel":         tb_name,

        # Build script
        "libraryname":          dut.library,
        "toolsettings":         "# TODO: add tool settings",
        "reldir":               "../..",
        "project_dir":          f"$BASE_DIR/{dut.entity_name}",
        "analyze_project_section": f"analyze $PROJECT_DIR/{dut.entity_name}.vhd",
        "testbench_top":        f"{tb_name}.vhd",
        "tbtest_file":          f"TbTest_{dut.entity_name}.vhd",
        "debug_mode":           "TRUE",
        "log_mode":             "TRUE",
        "base_test":            "TbTestTemplate",
    }


def render_all(dut: DutModel, res: VcResolution, output_dir: Path) -> dict[str, bool]:
    """Render all 4 templates into output_dir.

    Returns {filename: success} for each file.
    Templates in testbenchTemplate/ are never modified.
    Each file is written atomically (temp file → rename).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        variable_start_string='{{',
        variable_end_string='}}',
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=__import__('jinja2').Undefined,
    )

    tb_name = _tb_entity_name(dut.entity_name)
    template_output_map = {
        "TbToplevelTemplate.vhd": f"{tb_name}.vhd",
        "TestCtrl_e.vhd":         "TestCtrl_e.vhd",
        "TbTestTemplate.vhd":     f"TbTest_{dut.entity_name}.vhd",
        "build_template.pro":     "runTests.pro",
    }

    context = build_context(dut, res, env)
    results: dict[str, bool] = {}
    unfilled: dict[str, list[str]] = {}

    for template_name, out_name in template_output_map.items():
        try:
            tmpl = env.get_template(template_name)
            rendered = tmpl.render(**context)
        except TemplateError as e:
            print(f"error: rendering '{template_name}' failed: {e}", file=sys.stderr)
            results[out_name] = False
            continue

        # Scan for unfilled placeholders.
        remaining = _UNFILLED.findall(rendered)
        if remaining:
            unfilled[out_name] = remaining

        # Atomic write.
        out_path = output_dir / out_name
        try:
            fd, tmp_path = tempfile.mkstemp(dir=output_dir, prefix=f".{out_name}.")
            with open(fd, 'w') as fh:
                fh.write(rendered)
            Path(tmp_path).rename(out_path)
            results[out_name] = True
        except OSError as e:
            print(f"error: writing '{out_name}' failed: {e}", file=sys.stderr)
            results[out_name] = False

    return results, unfilled
