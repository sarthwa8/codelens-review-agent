"""tree-sitter based extraction of semantically coherent code chunks.

Two entry points:

* :func:`extract_changed_chunks` — for review: map the lines a patch touched to the smallest
  enclosing function/class/method, so the LLM sees whole units instead of +/- line fragments.
* :func:`extract_index_chunks` — for RAG indexing: split a whole file into top-level units.

Files without a supported grammar (or units that are too large) fall back to line windows.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache

from tree_sitter import Node, Parser

from app.parsing.diff import parse_patch
from app.parsing.languages import CONTAINER_NODE_TYPES, UNIT_NODE_TYPES, detect_language

logger = logging.getLogger(__name__)

MAX_UNIT_LINES = 150
WINDOW_CONTEXT = 6
INDEX_WINDOW_LINES = 60
INDEX_WINDOW_OVERLAP = 10
MAX_CHUNK_CHARS = 6_000

_FUNCTION_VALUE_TYPES = {"arrow_function", "function_expression", "function", "class"}


@dataclass(frozen=True)
class CodeChunk:
    path: str
    language: str
    kind: str  # tree-sitter node type, or "window"
    name: str | None
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    text: str
    scope: str | None = None  # enclosing units, e.g. "class OrderService"

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    @property
    def title(self) -> str:
        label = f"{self.kind} {self.name}" if self.name else self.kind
        return f"{label} ({self.location})" + (f" in {self.scope}" if self.scope else "")


@cache
def get_parser(language: str) -> Parser | None:
    if language not in UNIT_NODE_TYPES:
        return None
    try:
        import tree_sitter_language_pack as tslp

        return tslp.get_parser(language)
    except Exception:  # grammar missing/undownloadable → degrade to windows, never fail a review
        logger.warning("tree-sitter grammar unavailable for %s; using line windows", language)
        return None


def _text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8", errors="replace")


def _node_name(node: Node) -> str | None:
    name = _text(node.child_by_field_name("name"))
    if name:
        return name
    for child in node.named_children:
        if child.type in {"function_definition", "class_definition"}:  # decorated_definition
            return _node_name(child)
        if child.type in {"variable_declarator", "type_spec"}:  # const x = () => …, Go `type X`
            return _text(child.child_by_field_name("name"))
    return None


def _span(node: Node) -> tuple[int, int]:
    start = node.start_point.row + 1
    end = node.end_point.row + 1
    if node.end_point.column == 0 and end > start:
        end -= 1  # node ends at the very start of the following line
    return start, end


def _is_unit(node: Node, language: str) -> bool:
    if node.type not in UNIT_NODE_TYPES.get(language, ()):
        return False
    if node.type == "lexical_declaration":
        # Only `const handler = () => {}`-style declarations are units, not `const x = 1`.
        return any(
            (value := declarator.child_by_field_name("value")) is not None
            and value.type in _FUNCTION_VALUE_TYPES
            for declarator in node.named_children
            if declarator.type == "variable_declarator"
        )
    return True


def _unit_ancestors(node: Node, language: str) -> Iterator[Node]:
    """Units containing ``node`` (inclusive), innermost first. Decorators wrap their target."""
    current: Node | None = node
    while current is not None:
        if _is_unit(current, language):
            parent = current.parent
            if parent is not None and parent.type == "decorated_definition":
                current = parent
            yield current
        current = current.parent


def _node_at_line(root: Node, line: int, source_lines: list[str]) -> Node:
    row = line - 1
    text = source_lines[row] if 0 <= row < len(source_lines) else ""
    column = len(text) - len(text.lstrip())  # first non-blank char, not the parent's indentation
    return root.descendant_for_point_range((row, column), (row, column)) or root


def _scope(units: list[Node]) -> str | None:
    names = [f"{u.type} {_node_name(u)}" for u in reversed(units) if _node_name(u)]
    return " > ".join(names) or None


def _slice(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1 : end])[:MAX_CHUNK_CHARS]


def _merge_windows(lines_of_interest: list[int], context: int, total: int) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for line in sorted(set(lines_of_interest)):
        start, end = max(1, line - context), min(total, line + context)
        if windows and start <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


def _uncovered_segments(start: int, end: int, covered: set[int]) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    for line in range(start, end + 1):
        if line in covered:
            continue
        if segments and segments[-1][1] == line - 1:
            segments[-1] = (segments[-1][0], line)
        else:
            segments.append((line, line))
    return segments


def extract_changed_chunks(
    path: str, content: str, patch: str | None, *, max_unit_lines: int = MAX_UNIT_LINES
) -> list[CodeChunk]:
    lines = content.splitlines()
    total = max(len(lines), 1)
    changed = parse_patch(patch)
    # Blank added lines (spacing between functions) carry nothing to review; they would only
    # produce context windows that duplicate the neighbouring units.
    added = {line for line in changed.added if 0 < line <= len(lines) and lines[line - 1].strip()}
    targets = sorted({min(max(line, 1), total) for line in added | changed.deletion_anchors})
    if not targets:
        return []
    language = detect_language(path) or "text"
    parser = get_parser(language)

    units: dict[tuple[int, int], CodeChunk] = {}
    loose: list[tuple[int, str | None]] = []  # (line, scope) that need a window
    if parser is not None:
        root = parser.parse(content.encode("utf-8")).root_node
        for line in targets:
            ancestors = list(_unit_ancestors(_node_at_line(root, line, lines), language))
            if not ancestors:
                loose.append((line, None))
                continue
            innermost = ancestors[0]
            start, end = _span(innermost)
            if end - start + 1 > max_unit_lines:
                loose.append((line, _scope(ancestors)))
                continue
            if (start, end) not in units:
                units[(start, end)] = CodeChunk(
                    path=path,
                    language=language,
                    kind=innermost.type,
                    name=_node_name(innermost),
                    start_line=start,
                    end_line=end,
                    text=_slice(lines, start, end),
                    scope=_scope(ancestors[1:]),
                )
    else:
        loose = [(line, None) for line in targets]

    # A class-level edit selects the whole class; don't also send its methods a second time.
    units = {
        key: chunk
        for key, chunk in units.items()
        if not any(other != key and other[0] <= key[0] and key[1] <= other[1] for other in units)
    }

    scopes = dict(loose)
    covered = {ln for u in units.values() for ln in range(u.start_line, u.end_line + 1)}
    windows = []
    for start, end in _merge_windows([line for line, _ in loose], WINDOW_CONTEXT, total):
        # Clip the window around selected units so no line is sent to the model twice.
        for seg_start, seg_end in _uncovered_segments(start, end, covered):
            if not any(seg_start <= ln <= seg_end for ln in scopes):
                continue
            text = _slice(lines, seg_start, seg_end)
            if not text.strip():
                continue
            scope = next(
                (scopes[ln] for ln in range(seg_start, seg_end + 1) if scopes.get(ln)), None
            )
            windows.append(
                CodeChunk(path, language, "window", None, seg_start, seg_end, text, scope)
            )

    return sorted([*units.values(), *windows], key=lambda c: c.start_line)


def _file_windows(
    path: str, language: str, lines: list[str], start: int = 1, end: int | None = None
) -> list[CodeChunk]:
    end = end or len(lines)
    step = INDEX_WINDOW_LINES - INDEX_WINDOW_OVERLAP
    chunks = []
    for window_start in range(start, end + 1, step):
        window_end = min(end, window_start + INDEX_WINDOW_LINES - 1)
        text = _slice(lines, window_start, window_end)
        if text.strip():
            chunks.append(CodeChunk(path, language, "window", None, window_start, window_end, text))
        if window_end >= end:
            break
    return chunks


def extract_index_chunks(
    path: str, content: str, *, max_unit_lines: int = MAX_UNIT_LINES
) -> list[CodeChunk]:
    lines = content.splitlines()
    if not lines:
        return []
    language = detect_language(path) or "text"
    parser = get_parser(language)
    if parser is None:
        return _file_windows(path, language, lines)

    chunks: list[CodeChunk] = []

    def walk(node: Node, scope: str | None) -> None:
        for child in node.named_children:
            if not _is_unit(child, language):
                walk(child, scope)
                continue
            start, end = _span(child)
            name = _node_name(child)
            if end - start + 1 <= max_unit_lines:
                chunks.append(
                    CodeChunk(
                        path,
                        language,
                        child.type,
                        name,
                        start,
                        end,
                        _slice(lines, start, end),
                        scope,
                    )
                )
            elif child.type in CONTAINER_NODE_TYPES:
                inner_scope = f"{scope} > {child.type} {name}" if scope else f"{child.type} {name}"
                walk(child, inner_scope)
            else:
                chunks.extend(_file_windows(path, language, lines, start, end))

    walk(parser.parse(content.encode("utf-8")).root_node, None)
    return chunks or _file_windows(path, language, lines)
