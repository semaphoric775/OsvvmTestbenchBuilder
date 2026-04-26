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


def _clk_period_constants(clocks: list[str], generics: list | None = None) -> str:
    generic_map = {}
    if generics:
        for g in generics:
            if g.type.strip().lower() == "time" and g.default:
                generic_map[g.name.lower()] = g.default

    lines = []
    for clk in clocks:
        const_name = f"CLK_PERIOD_{clk.upper()}"
        # Look for tperiod_<ClkName> or <ClkName>_period in the generics.
        period_val = (
            generic_map.get(f"tperiod_{clk.lower()}")
            or generic_map.get(f"{clk.lower()}_period")
        )
        if period_val:
            lines.append(f"constant {const_name} : time := {period_val} ;")
        else:
            lines.append(f"constant {const_name} : time := 10 ns ;  -- TODO: set clock period")
    return '\n'.join(f"{_IND4}{l}" for l in lines)


def _clock_definitions(clocks: list[str]) -> str:
    return '\n'.join(f"{_IND4}signal {c} : std_logic := '0' ;" for c in clocks)


def _reset_definitions(resets: list[Reset]) -> str:
    return '\n'.join(f"{_IND4}signal {r.name} : std_logic := '0' ;" for r in resets)


def _substitute_generics(type_str: str, generics: list) -> str:
    """Replace generic names in a port type string with their default values."""
    result = type_str
    for g in generics:
        if g.default and re.search(r'\b' + re.escape(g.name) + r'\b', result, re.IGNORECASE):
            result = re.sub(r'\b' + re.escape(g.name) + r'\b', g.default, result, flags=re.IGNORECASE)
    return result


