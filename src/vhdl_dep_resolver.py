#!/usr/bin/env python3
"""VHDL Dependency Resolver

Usage: python3 vhdl_dep_resolver.py <top_level.vhd> <search_dir> [--exclude PHRASE ...]

Outputs VHDL files in compilation order (dependencies first).
"""

import argparse
import re
import sys
from collections import deque
from pathlib import Path

# Strip single-line VHDL comments before any regex matching.
_COMMENT = re.compile(r'--[^\n]*')

ENTITY_DECL   = re.compile(r'\bentity\s+(\w+)\s+is', re.IGNORECASE)
# Negative lookahead excludes "package body <name>"
PACKAGE_DECL  = re.compile(r'\bpackage\s+(?!body\b)(\w+)\s+is', re.IGNORECASE)
PKG_BODY_DECL = re.compile(r'\bpackage\s+body\s+(\w+)', re.IGNORECASE)
ARCH_DECL     = re.compile(r'\barchitecture\s+\w+\s+of\s+(\w+)\s+is', re.IGNORECASE)
COMP_INST     = re.compile(r'\w+\s*:\s*(\w+)\s+(?:port|generic)\s+map', re.IGNORECASE)
ENTITY_INST   = re.compile(r'\w+\s*:\s*entity\s+\w+\.(\w+)', re.IGNORECASE)
USE_CLAUSE    = re.compile(r'\buse\s+\w+\.(\w+)', re.IGNORECASE)


def _read(f: Path) -> str:
    """Read a file and strip VHDL comments."""
    try:
        text = f.read_text(errors='replace')
    except OSError as e:
        print(f"warning: cannot read '{f}': {e}", file=sys.stderr)
        return ''
    return _COMMENT.sub('', text)


def index_directory(search_dir: Path, exclude: list[str] = []) -> tuple[
    dict[str, list[Path]],   # index: name -> [files]
    dict[str, Path],          # pkg_bodies: pkg_name -> body_file
    dict[str, list[Path]],   # arch_index: entity_name -> [separate arch files]
]:
    """Scan search_dir and build lookup tables for all VHDL design units."""
    index: dict[str, list[Path]] = {}
    pkg_bodies: dict[str, Path] = {}
    arch_index: dict[str, list[Path]] = {}

    for f in search_dir.rglob('*'):
        if f.suffix.lower() not in ('.vhd', '.vhdl'):
            continue
        if any(phrase in str(f) for phrase in exclude):
            continue
        text = _read(f)
        if not text:
            continue

        declared_entities = {n.lower() for n in ENTITY_DECL.findall(text)}
        for name in declared_entities:
            index.setdefault(name, []).append(f)
        for name in PACKAGE_DECL.findall(text):
            index.setdefault(name.lower(), []).append(f)
        for name in PKG_BODY_DECL.findall(text):
            pkg_bodies[name.lower()] = f
        for name in ARCH_DECL.findall(text):
            name = name.lower()
            # Only track arch files that are separate from their entity declaration.
            if name not in declared_entities:
                arch_index.setdefault(name, []).append(f)

    return index, pkg_bodies, arch_index


def extract_components(f: Path) -> tuple[list[str], list[str]]:
    """Return (hard_deps, soft_deps) for a VHDL file.

    hard_deps: component/entity instantiations — warn if unresolved.
    soft_deps: use-clause packages + package-body implicit dep — skip silently
               if unresolved (covers external libraries like ieee).
    """
    text = _read(f)
    hard = [n.lower() for n in ENTITY_INST.findall(text) + COMP_INST.findall(text)]
    soft = [n.lower() for n in USE_CLAUSE.findall(text)]
    for pkg in PKG_BODY_DECL.findall(text):
        soft.append(pkg.lower())
    return hard, soft


def _common_parts(a: Path, b: Path) -> int:
    """Count leading path components shared between two absolute paths."""
    count = 0
    for pa, pb in zip(a.parts, b.parts):
        if pa != pb:
            break
        count += 1
    return count


