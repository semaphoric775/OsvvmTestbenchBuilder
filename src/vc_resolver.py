"""VC Resolver — maps DUT ports to OSVVM Verification Component instances."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from src.models import DutModel, Port

# ---------------------------------------------------------------------------
# Known VC definitions
# ---------------------------------------------------------------------------

@dataclass
class VcSpec:
    """Static description of an OSVVM VC type."""
    vc_type: str
    # Signal suffixes required to confirm a full VC group (lowercase, no prefix)
    required: list[str]
    # OSVVM library and context for use clauses
    osvvm_library: str
    osvvm_context: str
    # Record type names for manager and subordinate roles
    manager_rec_type: str
    subordinate_rec_type: str
    # Default OSVVM generic overrides / instantiation template info
    component_name: str


_VC_SPECS: list[VcSpec] = [
    VcSpec(
        vc_type="axi4lite",
        required=[
            "awvalid", "awready", "awaddr",
            "wvalid",  "wready",  "wdata",  "wstrb",
            "bvalid",  "bready",  "bresp",
            "arvalid", "arready", "araddr",
            "rvalid",  "rready",  "rdata",  "rresp",
        ],
        osvvm_library="osvvm_axi4",
        osvvm_context="osvvm_axi4.Axi4LiteContext",
        manager_rec_type="Axi4LiteRecType",
        subordinate_rec_type="Axi4LiteRecType",
        component_name="Axi4LiteSubordinate",
    ),
    VcSpec(
        vc_type="axi4",
        required=[
            "awvalid", "awready", "awaddr", "awlen", "awsize", "awburst",
            "wvalid",  "wready",  "wdata",  "wstrb", "wlast",
            "bvalid",  "bready",  "bresp",
            "arvalid", "arready", "araddr", "arlen", "arsize", "arburst",
            "rvalid",  "rready",  "rdata",  "rresp", "rlast",
        ],
        osvvm_library="osvvm_axi4",
        osvvm_context="osvvm_axi4.Axi4Context",
        manager_rec_type="Axi4RecType",
        subordinate_rec_type="Axi4RecType",
        component_name="Axi4Subordinate",
    ),
    VcSpec(
        vc_type="uart",
        required=["txd", "rxd"],
        osvvm_library="osvvm_uart",
        osvvm_context="osvvm_uart.UartContext",
        manager_rec_type="UartRecType",
        subordinate_rec_type="UartRecType",
        component_name="UartTbPkg",
    ),
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class VcInstance:
    vc_type: str            # "axi4lite", "uart", "plain"
    prefix: str             # port name prefix matched (e.g. "s_axi_"), or "" for plain
    role: str               # "manager" | "subordinate" | "signal"
    ports: list[Port]       # ports belonging to this group
    spec: VcSpec | None     # None for plain signals
    missing: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return len(self.missing) == 0

    @property
    def signal_name(self) -> str:
        """Name for the VC transaction record signal in the testbench."""
        return f"{self.prefix.rstrip('_')}Rec" if self.prefix else ""


@dataclass
class VcResolution:
    instances: list[VcInstance]


# ---------------------------------------------------------------------------
# Resolution logic
# ---------------------------------------------------------------------------

def _group_by_prefix(ports: list[Port]) -> dict[str, list[Port]]:
    """Group ports by their longest common underscore-delimited prefix."""
    groups: dict[str, list[Port]] = {}
    for port in ports:
        parts = port.name.split('_')
        # Try progressively shorter prefixes to find a group of ≥3 ports.
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

    Returns a VcInstance (possibly with missing signals) if any required
    signals are found, else None.
    """
    suffix_map = {p.name[len(prefix):].lower(): p for p in ports}
    found   = [s for s in spec.required if s in suffix_map]
    missing = [s for s in spec.required if s not in suffix_map]

    if not found:
        return None

    matched_ports = [suffix_map[s] for s in found]
    role = _infer_role(matched_ports)

    if missing:
        print(
            f"warning: VC '{spec.vc_type}' prefix '{prefix}': "
            f"missing signal(s): {', '.join(missing)}. "
            f"Falling back to plain signals.",
            file=sys.stderr,
        )
        return None

    return VcInstance(
        vc_type=spec.vc_type,
        prefix=prefix,
        role=role,
        ports=matched_ports,
        spec=spec,
        missing=missing,
    )


def resolve(dut: DutModel) -> VcResolution:
    """Map DUT ports to OSVVM VC instances and plain signals."""
    # Exclude clock and reset ports — they are handled separately.
    control_names = set(dut.clocks + [r.name for r in dut.resets])
    interface_ports = [p for p in dut.ports if p.name not in control_names]

    groups = _group_by_prefix(interface_ports)
    instances: list[VcInstance] = []
    claimed: set[str] = set()

    for prefix, ports in sorted(groups.items(), key=lambda kv: -len(kv[0])):
        if prefix == '':
            continue  # plain signals handled below
        for spec in _VC_SPECS:
            inst = _match_vc(prefix, ports, spec)
            if inst:
                instances.append(inst)
                claimed.update(p.name for p in inst.ports)
                break

    # Remaining unclaimed ports (including the '' group) become plain signals.
    plain_ports = [p for p in interface_ports if p.name not in claimed]
    plain_ports += groups.get('', [])
    # deduplicate while preserving order
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

    return VcResolution(instances=instances)