def _axi_stream_optional_type(field_name: str, data_width: int) -> str:
    """Return signal type for an unmatched optional AXI-Stream port."""
    if field_name == "tlast":
        return "std_logic"
    if field_name in ("tstrb", "tkeep"):
        w = max(1, data_width // 8)
        return f"std_logic_vector({w - 1} downto 0)"
    # tuser, tid, tdest — PARAM_WIDTH defaults to 1
    return "std_logic_vector(0 downto 0)"


def _signal_definitions(instances: list[VcInstance], generics: list | None = None) -> str:
    seen: set[str] = set()
    lines = []
    for inst in instances:
        if inst.vc_type == "plain":
            for p in inst.ports:
                sig = _signal_name_for(p.name)
                if sig not in seen:
                    seen.add(sig)
                    typ = _substitute_generics(p.type, generics or [])
                    lines.append(f"{_IND4}signal {sig} : {typ} ;")
        elif inst.spec and inst.spec.port_name_map:
            # VC with individual physical signals (e.g. axi4stream) — declare intermediates.
            for p in inst.ports:
                if p.name not in seen:
                    seen.add(p.name)
                    typ = _substitute_generics(p.type, generics or [])
                    lines.append(f"{_IND4}signal {p.name} : {typ} ;")
            # Declare minimal-size dummy signals for unmatched optional ports.
            matched = {p.name[len(inst.prefix):].lower() for p in inst.ports}
            if inst.vc_type == "axi4stream":
                dw = int(_stream_data_width(inst, generics or []))
            else:
                dw = 8
            for field_name in inst.spec.optional:
                if field_name not in matched:
                    sig_name = f"{inst.prefix}{field_name}"
                    if sig_name not in seen:
                        seen.add(sig_name)
                        typ = _axi_stream_optional_type(field_name, dw)
                        lines.append(f"{_IND4}signal {sig_name} : {typ} ;")
    return '\n'.join(lines)


def _stream_param_width(inst: 'VcInstance', generics: list) -> int:
    """Compute ParamToModel width: 1 (tlast) + tid_w + tdest_w + tuser_w.

    The OSVVM AxiStreamTbPkg packs {Last, ID, Dest, User} into a single Param
    vector and indexes into it.  If Param is undersized the simulation crashes
    with an index-out-of-bounds error.
    """
    prefix = inst.prefix

    def _field_width(field: str, default: int) -> int:
        # Check matched ports first
        for p in inst.ports:
            if p.name[len(prefix):].lower() == field:
                typ = _substitute_generics(p.type, generics)
                m = re.search(r'\((.+?)downto', typ)
                if m:
                    try:
                        return int(m.group(1).strip()) + 1
                    except ValueError:
                        pass
                return 1  # std_logic
        # Not matched — use the default dummy width from _axi_stream_optional_type
        return default

    tid_w   = _field_width("tid",   1)
    tdest_w = _field_width("tdest", 1)
    tuser_w = _field_width("tuser", 1)
    return 1 + tid_w + tdest_w + tuser_w


def _stream_rec_constraint(inst: 'VcInstance', generics: list) -> str:
    """Build a constrained StreamRecType string from the matched TData port width."""
    data_range = "7 downto 0"
    for p in inst.ports:
        if p.name[len(inst.prefix):].lower() == "tdata":
            typ = _substitute_generics(p.type, generics)
            m = re.search(r'\((.+?)\)', typ)
            if m:
                data_range = m.group(1)
            break
    param_w = _stream_param_width(inst, generics)
    param_range = f"{param_w - 1} downto 0"
    return (
        f"StreamRecType("
        f"DataToModel({data_range}), "
        f"DataFromModel({data_range}), "
        f"ParamToModel({param_range}), "
        f"ParamFromModel({param_range}))"
    )


def _vc_rec_type(inst: 'VcInstance', generics: list, constrained: bool = True) -> str:
    """Return the transaction record type string for a VC instance."""
    rec_type = (
        inst.spec.manager_rec_type
        if inst.role == "subordinate"
        else inst.spec.subordinate_rec_type
    )
    if constrained and rec_type == "StreamRecType":
        return _stream_rec_constraint(inst, generics)
    return rec_type


def _vc_record_signals(instances: list[VcInstance], generics: list | None = None) -> str:
    lines = []
    for inst in instances:
        if inst.spec:
            rec_type = _vc_rec_type(inst, generics or [])
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


def _stream_data_width(inst: 'VcInstance', generics: list) -> str:
    """Extract DATA_WIDTH integer string from the matched TData port."""
    for p in inst.ports:
        if p.name[len(inst.prefix):].lower() == "tdata":
            typ = _substitute_generics(p.type, generics)
            m = re.search(r'\((.+?)\)', typ)
            if m:
                rng = m.group(1).strip()
                # "N-1 downto 0" → N
                nm = re.match(r'^(\d+)\s*-\s*1\s+downto\s+0$', rng)
                if nm:
                    return nm.group(1)
                # "N downto 0" → N+1
                nm = re.match(r'^(\d+)\s+downto\s+0$', rng)
                if nm:
                    return str(int(nm.group(1)) + 1)
    return "8"


def _vc_instantiations(instances: list[VcInstance], clk: str, resets: list, generics: list | None = None) -> str:
    lines = []
    rst_signal = resets[0] if resets else None

    for inst in instances:
        if not inst.spec:
            continue

        nreset_expr = (
            rst_signal.name if (rst_signal and rst_signal.active_low)
            else f"not {rst_signal.name}" if rst_signal
            else "not rst  -- TODO: connect reset"
        )

        pm = [
            f"Clk      => {clk}",
            f"nReset   => {nreset_expr}",
        ]

        matched_suffixes = {p.name[len(inst.prefix):].lower() for p in inst.ports}

        if inst.spec.port_name_map:
            # Connected matched ports; connect unmatched optional ports to dummy signals.
            for field_name, vc_port in inst.spec.port_name_map.items():
                if field_name in matched_suffixes:
                    dut_port = next(
                        p.name for p in inst.ports
                        if p.name[len(inst.prefix):].lower() == field_name
                    )
                    pm.append(f"{vc_port:<8} => {dut_port}")
                elif field_name in {f for f in inst.spec.optional}:
                    dummy = f"{inst.prefix}{field_name}"
                    pm.append(f"{vc_port:<8} => {dummy}")

        pm.append(f"TransRec => {inst.signal_name}")

        pm_body = (",\n" + _IND12).join(pm)
        lines.append(
            f"{_IND4}{inst.signal_name}_VC : entity {inst.spec.osvvm_library}.{inst.component_name}\n"
            f"{_IND8}port map (\n"
            f"{_IND12}{pm_body}\n"
            f"{_IND8}) ;"
        )
    return '\n'.join(lines)


def _dut_port_mappings(dut: DutModel, instances: list[VcInstance]) -> str:
    plain_names = {p.name for inst in instances if inst.vc_type == "plain" for p in inst.ports}
    lines = []
    for p in dut.ports:
        sig = _signal_name_for(p.name) if p.name in plain_names else p.name
        lines.append(f"{p.name:<30} => {sig}")
    return (',\n' + _IND12).join(lines)


def _testctrl_ports_section(instances: list[VcInstance], indent: str = _IND8, generics: list | None = None) -> str:
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
                typ = _substitute_generics(p.type, generics or [])
                lines.append(f"{indent}{tc_name:<20} : {tc_dir:<6}{typ}")
    for inst in instances:
        if inst.spec:
            rec_type = _vc_rec_type(inst, generics or [], constrained=False)
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


def _testctrl_component_decl(clk: str, rst: str, all_instances: list, has_extra_ports: bool, generics: list | None = None) -> str:
    semicolon = " ;" if has_extra_ports else ""
    lines = [
        f"{_IND4}component TestCtrl is",
        f"{_IND8}port (",
        f"{_IND8}    {clk:<20} : In    std_logic ;",
        f"{_IND8}    {rst:<20} : In    std_logic{semicolon}",
    ]
    ports = _testctrl_ports_section(all_instances, indent=_IND12, generics=generics)
    if ports:
        lines.append(ports)
    lines += [
        f"{_IND8}) ;",
        f"{_IND4}end component TestCtrl ;",
    ]
    return '\n'.join(lines)


_OSVVM_LIB_PATHS: dict[str, str] = {
    "osvvm_axi4": "$::env(OSVVM_DIR)/AXI4",
    "osvvm_uart": "$::env(OSVVM_DIR)/UART",
}


def _toolsettings(instances: list[VcInstance], osvvm_dir: Path | None = None, compiled_libs_dir: Path | None = None) -> str:
    used_libs = dict.fromkeys(
        inst.spec.osvvm_library for inst in instances if inst.spec
    )

    lines: list[str] = []

    if osvvm_dir is not None:
        if compiled_libs_dir is not None:
            lines.append(f"LinkLibraryDirectory {{{compiled_libs_dir}}}")
        else:
            lines.append(f"build {{{osvvm_dir / 'OsvvmLibraries.pro'}}}")
    else:
        lines += [
            "# ── Library linking (source StartUp.tcl before building this file) ───────────",
            "# LinkLibraryDirectory /path/to/OsvvmLibraries/CompiledLibs",
        ]

    if used_libs:
        lines += [
            "#",
            "# ── OSVVM library dependencies (must be pre-compiled) ────────────────────────",
        ] + [f"#   {_OSVVM_LIB_PATHS[lib]}  ({lib})" for lib in used_libs if lib in _OSVVM_LIB_PATHS]

    return '\n'.join(lines)


def _test_time(dut: DutModel) -> str:
    return "10 ms"


def _reset_signal(dut: DutModel) -> str:
    return dut.resets[0].name if dut.resets else "rst"


def _reset_active_level(dut: DutModel) -> str:
    return "'0'" if (dut.resets and dut.resets[0].active_low) else "'1'"


_STALLPROC_STUB = """\
  ------------------------------------------------------------
  -- Stall test and wait for timeout
  --    TODO: Replace with actual test procedure
  ------------------------------------------------------------
  StallProc : process
  begin
      WaitForBarrier(TestDone) ;
      wait ;
  end process StallProc ;"""


def _get_txn_field(generated: dict, signal_name: str, field: str) -> str:
    """Extract a field from generated_transactions entry (new dict format or legacy str)."""
    entry = generated.get(signal_name)
    if entry is None:
        return ""
    if isinstance(entry, dict):
        return entry.get(field, "")
    # Legacy string format: treat whole value as body
    return entry if field == "body" else ""


def _test_declarations(instances: list[VcInstance], generated: dict) -> str:
    """Collect shared variable declarations from all generated transaction blocks."""
    lines = []
    for inst in instances:
        if not inst.spec or inst.vc_type == "plain":
            continue
        decl = _get_txn_field(generated, inst.signal_name, "shared_decls")
        if decl:
            for line in decl.splitlines():
                lines.append(f"    {line}")
    return "\n".join(lines)


def _test_processes(instances: list[VcInstance], generated: dict) -> str:
    """Build the test process block for TbTestTemplate.

    If generated transactions exist, emit one process per VC instance.
    Otherwise emit the static StallProc stub.
    """
    vc_instances = [i for i in instances if i.spec and i.vc_type != "plain"]
    if not generated or not vc_instances:
        return _STALLPROC_STUB

    blocks = []
    for inst in vc_instances:
        body = _get_txn_field(generated, inst.signal_name, "body")
        if not body:
            continue
        local_vars = _get_txn_field(generated, inst.signal_name, "local_vars")
        proc_name = f"{inst.signal_name}Proc"
        indented_vars = "\n".join(f"    {l}" for l in local_vars.splitlines()) if local_vars else ""
        indented_body = "\n".join(f"    {line}" for line in body.splitlines())
        var_section = f"{indented_vars}\n" if indented_vars else ""
        block = (
            f"  ------------------------------------------------------------\n"
            f"  -- {inst.vc_type.upper()} {inst.role} transactions — {inst.signal_name}\n"
            f"  --   Generated by OSVVM Testbench Builder (verify before simulating)\n"
            f"  ------------------------------------------------------------\n"
            f"  {proc_name} : process\n"
            f"{var_section}"
            f"  begin\n"
            f"{indented_body}\n"
            f"  end process {proc_name} ;"
        )
        blocks.append(block)

    if not blocks:
        return _STALLPROC_STUB

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def build_context(dut: DutModel, res: VcResolution, env=None, generated_transactions: dict[str, str] | None = None, dut_path: Path | None = None, osvvm_dir: Path | None = None, compiled_libs_dir: Path | None = None) -> dict:
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
        "constant_clk_per_definitions": _clk_period_constants(dut.clocks, dut.generics),
        "clock_definitions":            _clock_definitions(dut.clocks),
        "reset_definitions":            _reset_definitions(dut.resets),
        "signal_definitions":           _signal_definitions(all_instances, dut.generics),
        "verification_component_records": _vc_record_signals(all_instances, dut.generics),
        "component_declarations":       "",  # no extra component decls needed
        "TestCtrl_definition":          _testctrl_component_decl(clk, rst, all_instances, has_extra_ports, dut.generics),
        "clock_instantiations":         _clock_instantiations(
            dut.clocks,
            env.get_template("OsvvmClockTemplate.vhd") if env else None,
        ),
        "reset_instantiations":         _reset_instantiations(
            dut.resets, dut.clocks,
            env.get_template("OsvvmResetTemplate.vhd") if env else None,
        ),
        "osvvm_vc_instantiations":      _vc_instantiations(all_instances, clk, dut.resets, dut.generics),
        "DUT_entity_name":              dut.entity_name,
        "DUT_port_mappings":            _dut_port_mappings(dut, all_instances),
        "TestCtrl_instantiation":       "TestCtrl",
        "TestCtrl_port_mappings":       _testctrl_port_mappings(dut.clocks, dut.resets, all_instances),

        # TestCtrl entity
        "generic_section":     _generic_section(dut),
        "clock_signal":        clk,
        "resetn_signal":       rst,
        "semicolon_needed":    ";" if has_extra_ports else "",
        "ports_section":       _testctrl_ports_section(all_instances, generics=dut.generics),
        "generic_calculations": "",
        "fifo_aliases":        "",

        # Test template
        "test_time":           _test_time(dut),
        "reset_signal":        rst,
        "reset_active_level":  _reset_active_level(dut),
        "tb_toplevel":         tb_name,
        "test_declarations":   _test_declarations(all_instances, generated_transactions or {}),
        "test_processes":      _test_processes(all_instances, generated_transactions or {}),

        # Build script
        "libraryname":          dut.library,
        "toolsettings":         _toolsettings(all_instances, osvvm_dir=osvvm_dir, compiled_libs_dir=compiled_libs_dir),
        "analyze_project_section": (
            f"analyze {dut_path.resolve()}"
            if dut_path else
            f"analyze {dut.entity_name}.vhd  # TODO: set absolute path to DUT"
        ),
        "testbench_top":        f"{tb_name}.vhd",
        "tbtest_file":          f"TbTest_{dut.entity_name}.vhd",
        "debug_mode":           "TRUE",
        "log_mode":             "TRUE",
        "base_test":            tb_name,
    }


def render_all(dut: DutModel, res: VcResolution, output_dir: Path, generated_transactions: dict[str, str] | None = None, dut_path: Path | None = None) -> dict[str, bool]:
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

    from src.config import load_config
    cfg = load_config()
    context = build_context(dut, res, env, generated_transactions=generated_transactions, dut_path=dut_path, osvvm_dir=cfg.osvvm_dir, compiled_libs_dir=cfg.compiled_libs_dir)
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
