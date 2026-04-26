"""Load optional config.toml from the current working directory."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    osvvm_dir: Path | None = None
    compiled_libs_dir: Path | None = None


def load_config(config_path: Path | None = None) -> Config:
    """Read config.toml if present; return defaults if absent or missing keys."""
    path = config_path or Path("config.toml")
    if not path.is_file():
        return Config()

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    ghdl = data.get("ghdl", {})
    return Config(
        osvvm_dir=Path(ghdl["osvvm_dir"]) if "osvvm_dir" in ghdl else None,
        compiled_libs_dir=Path(ghdl["compiled_libs_dir"]) if "compiled_libs_dir" in ghdl else None,
    )
