"""One verdict per layout defect, however many tests, engines and shares found it.

A browser test records each finding of a failed check as a ``Finding``
(`BrowserGate`), and the run's parent gathers them (`run_report.Findings`).
A defect is one rule broken where one Part draws, the one place to fix, so
the findings are grouped by rule and Part, or by what they say with their
numbers masked (``signature``) where no Part draws what they name, and each
group is one verdict: its rule, its Part and that Part's file, and each
finding with every place it was found. A run of every share writes its findings to a folder instead
(``FOLDER``), and a run of this module over the folders of every share says
the verdicts and fails once for each, as the end of a whole run does.

Plain Python, so CI reads the shares' folders with nothing installed.
"""

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

# A folder each share of a split run writes its findings to, which leaves
# their verdict to the run of this module over every share's folder.
FOLDER = "HQ_BROWSER_FINDINGS"


@dataclass(frozen=True)
class Finding:
    """What one check found once: the check, what it said, where (engine,
    class, page, width, state), the Part that draws what it names and that
    Part's file, the test that found it, and the look that finds it again."""

    check: str
    said: str
    place: str
    part: str = ""
    fix: str = ""
    test: str = ""
    again: str = ""


def signature(check: str, said: object) -> tuple[str, str]:
    """What a finding says, its measures aside: the same defect at another
    width or on another load differs only in its numbers."""

    text = said if isinstance(said, str) else json.dumps(said, sort_keys=True)
    return check, re.sub(r"\d+", "#", text)


@dataclass(frozen=True)
class Verdict:
    """Every finding of one defect: one rule broken where one Part draws, or
    one finding alike but for its numbers where no Part draws it."""

    found: tuple[Finding, ...]

    @property
    def first(self) -> Finding:
        return self.found[0]

    def places(self) -> list[str]:
        return list(dict.fromkeys(finding.place for finding in self.found))

    def head(self) -> str:
        part = f"Part {self.first.part}, " if self.first.part else ""
        places = self.places()
        return f"{part}rule {self.first.check}: {len(places)} place{'s' * (len(places) != 1)}"

    def __str__(self) -> str:
        first = self.first
        lines = [self.head(), f"  Fix at: {first.fix or 'the page, since no Part owns what it names'}"]
        alike: dict[tuple[str, str], list[Finding]] = {}
        for finding in self.found:
            alike.setdefault(signature(finding.check, finding.said), []).append(finding)
        for said in alike.values():
            lines += [f"  {said[0].said}", *(f"    at {place}" for place in dict.fromkeys(f.place for f in said))]
        if first.again:
            lines.append(f"  Found again by: {first.again}")
        return "\n".join(lines)


def verdicts(findings: Iterable[Finding]) -> list[Verdict]:
    """The findings as one verdict per defect, in the order each was first
    found: by rule and the Part's file where a Part draws what they name,
    else by what they say (``signature``)."""

    grouped: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        key = (finding.check, finding.fix) if finding.fix else signature(finding.check, finding.said)
        grouped.setdefault(key, []).append(finding)
    return [Verdict(tuple(found)) for found in grouped.values()]


def write(folder: str, findings: list[Finding]) -> Path:
    """``findings`` written to a file of their own in ``folder``."""

    Path(folder).mkdir(parents=True, exist_ok=True)
    handle, path = tempfile.mkstemp(suffix=".json", dir=folder)
    with os.fdopen(handle, "w") as record:
        json.dump([asdict(finding) for finding in findings], record)
    return Path(path)


def read(folder: str) -> list[Finding]:
    """Every finding written to ``folder`` or a folder in it, in file order."""

    return [
        Finding(**each)
        for path in sorted(Path(folder).rglob("*.json"))
        for each in json.loads(path.read_text(encoding="utf-8"))
    ]


def _escaped(text: str, marks: str) -> str:
    for mark in ("%", "\r", "\n", *marks):
        text = text.replace(mark, f"%{ord(mark):02X}")
    return text


def main(argv: list[str]) -> int:
    """Say each defect the findings under the folder named found once, and
    fail when there is one."""

    (folder,) = argv
    found = verdicts(read(folder))
    for verdict in found:
        # GitHub's annotation, its title escaped as a property and its text as a message.
        print(f"::error title={_escaped(verdict.head(), ':,')}::{_escaped(verdict.first.said, '')}")
        print(verdict, end="\n\n")
    print(f"{len(found)} layout defect{'s' * (len(found) != 1)}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
