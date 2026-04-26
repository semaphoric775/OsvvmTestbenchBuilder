"""Tests for the renderer — golden template immutability and basic output."""

import hashlib
from pathlib import Path

import pytest

from src.models import DutModel, Port, Direction, Reset
from src.renderer import render_all
from src.vc_resolver import resolve

_TEMPLATE_DIR = Path(__file__).parent.parent / "testbenchTemplate"


def _file_hashes(directory: Path) -> dict[str, str]:
    """Return {relative_path: sha256} for every file under directory."""
    return {
        str(f.relative_to(directory)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(directory.rglob("*"))
        if f.is_file()
    }


def _simple_dut() -> DutModel:
    return DutModel(
        entity_name="SimpleDut",
        ports=[
            Port(name="clk",     direction=Direction.IN,  type="std_logic"),
            Port(name="rst",     direction=Direction.IN,  type="std_logic"),
            Port(name="data_in", direction=Direction.IN,  type="std_logic_vector(7 downto 0)"),
            Port(name="data_out",direction=Direction.OUT, type="std_logic_vector(7 downto 0)"),
        ],
        clocks=["clk"],
        resets=[Reset(name="rst", active_low=False)],
    )


# ---------------------------------------------------------------------------
# za0: golden templates are unmodified after render
# ---------------------------------------------------------------------------

def test_golden_templates_unmodified(tmp_path):
    """Rendering must not modify any file in testbenchTemplate/."""
    before = _file_hashes(_TEMPLATE_DIR)
    assert before, "testbenchTemplate/ appears empty"

    dut = _simple_dut()
    res = resolve(dut)
    render_all(dut, res, tmp_path / "out")

    after = _file_hashes(_TEMPLATE_DIR)
    assert before == after, "One or more template files were modified during render"
