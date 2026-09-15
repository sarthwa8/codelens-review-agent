import pytest

from app.parsing.diff import parse_patch
from app.parsing.languages import detect_language, skip_reason_for_path
from app.parsing.treesitter import extract_changed_chunks, extract_index_chunks

PY_SOURCE = """import os


class OrderService:
    rate = 3

    @cached
    def total(self, items):
        s = 0
        for i in items:
            s += i.price * i.qty
        log(s)
        return s

    def empty(self):
        return []


def helper(x):
    return x * 3
"""


def test_parse_patch_tracks_added_lines_and_deletion_anchors() -> None:
    patch = "@@ -10,3 +10,4 @@ def f():\n a\n-b\n+c\n+d\n e\n@@ -30,3 +31,2 @@\n x\n-y\n z\n"
    changed = parse_patch(patch)
    assert changed.added == {11, 12}
    assert 11 in changed.deletion_anchors  # the "-b" run sits where "+c" now is
    assert 32 in changed.deletion_anchors  # pure deletion hunk: anchored in the new file
    assert not parse_patch("")


def test_parse_patch_ignores_no_newline_marker() -> None:
    changed = parse_patch("@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n")
    assert changed.added == {1}


def test_changed_line_maps_to_enclosing_method_not_whole_class() -> None:
    patch = "@@ -10,2 +10,3 @@\n         for i in items:\n-            s += i.price\n+            s += i.price * i.qty\n+        log(s)\n"
    [chunk] = extract_changed_chunks("svc/orders.py", PY_SOURCE, patch)
    assert chunk.name == "total"
    assert chunk.kind == "decorated_definition"  # decorator travels with the function
    assert chunk.scope == "class_definition OrderService"
    assert chunk.text.lstrip().startswith("@cached")
    assert (chunk.start_line, chunk.end_line) == (7, 13)


def test_class_level_change_selects_class_once_without_duplicate_methods() -> None:
    patch = "@@ -5 +5 @@\n-    rate = 2\n+    rate = 3\n@@ -11 +11 @@\n-            s += i.price\n+            s += i.price * i.qty\n"
    chunks = extract_changed_chunks("svc/orders.py", PY_SOURCE, patch)
    assert [c.name for c in chunks] == ["OrderService"]


def test_top_level_change_outside_any_unit_becomes_window() -> None:
    [chunk] = extract_changed_chunks(
        "svc/orders.py", PY_SOURCE, "@@ -1 +1 @@\n-import sys\n+import os\n"
    )
    assert chunk.kind == "window" and chunk.start_line == 1


def test_oversized_unit_falls_back_to_window_with_scope() -> None:
    patch = "@@ -11 +11 @@\n-            s += i.price\n+            s += i.price * i.qty\n"
    [chunk] = extract_changed_chunks("svc/orders.py", PY_SOURCE, patch, max_unit_lines=3)
    assert chunk.kind == "window"
    assert chunk.scope is not None and "total" in chunk.scope


@pytest.mark.parametrize(
    ("path", "source", "line", "expected"),
    [
        (
            "api/handler.ts",
            "export const handler = async (req: Req) => {\n  return req.body;\n};\n",
            2,
            "handler",
        ),
        ("main.go", "package main\n\nfunc (s S) Run() int {\n\treturn 1\n}\n", 4, "Run"),
        ("Svc.java", "class Svc {\n  int run() {\n    return 1;\n  }\n}\n", 3, "run"),
        ("lib.rs", "fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n", 2, "add"),
    ],
)
def test_multi_language_unit_detection(path: str, source: str, line: int, expected: str) -> None:
    patch = f"@@ -{line} +{line} @@\n-old\n+new\n"
    [chunk] = extract_changed_chunks(path, source, patch)
    assert chunk.name == expected


def test_unknown_language_uses_windows() -> None:
    source = "\n".join(f"line {i}" for i in range(1, 41))
    [chunk] = extract_changed_chunks("notes.md", source, "@@ -20 +20 @@\n-x\n+line 20\n")
    assert chunk.kind == "window" and chunk.start_line <= 20 <= chunk.end_line


def test_index_chunks_split_large_classes_into_methods() -> None:
    chunks = extract_index_chunks("svc/orders.py", PY_SOURCE, max_unit_lines=8)
    names = {c.name for c in chunks}
    assert {"total", "empty", "helper"} <= names
    assert "OrderService" not in names


def test_language_detection_and_skip_rules() -> None:
    assert detect_language("web/App.tsx") == "tsx"
    assert detect_language("infra/main.tf") == "text"
    assert detect_language("logo.png") is None
    assert skip_reason_for_path("package-lock.json") == "generated lockfile"
    assert skip_reason_for_path("node_modules/x/index.js") == "vendored or build output directory"
    assert skip_reason_for_path("static/app.min.js") == "minified or source-map file"
    assert skip_reason_for_path("logo.png") == "unsupported file type"
    assert skip_reason_for_path("src/app.py") is None


def test_blank_added_lines_do_not_create_overlapping_windows() -> None:
    source = "def charge():\n    return 1\n\n\ndef refund(order):\n    if order.total < 0:\n        raise ValueError\n    return 2\n"
    patch = "@@ -2,0 +3,6 @@\n+\n+\n+def refund(order):\n+    if order.total < 0:\n+        raise ValueError\n+    return 2\n"
    chunks = extract_changed_chunks("pay.py", source, patch)
    assert [(c.kind, c.name) for c in chunks] == [("function_definition", "refund")]


def test_windows_are_clipped_around_units_instead_of_overlapping_them() -> None:
    source = "import os\nX = 1\n\ndef f():\n    return X\n"
    patch = "@@ -1,5 +1,5 @@\n-import sys\n+import os\n X = 1\n \n def f():\n-    return 0\n+    return X\n"
    chunks = extract_changed_chunks("m.py", source, patch)
    lines = [ln for c in chunks for ln in range(c.start_line, c.end_line + 1)]
    assert len(lines) == len(set(lines)), "a line was included in two chunks"
    assert {c.kind for c in chunks} == {"window", "function_definition"}


def test_whitespace_only_change_yields_no_chunks() -> None:
    assert extract_changed_chunks("m.py", "a = 1\n\n", "@@ -1,1 +1,2 @@\n a = 1\n+\n") == []


def test_large_file_extraction_survives_garbage_collection() -> None:
    """Regression: tree-sitter 0.26.0 corrupted memory on large trees and segfaulted during GC."""
    import gc

    source = "\n".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(600))
    patch = "@@ -0,0 +1,1800 @@\n" + "\n".join("+" + line for line in source.splitlines())
    for _ in range(3):
        chunks = extract_changed_chunks("big.py", source, patch)
        gc.collect()
    assert len(chunks) == 600
    assert {c.name for c in chunks} == {f"f{i}" for i in range(600)}


def test_commentable_lines_are_added_and_context_lines_inside_hunks() -> None:
    from app.parsing.diff import commentable_lines

    patch = (
        "@@ -10,4 +10,5 @@ def f():\n"
        " a\n"  # 10 context
        "-b\n"  # deleted: not on the RIGHT side
        "+c\n"  # 11
        "+d\n"  # 12
        " e\n"  # 13
        "\\ No newline at end of file\n"
        "@@ -40,2 +41,2 @@\n"
        "-x\n"
        "+y\n"  # 41
        " z\n"  # 42
    )
    assert commentable_lines(patch) == {10, 11, 12, 13, 41, 42}
    assert commentable_lines(None) == set()
    assert 30 not in commentable_lines(patch)  # between hunks: GitHub would reject the review
