"""Turn a unit's finished file reviews into what gets posted to GitHub.

Pure functions (no I/O) so the formatting rules are easy to test:

* check run: conclusion, title, markdown summary, full text, and line annotations for every located
  finding (annotations may point anywhere in the file);
* PR review: body plus inline comments, placed only on lines inside the diff — GitHub rejects the
  whole review with 422 if any comment targets a line outside a hunk.

Everything posted is derived from model output, which is derived from untrusted diffs, so it is
sanitized: @mentions are defused (no notification spam) and images are dropped (no tracking pixels).
"""

import re
from dataclasses import dataclass, field

from app.parsing.diff import commentable_lines
from app.review.findings import Finding, parse_findings

MAX_CHECK_TEXT = 65_535
MAX_ANNOTATION_MESSAGE = 60_000
MAX_REVIEW_BODY = 60_000
MAX_INLINE_COMMENTS = 30
REVIEW_MARKER = "<!-- codelens:unit:{unit_id} -->"

_LEVEL = {"blocker": "failure", "major": "warning", "minor": "notice", "nit": "notice"}
_MENTION = re.compile(r"(?<![\w`/])@(?=[A-Za-z0-9][A-Za-z0-9-]*)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_HTML_IMAGE = re.compile(r"<\s*(?:img|picture|source|video|audio|iframe)\b[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class FileReport:
    review_id: int
    path: str
    status: str  # complete | failed | skipped
    cache_hit: bool
    review_text: str = ""
    patch: str | None = None
    skip_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Annotation:
    path: str
    start_line: int
    end_line: int
    annotation_level: str
    title: str
    message: str


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    body: str


@dataclass
class UnitReport:
    conclusion: str
    title: str
    summary: str
    text: str
    review_body: str
    annotations: list[Annotation] = field(default_factory=list)
    inline_comments: list[InlineComment] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def sanitize(text: str) -> str:
    text = _IMAGE.sub(lambda m: m.group(1), text)
    text = _HTML_IMAGE.sub("", text)
    return _MENTION.sub("@\u200b", text)  # zero-width space: renders as "@user" but doesn't ping


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    notice = "\n\n… truncated — see the CodeLens dashboard for the full review."
    return text[: limit - len(notice)] + notice


def check_conclusion(findings: list[Finding], failed_files: int) -> str:
    """Decide the GitHub check-run conclusion for a reviewed commit or pull request.

    Returns one of "success", "neutral" or "failure". Branch protection can require this check,
    so the choice decides whether an AI review can block a merge.
    """
    # TODO(human): choose the merge-gating policy. The placeholder never blocks anything.
    return "neutral"


def _finding_path(finding: Finding, file_path: str) -> bool:
    """Models often shorten paths (``orders.py`` for ``shop/orders.py``)."""
    return finding.path is not None and (
        finding.path == file_path or file_path.endswith("/" + finding.path)
    )


def _severity_label(finding: Finding) -> str:
    return f"**[{finding.severity}]** " if finding.severity else ""


def _title(findings: list[Finding], failed: int, reviewed: int) -> str:
    if failed and not reviewed:
        return f"Review failed for {failed} file{'s' * (failed != 1)}"
    if not findings:
        title = "No issues found"
    else:
        counts = [
            f"{sum(f.severity == s for f in findings)} {s}"
            for s in ("blocker", "major", "minor", "nit")
        ]
        detail = ", ".join(c for c in counts if not c.startswith("0 "))
        title = f"{len(findings)} finding{'s' * (len(findings) != 1)}" + (
            f" ({detail})" if detail else ""
        )
    return title + (f" · {failed} file{'s' * (failed != 1)} failed" if failed else "")


def build_report(files: list[FileReport], *, unit_id: int, public_url: str) -> UnitReport:
    base = public_url.rstrip("/")
    all_findings: list[Finding] = []
    annotations: list[Annotation] = []
    inline: list[InlineComment] = []
    unplaced: list[str] = []
    rows: list[str] = []
    sections: list[str] = []
    failed = reviewed = cached = 0

    for file in sorted(files, key=lambda f: f.path):
        link = f"[`{file.path}`]({base}/reviews/{file.review_id})"
        if file.status == "skipped":
            rows.append(f"| {link} | skipped: {file.skip_reason or 'not reviewable'} | - |")
            continue
        if file.status != "complete":
            failed += 1
            rows.append(f"| {link} | review failed | - |")
            continue
        reviewed += 1
        cached += file.cache_hit
        findings = parse_findings(file.review_text)
        all_findings.extend(findings)
        rows.append(
            f"| {link} | reviewed{' (cached)' if file.cache_hit else ''} | {len(findings)} |"
        )
        sections.append(f"## {file.path}\n\n{sanitize(file.review_text.strip())}")

        diff_lines = commentable_lines(file.patch)
        for finding in findings:
            message = sanitize(finding.message)
            if finding.start_line is None or not _finding_path(finding, file.path):
                unplaced.append(f"- `{file.path}`: {_severity_label(finding)}{message}")
                continue
            end = finding.end_line or finding.start_line
            annotations.append(
                Annotation(
                    path=file.path,
                    start_line=finding.start_line,
                    end_line=end,
                    annotation_level=_LEVEL.get(finding.severity or "", "notice"),
                    title=f"CodeLens: {finding.severity or 'finding'}"[:255],
                    message=truncate(message, MAX_ANNOTATION_MESSAGE),
                )
            )
            target = (
                end
                if end in diff_lines
                else finding.start_line
                if finding.start_line in diff_lines
                else None
            )
            if target is not None and len(inline) < MAX_INLINE_COMMENTS:
                inline.append(
                    InlineComment(file.path, target, f"{_severity_label(finding)}{message}")
                )
            else:
                unplaced.append(
                    f"- `{file.path}:{finding.start_line}`: {_severity_label(finding)}{message}"
                )

    title = _title(all_findings, failed, reviewed)
    summary = "\n".join(
        [
            f"**CodeLens reviewed {reviewed} file{'s' * (reviewed != 1)}** · {title}"
            + (f" · {cached} served from cache" if cached else ""),
            "",
            "| File | Result | Findings |",
            "|---|---|---|",
            *rows,
        ]
    )
    body_parts = [summary]
    if unplaced:
        body_parts += ["", "**Findings outside the changed lines**", *unplaced]
    body_parts += ["", REVIEW_MARKER.format(unit_id=unit_id)]

    return UnitReport(
        conclusion=check_conclusion(all_findings, failed),
        title=title,
        summary=truncate(summary, MAX_CHECK_TEXT),
        text=truncate("\n\n".join(sections) or "No reviewable files.", MAX_CHECK_TEXT),
        review_body=truncate("\n".join(body_parts), MAX_REVIEW_BODY),
        annotations=annotations,
        inline_comments=inline,
        findings=all_findings,
    )