def build_graph(
    top_file: Path,
    index: dict[str, list[Path]],
    arch_index: dict[str, list[Path]],
) -> tuple[dict[str, Path], dict[Path, list[Path]]]:
    """BFS from top_file to resolve every reachable design unit to a file.

    When multiple files declare the same name, the one whose path shares the
    most leading components with top_file wins.

    Also enqueues separate architecture files and returns entity_file_to_archs
    so topo_sort knows to visit them after their entity file.

    Returns (resolved, entity_file_to_archs).
    """
    resolved: dict[str, Path] = {}
    entity_file_to_archs: dict[Path, list[Path]] = {}
    visited_files: set[Path] = set()

    # Invert index so we know what entity/package name(s) each file declares.
    file_to_names: dict[Path, list[str]] = {}
    for name, files in index.items():
        for f in files:
            file_to_names.setdefault(f, []).append(name)

    queue: deque[tuple[Path, int]] = deque([(top_file, 0)])
    visited_files.add(top_file)

    while queue:
        current_file, depth = queue.popleft()

        # Scan hard and soft deps from this file.
        hard, soft = extract_components(current_file)
        for name, warn in [(n, True) for n in hard] + [(n, False) for n in soft]:
            if name in resolved:
                continue
            candidates = index.get(name, [])
            if not candidates:
                if warn:
                    print(f"warning: no file found for '{name}'", file=sys.stderr)
                continue
            chosen = max(candidates, key=lambda c: _common_parts(c, top_file))
            resolved[name] = chosen
            if chosen not in visited_files:
                visited_files.add(chosen)
                queue.append((chosen, depth + 1))

        # For any entity this file declares, also enqueue separate arch files.
        for name in file_to_names.get(current_file, []):
            for arch_file in arch_index.get(name, []):
                if arch_file not in visited_files:
                    visited_files.add(arch_file)
                    queue.append((arch_file, depth + 1))
                    entity_file_to_archs.setdefault(current_file, []).append(arch_file)

    return resolved, entity_file_to_archs


def topo_sort(
    top_file: Path,
    resolved: dict[str, Path],
    pkg_bodies: dict[str, Path],
    entity_file_to_archs: dict[Path, list[Path]],
) -> list[Path]:
    """Post-order DFS -> compilation order (leaves first).

    After emitting an entity/package file:
    - its package body (if any) is spliced in immediately (no extra deps).
    - its separate architecture files are visited via full dfs (they have deps).
    """
    order: list[Path] = []
    visited: set[Path] = set()
    in_stack: set[Path] = set()

    # Map package declaration file -> its body file.
    pkg_file_to_body: dict[Path, Path] = {}
    for pkg_name, body_file in pkg_bodies.items():
        decl_file = resolved.get(pkg_name)
        if decl_file:
            pkg_file_to_body[decl_file] = body_file

    def dfs(f: Path) -> None:
        if f in in_stack:
            print(f"warning: circular dependency at '{f.name}'", file=sys.stderr)
            return
        if f in visited:
            return
        in_stack.add(f)
        hard, soft = extract_components(f)
        for name in hard + soft:
            dep = resolved.get(name)
            if dep:
                dfs(dep)
        in_stack.discard(f)
        visited.add(f)
        order.append(f)

        # Package body has no additional deps; splice in immediately.
        body = pkg_file_to_body.get(f)
        if body and body not in visited:
            visited.add(body)
            order.append(body)

        # Separate architecture files may have deps; visit via full dfs.
        for arch_file in entity_file_to_archs.get(f, []):
            dfs(arch_file)

    dfs(top_file)
    return order


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Outputs VHDL files in compilation order (dependencies first).'
    )
    parser.add_argument('top_file', help='Top-level VHDL file')
    parser.add_argument('search_dir', help='Directory to search for VHDL files')
    parser.add_argument(
        '--exclude', metavar='PHRASE', action='append', default=[],
        help='Exclude files whose path contains PHRASE (can be repeated)'
    )
    args = parser.parse_args()

    top_file   = Path(args.top_file).resolve()
    search_dir = Path(args.search_dir).resolve()

    if not top_file.is_file():
        print(f"error: top-level file not found: {top_file}", file=sys.stderr)
        sys.exit(1)
    if not search_dir.is_dir():
        print(f"error: search directory not found: {search_dir}", file=sys.stderr)
        sys.exit(1)

    index, pkg_bodies, arch_index = index_directory(search_dir, args.exclude)
    resolved, entity_file_to_archs = build_graph(top_file, index, arch_index)
    order = topo_sort(top_file, resolved, pkg_bodies, entity_file_to_archs)

    for f in order:
        print(f)


if __name__ == '__main__':
    main()
