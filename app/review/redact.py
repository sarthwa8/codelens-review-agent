"""Best-effort secret redaction before code leaves the network for a third-party LLM.

This is defense in depth, not a secret scanner: it catches the common, high-confidence token
formats and obvious ``password = "..."`` assignments.
"""

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED ANTHROPIC KEY]"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "[REDACTED OPENAI KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED SLACK TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED GOOGLE KEY]"),
]

_ASSIGNMENT = re.compile(
    r"""(?P<key>\b[\w.-]*(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)[\w.-]*\b\s*[:=]\s*)"""
    r"""(?P<quote>['"])(?P<value>[^'"\n]{8,})(?P=quote)""",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    for pattern, replacement in _PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n

    def _mask(match: re.Match[str]) -> str:
        return f"{match.group('key')}{match.group('quote')}[REDACTED]{match.group('quote')}"

    text, n = _ASSIGNMENT.subn(_mask, text)
    return text, count + n
