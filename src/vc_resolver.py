"""VC Resolver — maps DUT ports to OSVVM Verification Component instances."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from src.models import DutModel, Port

# ---------------------------------------------------------------------------
# VC spec registry — loaded from src/vc_specs.json
# ---------------------------------------------------------------------------

_SPECS_FILE = Path(__file__).parent / "vc_specs.json"


@dataclass
class VcSpec:
    """Static description of an OSVVM VC type."""
    vc_type: str
    required: list[str]           # must all be present for a match
    optional: list[str]           # may be absent; absence is noted but not a blocker
    osvvm_library: str
    osvvm_context: str
    manager_rec_type: str
    subordinate_rec_type: str
    manager_component: str
    subordinate_component: str
    # Maps lowercase spec field names to VC entity port names (e.g. "tvalid" -> "TValid").
    # Empty for VCs that bundle physical signals into a record (e.g. AXI4Lite AxiBus).
    port_name_map: dict = dc_field(default_factory=dict)


def _load_specs(path: Path = _SPECS_FILE) -> list[VcSpec]:
    data = json.loads(path.read_text())
    return [VcSpec(**entry) for entry in data]


_VC_SPECS: list[VcSpec] = _load_specs()


@dataclass
class VcInstance:
    vc_type: str            # "axi4lite", "uart", "plain"
    prefix: str             # port name prefix matched (e.g. "s_axi_"), or "" for plain
    role: str               # "manager" | "subordinate" | "signal"
    ports: list[Port]       # ports belonging to this group
    spec: VcSpec | None     # None for plain signals
    missing: list[str] = dc_field(default_factory=list)
    llm_inferred: bool = False

    @property
    def is_complete(self) -> bool:
        return len(self.missing) == 0

    @property
    def signal_name(self) -> str:
        """Name for the VC transaction record signal in the testbench."""
        return f"{self.prefix.rstrip('_')}Rec" if self.prefix else ""

    @property
    def component_name(self) -> str:
        """OSVVM entity component name for this role.

        When the DUT is subordinate, the testbench VC must be the manager (drives the DUT).
        When the DUT is manager, the testbench VC must be the subordinate.
        """
        if self.spec is None:
            return ""
        return (
            self.spec.manager_component
            if self.role == "subordinate"
            else self.spec.subordinate_component
        )


@dataclass
class AmbiguousGroup:
    """A port group that partially matched a VC spec but couldn't be fully resolved."""
    prefix: str
    ports: list[Port]
    closest_spec: str | None   # vc_type of the closest partial match, or None
    missing: list[str]         # required signals absent from the closest match


@dataclass
class VcResolution:
    instances: list[VcInstance]
    ambiguous: list[AmbiguousGroup] = dc_field(default_factory=list)

# ---------------------------------------------------------------------------
# Resolution logic
# ---------------------------------------------------------------------------

def _group_by_prefix(ports: list[Port]) -> dict[str, list[Port]]:
    """Group ports by their longest common underscore-delimited prefix."""
    groups: dict[str, list[Port]] = {}
    for port in ports:
        parts = port.name.split('_')
        for length in range(len(parts) - 1, 0, -1):
            prefix = '_'.join(parts[:length]) + '_'
            others = [p for p in ports if p.name.startswith(prefix)]
            if len(others) >= 3:
                groups.setdefault(prefix, [])
                if port not in groups[prefix]:
                    groups[prefix].append(port)
                break
        else:
            groups.setdefault('', []).append(port)
    return groups

def _infer_role(ports: list[Port]) -> str:
    """Infer manager/subordinate from port directions.

    In a subordinate (slave), address/data channels are inputs (driven by manager).
    """
    in_count  = sum(1 for p in ports if p.direction.value == 'in')
    out_count = sum(1 for p in ports if p.direction.value == 'out')
    return "subordinate" if in_count >= out_count else "manager"

def _match_vc(prefix: str, ports: list[Port], spec: VcSpec) -> VcInstance | None:
    """Try to match a group of ports against a VC spec.

    All signals in spec.required must be present.  Signals in spec.optional
    may be absent; their absence is logged at info level but does not block
    the match.  Returns None if any required signal is missing.
    """
    suffix_map = {p.name[len(prefix):].lower(): p for p in ports}

    missing_required = [s for s in spec.required if s not in suffix_map]
    if missing_required:
        return None

    matched_ports = [suffix_map[s] for s in spec.required]

    missing_optional = [s for s in spec.optional if s not in suffix_map]
    if missing_optional:
        print(
            f"info: VC '{spec.vc_type}' prefix '{prefix}': "
            f"optional signal(s) not present: {', '.join(missing_optional)}.",
            file=sys.stderr,
        )
    for s in spec.optional:
        if s in suffix_map:
            matched_ports.append(suffix_map[s])

    role = _infer_role(matched_ports)
    return VcInstance(
        vc_type=spec.vc_type,
        prefix=prefix,
        role=role,
        ports=matched_ports,
        spec=spec,
        missing=missing_optional,
    )


def _best_partial_match(prefix: str, ports: list[Port]) -> AmbiguousGroup | None:
    """Return an AmbiguousGroup if any spec has a partial (but not full) required-signal overlap."""
    suffix_map = {p.name[len(prefix):].lower(): p for p in ports}
    best_spec: str | None = None
    best_missing: list[str] = []
    best_overlap = 0

    for spec in _VC_SPECS:
        present  = [s for s in spec.required if s in suffix_map]
        missing  = [s for s in spec.required if s not in suffix_map]
        if present and missing and len(present) > best_overlap:
            best_overlap = len(present)
            best_spec    = spec.vc_type
            best_missing = missing

    if best_spec is None:
        return None
    return AmbiguousGroup(prefix=prefix, ports=ports, closest_spec=best_spec, missing=best_missing)

def resolve(dut: DutModel) -> VcResolution:
    """Map DUT ports to OSVVM VC instances and plain signals."""
    control_names = set(dut.clocks + [r.name for r in dut.resets])
    interface_ports = [p for p in dut.ports if p.name not in control_names]

    groups = _group_by_prefix(interface_ports)
    instances: list[VcInstance] = []
    ambiguous: list[AmbiguousGroup] = []
    claimed: set[str] = set()

    for prefix, ports in sorted(groups.items(), key=lambda kv: -len(kv[0])):
        if prefix == '':
            continue
        matched = False
        for spec in _VC_SPECS:
            inst = _match_vc(prefix, ports, spec)
            if inst:
                instances.append(inst)
                claimed.update(p.name for p in inst.ports)
                matched = True
                break

        if not matched:
            partial = _best_partial_match(prefix, ports)
            if partial:
                print(
                    f"warning: prefix '{prefix}' partially matches '{partial.closest_spec}' "
                    f"but is missing: {', '.join(partial.missing)}. "
                    f"Marking ambiguous.",
                    file=sys.stderr,
                )
                ambiguous.append(partial)
                claimed.update(p.name for p in ports)

    plain_ports = [p for p in interface_ports if p.name not in claimed]
    plain_ports += groups.get('', [])
    seen: set[str] = set()
    unique_plain: list[Port] = []
    for p in plain_ports:
        if p.name not in seen:
            seen.add(p.name)
            unique_plain.append(p)

    if unique_plain:
        instances.append(VcInstance(
            vc_type="plain",
            prefix="",
            role="signal",
            ports=unique_plain,
            spec=None,
        ))

    return VcResolution(instances=instances, ambiguous=ambiguous)
