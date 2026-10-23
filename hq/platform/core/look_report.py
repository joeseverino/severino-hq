"""What dev:look says once it has looked: its findings page by page, the
verdict, and how they are matched against a base's, so a finding is new for
what a change drew and never for which run drew it."""

import math
import re
from collections import Counter
from itertools import groupby, zip_longest

# What a look found, by page and width, each finding a line.
type Found = dict[tuple[str, int], list[str]]
# What a look measured, by page and width: `columns` and `eval`, as printed.
type Measured = dict[tuple[str, int], dict[str, str]]


def block(name: str, found: dict[int, list[str]], rule: str) -> str:
    """One page's findings, under its name, width by width."""

    lines = [name]
    for width, said in found.items():
        verdict = f"{len(said)} found" if said else "holds" if rule else "looked at"
        lines.append(f"  {width}: {verdict}")
        lines.extend(f"    {line}" for line in said)
    return "\n".join(lines)


def grouped(found: Found, rule: str) -> str:
    """Findings page by page, in the order they were looked at."""

    return "\n".join(
        block(name, {width: said for (_, width), said in entries}, rule)
        for name, entries in groupby(found.items(), key=lambda entry: entry[0][0])
    )


def failed_pages(found: Found) -> list[str]:
    return list(dict.fromkeys(name for (name, _), said in found.items() if said))


def verdict_line(rule: str, found: Found) -> str:
    pages = list(dict.fromkeys(name for name, _ in found))
    widths = ", ".join(map(str, dict.fromkeys(width for _, width in found)))
    failed = failed_pages(found)
    if not failed:
        return f"{rule} holds on every page at {widths}"
    return f"{rule} fails on {len(failed)} of {len(pages)} pages: {', '.join(failed)}"


# What a finding names that is drawn anew on every run, so no fact of the
# layout: an origin panel's element id (its origin's key slugged, often with a
# record's id in it), a UUID or the start of one (a finding cuts its text
# short), and a word of hex holding a digit.
_DRAWN_ANEW = (
    (re.compile(r"#origin-[\w-]+"), "#origin-…"),
    (re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{1,12}){1,4}", re.IGNORECASE), "…"),
)
_HEX = re.compile(r"\b[0-9a-f]*\d[0-9a-f]*\b", re.IGNORECASE)
# The words a finding ends on after its measure: the record whose row the box
# is in, which is which row has the fault rather than the fault.
_RECORD = re.compile(r"(\d(?:\.\d+)?px): .*$")
# A size or a ratio is kept to its power of four, so a box 412px wide is
# a box 430px wide, and one 900px past its cell is not one 2px past. Any other
# number (a row's place, a count) is taken out.
_MEASURE = re.compile(r"(\d+(?:\.\d+)?)(px|:1)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# Under this a measure is a pixel's jitter, or a ratio short of any bar.
_SMALL = 4


def magnitude(measure: re.Match) -> str:
    """A measure as its power of four, spelled in letters so the numbers
    taken out after it leave it be."""

    value = float(measure.group(1))
    order = "small" if value < _SMALL else "four to the " + "i" * math.floor(math.log(value, 4))
    return f"~{order}{measure.group(2)}"


def finding_key(line: str) -> str:
    """A finding as it is matched with the base's: what is drawn anew on every
    run taken out, each size and ratio kept to its order of magnitude, and
    every other number taken out, and the record named after a measure. A
    finding whose row moved, whose overflow grew by a few pixels, or that
    names another record is the same finding; one whose overflow grew many
    times over is not."""

    for drawn, mark in _DRAWN_ANEW:
        line = drawn.sub(mark, line)
    line = _RECORD.sub(r"\1", line)
    line = _MEASURE.sub(magnitude, line)
    line = _HEX.sub("#", line)
    return _NUMBER.sub("#", line)


def unmatched(lines: list[str], against: list[str]) -> list[str]:
    """The lines with no counterpart in ``against``, matched by their keys as
    a multiset: one counterpart each, so two alike findings against one leave
    one unmatched."""

    left = Counter(map(finding_key, against))
    unmatched = []
    for line in lines:
        key = finding_key(line)
        if left[key]:
            left[key] -= 1
        else:
            unmatched.append(line)
    return unmatched


def compare(base: Found, branch: Found) -> tuple[Found, Found]:
    """What the branch finds that the base does not, and what the base found
    on a page the branch looked at too that the branch no longer does."""

    new = {key: unmatched(said, base.get(key, [])) for key, said in branch.items()}
    fixed = {key: unmatched(base[key], said) for key, said in branch.items() if key in base}
    return {key: said for key, said in new.items() if said}, {key: said for key, said in fixed.items() if said}


def against(ref: str, rule: str, base: Found, branch: Found) -> tuple[str, list[str], bool]:
    """A look against a base: the whole report, new and fixed findings page by
    page and then all this checkout found; what is printed, a line for each new finding and the verdict last;
    and whether it found anything new."""

    new, fixed = compare(base, branch)
    parts = []
    if new:
        parts.append(f"New against {ref}:\n{grouped(new, rule)}")
    if fixed:
        parts.append(f"Fixed against {ref}:\n{grouped(fixed, rule)}")
    parts.append(f"All found here:\n{grouped(branch, rule)}")
    count = sum(map(len, new.values()))
    fixes = f"; {sum(map(len, fixed.values()))} fixed" if fixed else ""
    if new:
        verdict = f"{rule} against {ref}: {count} new on {', '.join(failed_pages(new))}{fixes}"
    else:
        verdict = f"{rule} against {ref}: nothing new on {len(dict.fromkeys(n for n, _ in branch))} pages{fixes}"
    printed = [f"new: {name} at {width}: {line}" for (name, width), said in new.items() for line in said]
    return "\n".join([*parts, verdict]), [*printed, verdict], bool(new)


def closing(rule: str, found: Found, base: Found | None, ref: str) -> tuple[str, list[str], bool]:
    """How a run with a rule closes: the report kept in ``REPORT``, every
    finding page by page; what it prints, a line for each new finding against
    ``base`` and the verdict last; and whether it failed. Without a base,
    every finding is in the report and only the verdict is printed."""

    if base is not None:
        return against(ref, rule, base, found)
    verdict = verdict_line(rule, found)
    return f"{grouped(found, rule)}\n{verdict}", [verdict], bool(failed_pages(found))


def beside(ref: str, ours: str, theirs: str | None) -> str:
    """What this checkout measured, each line followed by the one the base
    measured in its place when the two differ, marked with ``ref``."""

    if theirs is None:
        return f"{ours}\n{ref}: (not measured)"
    lines = []
    for here, there in zip_longest(ours.splitlines(), theirs.splitlines()):
        if here is not None:
            lines.append(here)
        if there != here:
            lines.append(f"{ref}: {'(no such line)' if there is None else there.strip()}")
    return "\n".join(lines)
