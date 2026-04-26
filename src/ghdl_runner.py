"""Run a generated testbench through GHDL via OSVVM TCL scripts.

Requires:
  - OSVVM_DIR (or OSVVM_PATH) environment variable pointing to OsvvmLibraries
  - tclsh and ghdl on PATH (StartGHDL.tcl sourced automatically by GHDL setup)
  - The generated runTests.pro must exist in output_dir

Invocation model:
  tclsh launcher.tcl   (run from output_dir, OSVVM_DIR set in env)

where launcher.tcl contains:
  source $OSVVM_DIR/Scripts/StartUp.tcl
  build $OSVVM_DIR/OsvvmLibraries.pro
  build runTests.pro
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GhdlResult:
    success: bool
    returncode: int
    stdout: str
    stderr: str
    error_lines: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


def _find_osvvm_dir() -> Path | None:
    for var in ("OSVVM_DIR", "OSVVM_PATH"):
        val = os.environ.get(var)
        if val:
            return Path(val)
    return None


def _parse_errors(output: str) -> list[str]:
    """Extract GHDL/OSVVM compile/simulate error lines from combined output."""
    results = []
    for line in output.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in ("error:", "fatal:", "failure:")):
            # Skip OSVVM summary lines — success/failure judged via _osvvm_compile_ok
            if any(skip in lower for skip in ("alerts:", "passed:", "analyze errors:", "simulate errors:")):
                continue
            results.append(line.strip())
    return results


def _osvvm_compile_ok(output: str) -> bool:
    """Return True if the OSVVM build summary shows zero analyze and simulate errors.

    A stub testbench with no assertions produces Passed: 0 / Failed: 1 (NOCHECKS),
    which is expected — we only care that the VHDL itself compiled and simulated.
    """
    import re
    # Look for the runTests build summary line, e.g.:
    #   BuildError: foo FAILED, ..., Analyze Errors: 0,  Simulate Errors: 0, ...
    #   Build: foo PASSED, ..., Analyze Errors: 0,  Simulate Errors: 0, ...
    for line in output.splitlines():
        if "analyze errors:" in line.lower() and "simulate errors:" in line.lower():
            analyze = re.search(r'Analyze Errors:\s*(\d+)', line, re.IGNORECASE)
            simulate = re.search(r'Simulate Errors:\s*(\d+)', line, re.IGNORECASE)
            if analyze and simulate:
                return int(analyze.group(1)) == 0 and int(simulate.group(1)) == 0
    return False


def run_ghdl(output_dir: Path, osvvm_dir: Path | None = None) -> GhdlResult:
    """Run the generated testbench at output_dir through GHDL.

    If osvvm_dir is None, reads OSVVM_DIR or OSVVM_PATH from the environment.
    Returns GhdlResult with skipped=True if prerequisites are missing.
    """
    if osvvm_dir is None:
        osvvm_dir = _find_osvvm_dir()

    if osvvm_dir is None:
        return GhdlResult(
            success=False, returncode=-1, stdout="", stderr="",
            skipped=True, skip_reason="OSVVM_DIR not set",
        )

    startup_tcl = osvvm_dir / "Scripts" / "StartUp.tcl"
    if not startup_tcl.exists():
        return GhdlResult(
            success=False, returncode=-1, stdout="", stderr="",
            skipped=True, skip_reason=f"StartUp.tcl not found at {startup_tcl}",
        )

    run_pro = output_dir / "runTests.pro"
    if not run_pro.exists():
        return GhdlResult(
            success=False, returncode=-1, stdout="", stderr="",
            skipped=True, skip_reason=f"runTests.pro not found in {output_dir}",
        )

    osvvm_libs_pro = osvvm_dir / "OsvvmLibraries.pro"
    launcher = (
        f"source {{{startup_tcl}}}\n"
        f"build {{{osvvm_libs_pro}}}\n"
        f"build runTests.pro\n"
    )

    fd, launcher_path = tempfile.mkstemp(suffix=".tcl", prefix="osvvm_launch_")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(launcher)

        env = os.environ.copy()
        env["OSVVM_DIR"] = str(osvvm_dir)

        proc = subprocess.run(
            ["tclsh", launcher_path],
            cwd=output_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        Path(launcher_path).unlink(missing_ok=True)
        return GhdlResult(
            success=False, returncode=-1,
            stdout="", stderr="tclsh timed out after 180 s",
            error_lines=["Timed out"],
        )
    finally:
        Path(launcher_path).unlink(missing_ok=True)

    combined = proc.stdout + proc.stderr
    error_lines = _parse_errors(combined)
    success = _osvvm_compile_ok(combined) and not error_lines

    return GhdlResult(
        success=success,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        error_lines=error_lines,
    )
