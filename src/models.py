from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Direction(str, Enum):
    IN = "in"
    OUT = "out"
    INOUT = "inout"


class Generic(BaseModel):
    name: str
    type: str
    default: Optional[str] = None


class Port(BaseModel):
    name: str
    direction: Direction
    type: str


class Reset(BaseModel):
    name: str
    active_low: bool = False
    clock: str | None = None   # paired clock name; None = async or unknown


class DutModel(BaseModel):
    entity_name: str
    library: str = "work"
    generics: list[Generic] = []
    ports: list[Port] = []
    clocks: list[str] = []        # port names identified as clocks
    resets: list[Reset] = []      # reset ports with active-level info
    dut_libraries: list[str] = [] # non-standard library/use/context lines from DUT preamble
