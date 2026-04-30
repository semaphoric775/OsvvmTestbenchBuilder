"""VHDL entity extractor — parses an entity declaration into a DutModel."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from nltk.metrics.distance import edit_distance, jaccard_distance

from src.models import Direction, DutModel, Generic, Port, Reset

# Tokens that indicate signal type, not domain — stripped before similarity comparison.
_DOMAIN_STRIP = frozenset({'clk', 'clock', 'rst', 'reset', 'n', 'b'})


def _domain_tokens(name: str) -> frozenset[str]:
    return frozenset(t.lower() for t in name.split('_') if t.lower() not in _DOMAIN_STRIP)


def _pair_resets_to_clocks(resets: list[Reset], clocks: list[str]) -> list[Reset]:
    """Assign each reset to its most likely clock using NLTK text similarity."""
    if not clocks:
        return resets
    if len(clocks) == 1:
        paired = [Reset(name=r.name, active_low=r.active_low, clock=clocks[0]) for r in resets]
        for r in paired:
            print(f"info: reset '{r.name}' paired to clock '{r.clock}' (only clock)", file=sys.stderr)
        return paired

    paired = []
    for rst in resets:
        rst_tok = _domain_tokens(rst.name)
        best_clk, best_score = clocks[0], float('inf')

        for clk in clocks:
            clk_tok = _domain_tokens(clk)
            if rst_tok and clk_tok:
                score = jaccard_distance(rst_tok, clk_tok)
            else:
                # No meaningful tokens on one side — fall back to normalised edit distance.
                dist = edit_distance(rst.name.lower(), clk.lower())
                score = dist / max(len(rst.name), len(clk))

            if score < best_score:
                best_score, best_clk = score, clk

        confidence = 1.0 - best_score
        paired.append(Reset(name=rst.name, active_low=rst.active_low, clock=best_clk))
        print(
            f"info: reset '{rst.name}' paired to clock '{best_clk}' "
            f"(confidence {confidence:.2f})",
            file=sys.stderr,
        )

    return paired

_COMMENT = re.compile(r'--[^\n]*')

_ENTITY_START = re.compile(r'\bentity\s+(\w+)\s+is\b', re.IGNORECASE)

_LIB_DECL = re.compile(r'library\s+(\w+)\s*;', re.IGNORECASE)
_USE_DECL  = re.compile(r'use\s+([\w.]+)\s*;',  re.IGNORECASE)
_CTX_DECL  = re.compile(r'context\s+([\w.]+)\s*;', re.IGNORECASE)

# Libraries whose declarations are already emitted by the testbench templates.
_BUILTIN_LIBS = frozenset({'ieee', 'std', 'osvvm'})
_ENTITY_END   = re.compile(r'\bend\b', re.IGNORECASE)

# Match "generic (" or "port (" at the start of a section.
_SECTION_START = re.compile(r'\b(generic|port)\s*\(', re.IGNORECASE)

# One declaration line: names : [direction] type [:= default]
# Direction must be a standalone keyword (word boundary on both sides).
_DECL = re.compile(
    r'([\w][\w\s,]*?)\s*:\s*'
    r'(?:(in|out|inout|buffer|linkage)\b\s*)?'
    r'([^;:=\n]+?)'
    r'(?::=\s*([^;\n]+?))?\s*(?:;|$)',
    re.IGNORECASE,
)

_CLOCK_NAMES = re.compile(r'clk|clock', re.IGNORECASE)
_RESET_NAMES = re.compile(r'rst|reset', re.IGNORECASE)

# Matches active-low reset naming: nrst, n_rst, sys_nrst, rst_n, rstn, sys_rst_n, etc.
_ACTIVE_LOW_RESET = re.compile(
    r'(?:^|_)n_?(?:rst|reset)'   # n before rst/reset  (nrst, n_rst, sys_nrst)
    r'|(?:rst|reset)_?n(?:_|$)', # rst/reset before n  (rstn, rst_n, sys_rst_n_x)
    re.IGNORECASE,
)

# Naming-convention hints for direction inference.
_NAME_INPUT  = re.compile(r'^(?:i_|in_|input_)|(?:_i|_in|_input)$', re.IGNORECASE)
_NAME_OUTPUT = re.compile(r'^(?:o_|out_|output_)|(?:_o|_out|_output)$', re.IGNORECASE)


def _infer_direction_from_name(name: str) -> Direction | None:
    """Return the direction implied by naming convention, or None if ambiguous."""
    is_in  = bool(_NAME_INPUT.search(name))
    is_out = bool(_NAME_OUTPUT.search(name))
    if is_in and not is_out:
        return Direction.IN
    if is_out and not is_in:
        return Direction.OUT
    return None


def _extract_dut_libraries(preamble: str) -> list[str]:
    """Return non-standard library/use/context lines from the DUT file preamble.

    Filters out ieee, std, and osvvm — those are already in the testbench templates.
    """
    result: list[str] = []
    for line in preamble.splitlines():
        s = line.strip()
        if not s:
            continue
        lib_m = _LIB_DECL.match(s)
        if lib_m:
            if lib_m.group(1).lower() not in _BUILTIN_LIBS:
                result.append(f'library {lib_m.group(1)} ;')
            continue
        use_m = _USE_DECL.match(s)
        if use_m:
            if use_m.group(1).split('.')[0].lower() not in _BUILTIN_LIBS:
                result.append(f'  use {use_m.group(1)} ;')
            continue
        ctx_m = _CTX_DECL.match(s)
        if ctx_m:
            if ctx_m.group(1).split('.')[0].lower() not in _BUILTIN_LIBS:
                result.append(f'  context {ctx_m.group(1)} ;')
    return result


def _strip_comments(text: str) -> str:
    return _COMMENT.sub('', text)


def _extract_balanced(text: str, start: int) -> str:
    """Return the content inside the parentheses that open at text[start].

    start should point at the '(' character.
    """
    depth = 0
    i = start
    for i, ch in enumerate(text[start:], start):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return text[start + 1:]


def _parse_section(section: str, has_direction: bool) -> list[tuple[list[str], str | None, str, str | None]]:
    """Parse a generic or port section into (names, direction, type, default) tuples."""
    results = []
    for m in _DECL.finditer(section):
        raw_names, direction, vhdl_type, default = m.groups()
        names = [n.strip() for n in raw_names.split(',') if n.strip()]
        if not names or not vhdl_type.strip():
            continue
        if not has_direction:
            direction = None
        results.append((names, direction, vhdl_type.strip(), default.strip() if default else None))
    return results


def dut_from_f_file(f_file: Path) -> Path:
    """Return the last VHDL file path listed in a .f file.

    Lines that are blank or start with '#' or '//' are skipped.
    Paths are resolved relative to the .f file's directory.
    """
    base = f_file.parent
    paths: list[Path] = []
    try:
        lines = f_file.read_text(errors='replace').splitlines()
    except OSError as e:
        print(f"error: cannot read '{f_file}': {e}", file=sys.stderr)
        sys.exit(1)

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or stripped.startswith('//'):
            continue
        paths.append((base / stripped).resolve())

    if not paths:
        print(f"error: no VHDL files listed in '{f_file}'", file=sys.stderr)
        sys.exit(1)

    return paths[-1]


def extract(vhd_file: Path, library: str = "work") -> DutModel:
    """Parse a VHDL file and return a DutModel.

    Exits with a clear message if the entity cannot be found.
    Warns to stderr for declarations that could not be parsed.
    """
    try:
        raw = vhd_file.read_text(errors='replace')
    except OSError as e:
        print(f"error: cannot read '{vhd_file}': {e}", file=sys.stderr)
        sys.exit(1)

    text = _strip_comments(raw)

    em = _ENTITY_START.search(text)
    if not em:
        print(f"error: no entity declaration found in '{vhd_file}'", file=sys.stderr)
        sys.exit(1)

    entity_name = em.group(1)
    dut_libraries = _extract_dut_libraries(text[:em.start()])

    # Find the end of the entity block.
    end_m = _ENTITY_END.search(text, em.end())
    entity_body = text[em.end(): end_m.start() if end_m else len(text)]

    generics: list[Generic] = []
    ports: list[Port] = []
    clocks: list[str] = []
    resets: list[str] = []

    for sm in _SECTION_START.finditer(entity_body):
        kind = sm.group(1).lower()
        paren_start = sm.end() - 1  # points at '('
        section = _extract_balanced(entity_body, paren_start)

        if kind == 'generic':
            for names, _, vtype, default in _parse_section(section, has_direction=False):
                for name in names:
                    generics.append(Generic(name=name, type=vtype, default=default))

        elif kind == 'port':
            for names, raw_dir, vtype, _ in _parse_section(section, has_direction=True):
                direction = Direction(raw_dir.lower()) if raw_dir else Direction.IN
                for name in names:
                    ports.append(Port(name=name, direction=direction, type=vtype))
                    implied = _infer_direction_from_name(name)
                    if implied is not None and implied != direction:
                        print(
                            f"warning: port '{name}' is declared '{direction.value}' "
                            f"but name suggests '{implied.value}'",
                            file=sys.stderr,
                        )
                    if _CLOCK_NAMES.search(name) and direction == Direction.IN:
                        clocks.append(name)
                    elif _RESET_NAMES.search(name) and direction != Direction.OUT:
                        active_low = bool(_ACTIVE_LOW_RESET.search(name))
                        resets.append(Reset(name=name, active_low=active_low))
                        level = "active low" if active_low else "active high"
                        print(f"info: reset '{name}' detected as {level}", file=sys.stderr)

    if not ports:
        print(f"warning: no ports found in entity '{entity_name}'", file=sys.stderr)
    if not clocks:
        print(f"warning: no clock ports identified in entity '{entity_name}'", file=sys.stderr)
    if not resets:
        print(f"warning: no reset ports identified in entity '{entity_name}'", file=sys.stderr)

    resets = _pair_resets_to_clocks(resets, clocks)

    return DutModel(
        entity_name=entity_name,
        library=library,
        generics=generics,
        ports=ports,
        clocks=clocks,
        resets=resets,
        dut_libraries=dut_libraries,
    )
