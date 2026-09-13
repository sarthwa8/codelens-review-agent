"""Language detection and the tree-sitter node types that count as a reviewable unit."""

from pathlib import PurePosixPath

EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
}

# Languages we review with line-window chunks instead of AST units.
PLAIN_TEXT_EXTENSIONS = {
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".php", ".scala", ".sh",
    ".sql", ".yaml", ".yml", ".toml", ".json", ".md", ".html", ".css", ".scss", ".vue",
    ".svelte", ".dockerfile", ".tf",
}  # fmt: skip

PLAIN_TEXT_FILENAMES = {"Dockerfile", "Makefile"}

# Structural units per grammar. Order doesn't matter; the smallest enclosing unit wins.
UNIT_NODE_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"function_definition", "class_definition", "decorated_definition"}),
    "javascript": frozenset(
        {
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
            "method_definition",
            "lexical_declaration",  # const foo = () => {...}
        }
    ),
    "typescript": frozenset(
        {
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
            "abstract_class_declaration",
            "method_definition",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
            "lexical_declaration",
        }
    ),
    "go": frozenset({"function_declaration", "method_declaration", "type_declaration"}),
    "java": frozenset(
        {
            "method_declaration",
            "constructor_declaration",
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        }
    ),
    "rust": frozenset(
        {"function_item", "impl_item", "struct_item", "enum_item", "trait_item", "mod_item"}
    ),
    "ruby": frozenset({"method", "singleton_method", "class", "module"}),
}
UNIT_NODE_TYPES["tsx"] = UNIT_NODE_TYPES["typescript"]

# Units that are containers: when too large, descend into them rather than windowing.
CONTAINER_NODE_TYPES = frozenset(
    {
        "class_definition",
        "decorated_definition",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "impl_item",
        "trait_item",
        "mod_item",
        "class",
        "module",
    }
)

GENERATED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
    "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock",
}  # fmt: skip

IGNORED_DIRS = {
    "node_modules", "vendor", "dist", "build", ".git", "__pycache__", ".venv", "venv",
    "target", ".next", "coverage", "third_party",
}  # fmt: skip


def detect_language(path: str) -> str | None:
    """Return a tree-sitter grammar name, "text" for reviewable non-AST files, or None."""
    p = PurePosixPath(path)
    suffix = p.suffix.lower()
    if suffix in EXTENSION_LANGUAGES:
        return EXTENSION_LANGUAGES[suffix]
    if suffix in PLAIN_TEXT_EXTENSIONS or p.name in PLAIN_TEXT_FILENAMES:
        return "text"
    return None


def skip_reason_for_path(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.name in GENERATED_FILENAMES:
        return "generated lockfile"
    if any(part in IGNORED_DIRS for part in p.parts[:-1]):
        return "vendored or build output directory"
    if p.name.endswith((".min.js", ".min.css", ".map")):
        return "minified or source-map file"
    if detect_language(path) is None:
        return "unsupported file type"
    return None
