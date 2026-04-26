"""Full testbench-generation pipeline as a LangGraph state graph.

Nodes
-----
extract           — parse the VHDL entity into a DutModel
rule_resolve      — run the deterministic VC resolver
llm_resolve_one   — resolve a single AmbiguousGroup via LLM (fanned out in parallel)
merge             — fold LLM patches into the VcResolution
generate_txns     — generate initial OSVVM test transactions (llm_enabled only)
render            — render Jinja2 templates and write output files

Edges
-----
extract → rule_resolve → (conditional)
    no ambiguous groups, or llm disabled → merge
    ambiguous groups + llm enabled       → [Send] llm_resolve_one × N
llm_resolve_one → merge   (via reducer)
merge → generate_txns → render → END
"""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Send

from src.models import DutModel
from src.vc_resolver import AmbiguousGroup, VcInstance, VcResolution, resolve


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class PipelineState(TypedDict):
    # inputs — set once before the graph runs
    dut_path: str
    library: str
    output_dir: str
    llm_enabled: bool

    # set by extract node
    dut: DutModel | None

    # set by rule_resolve node
    resolution: VcResolution | None

    # accumulated by parallel llm_resolve_one nodes via reducer
    llm_patches: Annotated[list[VcInstance], operator.add]

    # set by generate_plan node (or empty string if skipped)
    test_plan: str

    # set by generate_txns node: {rec_signal: {"shared_decls", "local_vars", "body"}}
    generated_transactions: dict[str, dict]

    # set by render node
    results: dict[str, bool]
    unfilled: dict[str, list[str]]


# State for a single parallel LLM node — carries one group only
class SingleGroupState(TypedDict):
    group: AmbiguousGroup


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def _node_extract(state: PipelineState) -> dict:
    from src.extractor import extract
    dut = extract(Path(state["dut_path"]), library=state["library"])
    return {"dut": dut}


def _node_rule_resolve(state: PipelineState) -> dict:
    resolution = resolve(state["dut"])
    return {"resolution": resolution}


def _node_llm_resolve_one(state: SingleGroupState) -> dict:
    """Resolve a single AmbiguousGroup. Runs in parallel via Send fan-out."""
    from src.llm_vc_node import resolve_ambiguous_groups
    patches = resolve_ambiguous_groups([state["group"]])
    return {"llm_patches": patches}


def _node_merge(state: PipelineState) -> dict:
    """Merge LLM-produced instances into the resolution."""
    if not state.get("llm_patches"):
        return {}
    merged = VcResolution(
        instances=list(state["resolution"].instances) + state["llm_patches"],
        ambiguous=[],
    )
    return {"resolution": merged}


def _node_generate_plan(state: PipelineState) -> dict:
    """Generate a structured test plan via LLM. Only runs when llm_enabled."""
    if not state["llm_enabled"]:
        return {"test_plan": ""}
    from src.plan_gen_node import generate_plan
    plan = generate_plan(state["dut"], state["resolution"])
    return {"test_plan": plan}


def _node_generate_txns(state: PipelineState) -> dict:
    """Generate OSVVM test transactions via LLM. Only runs when llm_enabled."""
    if not state["llm_enabled"]:
        return {"generated_transactions": {}}
    from src.txn_gen_node import generate_transactions
    txns = generate_transactions(
        state["resolution"].instances,
        state["dut"].entity_name,
        test_plan=state.get("test_plan", ""),
    )
    return {"generated_transactions": txns}


def _node_render(state: PipelineState) -> dict:
    from src.renderer import render_all
    results, unfilled = render_all(
        state["dut"],
        state["resolution"],
        Path(state["output_dir"]),
        generated_transactions=state.get("generated_transactions", {}),
        dut_path=Path(state["dut_path"]),
    )
    return {"results": results, "unfilled": unfilled}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _route_after_resolve(state: PipelineState):
    """Fan out to one llm_resolve_one per ambiguous group, or go straight to merge."""
    if state["llm_enabled"] and state["resolution"].ambiguous:
        return [Send("llm_resolve_one", {"group": g}) for g in state["resolution"].ambiguous]
    return "merge"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_graph(interrupt_before_render: bool = False, interrupt_before_txns: bool = False):
    g = StateGraph(PipelineState)

    g.add_node("extract",          _node_extract)
    g.add_node("rule_resolve",     _node_rule_resolve)
    g.add_node("llm_resolve_one",  _node_llm_resolve_one)
    g.add_node("merge",            _node_merge)
    g.add_node("generate_plan",    _node_generate_plan)
    g.add_node("generate_txns",    _node_generate_txns)
    g.add_node("render",           _node_render)

    g.set_entry_point("extract")
    g.add_edge("extract", "rule_resolve")
    g.add_conditional_edges(
        "rule_resolve",
        _route_after_resolve,
        {"merge": "merge", "llm_resolve_one": "llm_resolve_one"},
    )
    g.add_edge("llm_resolve_one", "merge")
    g.add_edge("merge", "generate_plan")
    g.add_edge("generate_plan", "generate_txns")
    g.add_edge("generate_txns", "render")
    g.add_edge("render", END)

    interrupt_nodes = []
    if interrupt_before_render:
        interrupt_nodes.append("render")
    if interrupt_before_txns:
        interrupt_nodes.append("generate_txns")

    kwargs = {}
    if interrupt_nodes:
        kwargs["interrupt_before"] = interrupt_nodes
        kwargs["checkpointer"] = MemorySaver()

    return g.compile(**kwargs)


# Lazily built graphs keyed by interrupt flags
_GRAPHS: dict[tuple[bool, bool], object] = {}


def run_pipeline(
    dut_path: str | Path,
    library: str = "work",
    output_dir: str | Path = "./tb_out",
    llm_enabled: bool = False,
    interrupt_before_render: bool = False,
    interrupt_before_txns: bool = False,
    thread_id: str = "default",
) -> tuple[PipelineState, object | None]:
    """Run the pipeline and return (final_state, graph_handle).

    When interrupt_before_render=True a MemorySaver checkpointer is used and
    the graph may return before render completes.  The caller must inspect
    final_state["resolution"].llm_patches and optionally call resume_pipeline().

    Returns (state, graph) so the caller can resume if interrupted.
    """
    needs_checkpointer = interrupt_before_render or interrupt_before_txns
    key = (interrupt_before_render, interrupt_before_txns)
    if key not in _GRAPHS:
        _GRAPHS[key] = _build_graph(
            interrupt_before_render=interrupt_before_render,
            interrupt_before_txns=interrupt_before_txns,
        )
    graph = _GRAPHS[key]

    initial: PipelineState = {
        "dut_path":               str(dut_path),
        "library":                library,
        "output_dir":             str(output_dir),
        "llm_enabled":            llm_enabled,
        "dut":                    None,
        "resolution":             VcResolution(instances=[], ambiguous=[]),
        "llm_patches":            [],
        "test_plan":              "",
        "generated_transactions": {},
        "results":                {},
        "unfilled":               {},
    }

    config = {"configurable": {"thread_id": thread_id}} if needs_checkpointer else {}
    final = graph.invoke(initial, config)
    return final, (graph if needs_checkpointer else None)


def resume_pipeline(
    graph,
    patch_state: dict | None,
    thread_id: str = "default",
) -> PipelineState:
    """Resume an interrupted pipeline after human review.

    patch_state: dict of state fields to update before resuming (e.g. to strip
    llm_patches), or None to resume with no changes.
    """
    config = {"configurable": {"thread_id": thread_id}}
    if patch_state:
        graph.update_state(config, patch_state)
    return graph.invoke(None, config)
