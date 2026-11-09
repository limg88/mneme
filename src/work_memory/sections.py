"""Deterministic source slices. Offsets always refer to the unchanged observation."""

import hashlib
import re
from bisect import bisect_right
from dataclasses import dataclass

MAX_SECTION_CHARS = 12_000
PLANNER_VERSION = "sections-v1"


@dataclass(frozen=True)
class Section:
    id: str
    label: str
    start: int
    end: int


def plan_sections(content: str, *, max_chars: int = MAX_SECTION_CHARS) -> list[Section]:
    if not 1000 <= max_chars <= MAX_SECTION_CHARS:
        raise ValueError("Section size must be between 1000 and 12000 characters")
    headings = list(re.finditer(r"^#{1,3} [^\n]+", content, re.M))
    boundaries = [0]
    # Small documents retain their context in a single request. For long documents,
    # do not split the introduction from the first heading into a paid metadata-only call.
    if len(content) > max_chars:
        for heading in headings[1:]:
            if len(headings) <= 128 or heading.start() - boundaries[-1] >= 4_000:
                boundaries.append(heading.start())
    boundaries.append(len(content))
    sections = []
    heading_offsets = [m.start() for m in headings]
    for lower, upper in zip(boundaries, boundaries[1:], strict=False):
        start = lower
        while start < upper:
            end = min(start + max_chars, upper)
            if end < upper:
                newline = content.rfind("\n", start + max_chars // 2, end)
                if newline >= 0:
                    end = newline + 1
            position = bisect_right(heading_offsets, start) - 1
            heading = headings[max(0, position)] if headings else None
            label = heading[0] if heading else "Document"
            key = hashlib.sha256(
                f"{PLANNER_VERSION}:{start}:{end}:{content[start:end]}".encode()
            ).hexdigest()
            sections.append(Section(key, label, start, end))
            start = end
    return sections
