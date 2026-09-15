"""Unified-diff hunk parsing: which lines of the *new* file did a patch touch?"""

import re
from dataclasses import dataclass, field

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ChangedLines:
    added: set[int] = field(default_factory=set)  # 1-based line numbers in the new file
    # Deleted lines don't exist in the new file. For each run of deletions we record the new-file
    # line where the removal happened, so the enclosing function can still be located.
    deletion_anchors: set[int] = field(default_factory=set)

    @property
    def all(self) -> set[int]:
        return self.added | self.deletion_anchors

    def __bool__(self) -> bool:
        return bool(self.added or self.deletion_anchors)


def parse_patch(patch: str | None) -> ChangedLines:
    changed = ChangedLines()
    if not patch:
        return changed
    new_line = 0
    in_hunk = False
    previous_was_deletion = False
    for line in patch.splitlines():
        header = _HUNK_HEADER.match(line)
        if header:
            in_hunk = True
            new_line = int(header.group(3))
            previous_was_deletion = False
            continue
        if not in_hunk or line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith("+"):
            changed.added.add(new_line)
            new_line += 1
            previous_was_deletion = False
        elif line.startswith("-"):
            if not previous_was_deletion:
                changed.deletion_anchors.add(max(new_line, 1))
            previous_was_deletion = True
        else:
            new_line += 1
            previous_was_deletion = False
    return changed


def commentable_lines(patch: str | None) -> set[int]:
    """New-file line numbers a PR review comment may target on the RIGHT side of the diff.

    GitHub only accepts inline comments on lines inside a hunk (added or context lines); a single
    comment outside the diff makes the whole create-review request fail with 422.
    """
    lines: set[int] = set()
    new_line = 0
    in_hunk = False
    for line in (patch or "").splitlines():
        header = _HUNK_HEADER.match(line)
        if header:
            in_hunk = True
            new_line = int(header.group(3))
            continue
        if not in_hunk or line.startswith(("\\", "-")):
            continue
        lines.add(new_line)  # "+" added or " " context line
        new_line += 1
    return lines
