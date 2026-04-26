"""Tests for VC resolver — prefix grouping and VC matching."""

import pytest

from src.models import Direction, DutModel, Port, Reset
from src.vc_resolver import resolve


def _port(name: str, direction: str = "in", typ: str = "std_logic") -> Port:
    return Port(name=name, direction=Direction(direction), type=typ)


def _axi4lite_ports(prefix: str, role: str = "subordinate") -> list[Port]:
    """Build a full AXI4-Lite port set under *prefix*."""
    if role == "subordinate":
        # From DUT perspective: subordinate receives writes/reads → mostly inputs
        return [
            _port(f"{prefix}awvalid", "in"),
            _port(f"{prefix}awready", "out"),
            _port(f"{prefix}awaddr",  "in",  "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}wvalid",  "in"),
            _port(f"{prefix}wready",  "out"),
            _port(f"{prefix}wdata",   "in",  "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}wstrb",   "in",  "std_logic_vector(3 downto 0)"),
            _port(f"{prefix}bvalid",  "out"),
            _port(f"{prefix}bready",  "in"),
            _port(f"{prefix}bresp",   "out", "std_logic_vector(1 downto 0)"),
            _port(f"{prefix}arvalid", "in"),
            _port(f"{prefix}arready", "out"),
            _port(f"{prefix}araddr",  "in",  "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}rvalid",  "out"),
            _port(f"{prefix}rready",  "in"),
            _port(f"{prefix}rdata",   "out", "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}rresp",   "out", "std_logic_vector(1 downto 0)"),
        ]
    else:
        # Manager drives writes/reads → mostly outputs
        return [
            _port(f"{prefix}awvalid", "out"),
            _port(f"{prefix}awready", "in"),
            _port(f"{prefix}awaddr",  "out", "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}wvalid",  "out"),
            _port(f"{prefix}wready",  "in"),
            _port(f"{prefix}wdata",   "out", "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}wstrb",   "out", "std_logic_vector(3 downto 0)"),
            _port(f"{prefix}bvalid",  "in"),
            _port(f"{prefix}bready",  "out"),
            _port(f"{prefix}bresp",   "in",  "std_logic_vector(1 downto 0)"),
            _port(f"{prefix}arvalid", "out"),
            _port(f"{prefix}arready", "in"),
            _port(f"{prefix}araddr",  "out", "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}rvalid",  "in"),
            _port(f"{prefix}rready",  "out"),
            _port(f"{prefix}rdata",   "in",  "std_logic_vector(31 downto 0)"),
            _port(f"{prefix}rresp",   "in",  "std_logic_vector(1 downto 0)"),
        ]


# ---------------------------------------------------------------------------
# ag2: two independent VC prefixes (m_axi_* and s_axi_*)
# ---------------------------------------------------------------------------

def test_two_independent_vc_prefixes():
    """A DUT with both m_axi_ and s_axi_ ports should produce two VC instances."""
    ports = (
        [_port("clk"), _port("rst")]
        + _axi4lite_ports("m_axi_", role="manager")
        + _axi4lite_ports("s_axi_", role="subordinate")
    )
    dut = DutModel(
        entity_name="MyBridge",
        ports=ports,
        clocks=["clk"],
        resets=[Reset(name="rst")],
    )
    res = resolve(dut)

    vc_instances = [i for i in res.instances if i.spec is not None]
    assert len(vc_instances) == 2

    prefixes = {i.prefix for i in vc_instances}
    assert "m_axi_" in prefixes
    assert "s_axi_" in prefixes

    m = next(i for i in vc_instances if i.prefix == "m_axi_")
    s = next(i for i in vc_instances if i.prefix == "s_axi_")
    assert m.role == "manager"
    assert s.role == "subordinate"

    # Component names must come from the correct slot
    assert m.component_name == m.spec.manager_component
    assert s.component_name == s.spec.subordinate_component


# ---------------------------------------------------------------------------
# x2f: incomplete AXI4Lite group falls back to plain signals
# ---------------------------------------------------------------------------

def test_incomplete_axi4lite_becomes_ambiguous():
    """An AXI4-Lite group missing required ports should surface as an ambiguous group."""
    # Only provide the write-address channel — missing 13 required signals
    ports = [
        _port("clk"),
        _port("rst"),
        _port("s_axi_awvalid", "in"),
        _port("s_axi_awready", "out"),
        _port("s_axi_awaddr",  "in", "std_logic_vector(31 downto 0)"),
    ]
    dut = DutModel(
        entity_name="Incomplete",
        ports=ports,
        clocks=["clk"],
        resets=[Reset(name="rst")],
    )
    res = resolve(dut)

    vc_instances = [i for i in res.instances if i.spec is not None]
    assert len(vc_instances) == 0

    assert len(res.ambiguous) == 1
    amb = res.ambiguous[0]
    assert amb.prefix == "s_axi_"
    assert amb.closest_spec == "axi4lite"
    assert "wvalid" in amb.missing


# ---------------------------------------------------------------------------
# uqi: AXI Stream partial match (axis_video_ prefix, tdata/tlast present but
#      optional sidebands like tkeep/tuser absent)
# ---------------------------------------------------------------------------

def test_axi_stream_partial_match():
    """axis_video_ ports with only tvalid/tready/tdata/tlast should match as axi4stream."""
    ports = [
        _port("clk"),
        _port("rst"),
        # Transmitter side: tvalid out, tready in
        _port("axis_video_tvalid", "out"),
        _port("axis_video_tready", "in"),
        _port("axis_video_tdata",  "out", "std_logic_vector(23 downto 0)"),
        _port("axis_video_tlast",  "out"),
    ]
    dut = DutModel(
        entity_name="VideoSrc",
        ports=ports,
        clocks=["clk"],
        resets=[Reset(name="rst")],
    )
    res = resolve(dut)

    vc_instances = [i for i in res.instances if i.spec is not None]
    assert len(vc_instances) == 1

    inst = vc_instances[0]
    assert inst.vc_type == "axi4stream"
    assert inst.prefix == "axis_video_"
    # tkeep, tstrb, tuser, tid, tdest are absent — that's fine
    assert set(inst.missing) >= {"tkeep", "tstrb", "tuser", "tid", "tdest"}
    # matched ports include the present optional signals
    matched_suffixes = {p.name[len("axis_video_"):] for p in inst.ports}
    assert "tvalid" in matched_suffixes
    assert "tready" in matched_suffixes
    assert "tdata"  in matched_suffixes
    assert "tlast"  in matched_suffixes


def test_axi_stream_minimum_match():
    """tvalid + tready alone (no optionals) is still a valid AXIS interface."""
    ports = [
        _port("clk"),
        _port("rst"),
        _port("s_axis_tvalid", "in"),
        _port("s_axis_tready", "out"),
        # one extra unrelated port to hit the >=3 prefix threshold
        _port("s_axis_tdata",  "in", "std_logic_vector(7 downto 0)"),
    ]
    dut = DutModel(
        entity_name="MinAxis",
        ports=ports,
        clocks=["clk"],
        resets=[Reset(name="rst")],
    )
    res = resolve(dut)

    vc_instances = [i for i in res.instances if i.spec is not None]
    assert len(vc_instances) == 1
    assert vc_instances[0].vc_type == "axi4stream"
