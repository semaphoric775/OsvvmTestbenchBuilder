"""Tests for the VHDL entity extractor."""

import textwrap
from pathlib import Path

import pytest

from src.extractor import extract
from src.models import Direction


def _vhd(content: str, tmp_path: Path) -> Path:
    f = tmp_path / "dut.vhd"
    f.write_text(textwrap.dedent(content))
    return f


# ---------------------------------------------------------------------------
# za0: multi-name port declarations (a, b : in std_logic)
# ---------------------------------------------------------------------------

def test_multi_name_port_declaration(tmp_path):
    """a, b : in std_logic should produce two separate Port entries."""
    src = """\
        entity MultiPort is
        port (
            clk, en : in  std_logic ;
            data_out : out std_logic_vector(7 downto 0)
        ) ;
        end entity ;
    """
    dut = extract(_vhd(src, tmp_path))

    names = [p.name for p in dut.ports]
    assert "clk" in names
    assert "en" in names
    assert "data_out" in names

    clk_port = next(p for p in dut.ports if p.name == "clk")
    en_port  = next(p for p in dut.ports if p.name == "en")
    assert clk_port.direction == Direction.IN
    assert en_port.direction  == Direction.IN
    assert clk_port.type == en_port.type
