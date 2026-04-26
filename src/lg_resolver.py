"""LangGraph-based resolution pipeline.

Wraps the rule-based resolver in a state graph that can optionally call an LLM
to handle ambiguous port groups.  The deterministic path is unchanged when
llm_enabled=False or when there are no ambiguous groups.
"""

from __future__ import annotations

import sys
from typing import TypedDict

from langgraph.graph import END, StateGraph

from src.models import DutModel
from src.vc_resolver import AmbiguousGroup, VcInstance, VcResolution, resolve


class ResolverState(TypedDict):
    dut: DutModel
    resolution: VcResolution
    llm_enabled: bool
    llm_patches: list[VcInstance]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def _node_rule_resolve(state: ResolverState) -> ResolverState:
    return {**state, "resolution": resolve(state["dut"])}


def _node_llm_resolve(state: ResolverState) -> ResolverState:
    """Call the LLM for each ambiguous group and collect VcInstance patches."""
    from src.llm_vc_node import resolve_ambiguous_groups  # imported lazily to avoid cost at import time
    patches = resolve_ambiguous_groups(state["resolution"].ambiguous)
    return {**state, "llm_patches": patches}


def _node_merge(state: ResolverState) -> ResolverState:
    """Fold LLM-produced instances into the resolution, replacing the ambiguous list."""
    if not state["llm_patches"]:
        return state
    merged_instances = list(state["resolution"].instances) + state["llm_patches"]
    merged_resolution = VcResolution(instances=merged_instances, ambiguous=[])
    return {**state, "resolution": merged_resolution}


# ---------------------------------------------------------------------------
# Conditional edge
# ---------------------------------------------------------------------------

def _should_call_llm(state: ResolverState) -> str:
    if state["llm_enabled"] and state["resolution"].ambiguous:
        return "llm_resolve"
    return "merge"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def _build_graph():
    g = StateGraph(ResolverState)
    g.add_node("rule_resolve", _node_rule_resolve)
    g.add_node("llm_resolve",  _node_llm_resolve)
    g.add_node("merge",        _node_merge)

    g.set_entry_point("rule_resolve")
    g.add_conditional_edges("rule_resolve", _should_call_llm, {
        "llm_resolve": "llm_resolve",
        "merge":       "merge",
    })
    g.add_edge("llm_resolve", "merge")
    g.add_edge("merge", END)
    return g.compile()


_GRAPH = _build_graph()


def run_graph(dut: DutModel, llm_enabled: bool = False) -> VcResolution:
    """Run the resolution pipeline and return the final VcResolution."""
    initial: ResolverState = {
        "dut": dut,
        "resolution": VcResolution(instances=[], ambiguous=[]),
        "llm_enabled": llm_enabled,
        "llm_patches": [],
    }
    final = _GRAPH.invoke(initial)
    return final["resolution"]
