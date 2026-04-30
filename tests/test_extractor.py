"""Tests for the VHDL entity extractor."""

import textwrap
from pathlib import Path

import pytest

from src.extractor import dut_from_f_file, extract
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


# ---------------------------------------------------------------------------
# dut_from_f_file
# ---------------------------------------------------------------------------

def test_dut_from_f_file_returns_last_vhd(tmp_path):
    """Last non-blank, non-comment line in a .f file is returned as the DUT path."""
    a = tmp_path / "a.vhd"
    b = tmp_path / "b.vhd"
    a.write_text("")
    b.write_text("")
    f_file = tmp_path / "design.f"
    f_file.write_text(f"a.vhd\nb.vhd\n")
    result = dut_from_f_file(f_file)
    assert result == b.resolve()


def test_dut_from_f_file_skips_comments_and_blanks(tmp_path):
    """Blank lines and # / // comments are ignored."""
    dut = tmp_path / "top.vhd"
    dut.write_text("")
    f_file = tmp_path / "design.f"
    f_file.write_text(
        "# this is a comment\n"
        "\n"
        "// another comment\n"
        "top.vhd\n"
        "\n"
    )
    result = dut_from_f_file(f_file)
    assert result == dut.resolve()


def test_dut_from_f_file_resolves_relative_to_f_dir(tmp_path):
    """Paths in the .f file are resolved relative to the .f file's directory."""
    subdir = tmp_path / "src"
    subdir.mkdir()
    vhd = subdir / "top.vhd"
    vhd.write_text("")
    f_file = tmp_path / "design.f"
    f_file.write_text("src/top.vhd\n")
    result = dut_from_f_file(f_file)
    assert result == vhd.resolve()
