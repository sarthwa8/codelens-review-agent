"""Parse the "### Findings" section of a review into structured findings.

The prompt asks for bullets like ``**[major]** `path:12` — problem — fix``, but models drift: missing
brackets or bold, numbered lists, hyphens instead of em dashes, ``L12``, ranges, and bullets wrapped
over several lines. The parser is deliberately lenient; anything it can't place becomes a finding with
no location, which is still shown in summaries but never posted as an inline comment.
"""

import re
from dataclasses import dataclass

SEVERITIES = ("blocker", "major", "minor", "nit")

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_SEVERITY = re.compile(
    r"^(?:\*\*|__)?\s*\[?\s*(blocker|major|minor|nit)\s*\]?\s*(?:\*\*|__)?\s*[:\-\u2013\u2014]?\s*",
    re.IGNORECASE,
)
_BACKTICK = re.compile(r"`([^`]+)`")
_LOCATION = re.compile(r"([\w./@+-]*[\w-]\.[A-Za-z0-9]+):L?(\d+)(?:\s*[-\u2013]\s*L?(\d+))?")
_NO_ISSUES = re.compile(r"^\s*(?:\*\*)?no (?:issues|findings)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    severity: str | None
    message: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None


def _findings_section(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading and heading.group(1).lower().startswith("findings"):
            section = []
            for following in lines[i + 1 :]:
                if _HEADING.match(following):
                    break
                section.append(following)
            return section
    return []


def _bullets(section: list[str]) -> list[str]:
    items: list[str] = []
    for line in section:
        bullet = _BULLET.match(line)
        if bullet:
            items.append(bullet.group(1).strip())
        elif line.strip() and items:
            items[-1] += " " + line.strip()  # wrapped continuation of the previous bullet
    return items


def _location(text: str) -> tuple[str | None, int | None, int | None, re.Match[str] | None]:
    for span in _BACKTICK.finditer(text):
        found = _LOCATION.search(span.group(1))
        if found:
            return (*_unpack(found), span)
    found = _LOCATION.search(text)
    return (*_unpack(found), None) if found else (None, None, None, None)


def _unpack(found: re.Match[str]) -> tuple[str, int, int]:
    start = int(found.group(2))
    end = int(found.group(3)) if found.group(3) else start
    return found.group(1), min(start, end), max(start, end)


def parse_findings(markdown: str) -> list[Finding]:
    findings = []
    for item in _bullets(_findings_section(markdown)):
        if _NO_ISSUES.match(item):
            continue
        severity = None
        severity_match = _SEVERITY.match(item)
        if severity_match:
            severity = severity_match.group(1).lower()
            item = item[severity_match.end() :]
        path, start, end, span = _location(item)
        message = item
        if span is not None and not item[: span.start()].strip():
            # Drop a leading `path:line` span and the separator after it; the location is structured now.
            message = re.sub(r"^\s*[:\-\u2013\u2014]\s*", "", item[span.end() :])
        findings.append(Finding(severity, message.strip() or item.strip(), path, start, end))
    return findings
