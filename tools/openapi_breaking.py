"""lint:openapi-breaking: a change to the machine API's document that breaks a client fails, unless
the exemption file admits that change with a reason.

oasdiff compares the document with the base's copy and judges each change by its own checks. The
exemption file holds two kinds of line, each followed by ``Reason:`` and why clients can take it:

- An entry is the line the gate printed for one change, handed to oasdiff as its ``--err-ignore``
  list. oasdiff drops an error only for an entry naming its exact path, operation and text, so an
  entry admits one change of one operation.
- A severity line, ``<check-id> info``, is handed to oasdiff as its ``--severity-levels`` file and
  lowers every change that check finds to info, which fails nothing: one line admits a correction
  made across many operations. The gate prints how many changes each one lowered.

Only the lines this change adds are handed to oasdiff. A line the base already holds admitted a
break the base already made, so it admits nothing here. This module holds every line oasdiff could
read to its reason, and every severity line to info, before oasdiff runs.

    python -m tools.openapi_breaking [base]

The base is fast's: the merge base with the ref named, or else with the branch HEAD lands on.
"""

import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from tools.gate.chain import ROOT, merge_base

DOCUMENT = "hq/platform/api/hq-api.openapi.json"
EXEMPTIONS = "hq/platform/api/hq-api.breaking-exemptions.md"
REASON = "Reason:"
# The ``level`` oasdiff's JSON gives a change (checker.ERR and checker.WARN).
ERR, WARN = 3, 2
# A check id followed by a level, as oasdiff's GetSeverityLevels (checker/level.go) reads a line.
SEVERITY = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)+)\s+(err|warn|info|none)(?:\s|$)", re.IGNORECASE)
LOWERED = "info"


def lowered(line: str) -> str | None:
    """The check a severity line lowers, or None for any other line."""

    found = SEVERITY.match(line)
    return found[1] if found else None


def unexplained(lines: Iterable[str]) -> list[int]:
    """The numbers of the lines oasdiff could read that give no reason after ``Reason:``.

    oasdiff (checker/*_change.go, MatchIgnore) matches an entry, lowercased, to an operation's
    change by a field starting with a slash, and to a shared schema's or a security scheme's change
    by the word components or security in it; a severity line names a check; any other line matches
    nothing."""

    found = []
    for number, line in enumerate(lines, start=1):
        text = line.lower()
        matchable = (
            any(field.startswith("/") for field in text.split())
            or any(word in text for word in ("components", "security"))
            or lowered(line)
        )
        if matchable and not line.partition(REASON)[2].strip():
            found.append(number)
    return found


def misleveled(lines: Iterable[str]) -> list[int]:
    """The numbers of the severity lines that set their check to anything but info: a line here
    only lowers a check to a level that fails nothing."""

    return [
        number for number, line in enumerate(lines, start=1) if (found := SEVERITY.match(line)) and found[2] != LOWERED
    ]


def main(argv: list[str]) -> int:
    lines = (ROOT / EXEMPTIONS).read_text().splitlines()
    refused = [
        *(f"{n}: an exemption says why clients can take it, after {REASON}" for n in unexplained(lines)),
        *(f"{n}: a severity line lowers its check to {LOWERED}" for n in misleveled(lines)),
    ]
    for refusal in refused:
        print(f"{EXEMPTIONS}:{refusal}", file=sys.stderr)
    if refused:
        return 1
    base = merge_base(argv[0] if argv and argv[0] else None)
    print(f"lint:openapi-breaking: {DOCUMENT} against {base[:9]}", flush=True)
    new = added(lines, base, ROOT)
    checks = [check for line in new if (check := lowered(line))]
    with (
        tempfile.NamedTemporaryFile("w", suffix=".md") as ignored,
        tempfile.NamedTemporaryFile("w", suffix=".txt") as levels,
    ):
        ignored.write("".join(f"{line}\n" for line in new if not lowered(line)))
        levels.write("".join(f"{check} {LOWERED}\n" for check in checks))
        ignored.flush()
        levels.flush()
        return judge(f"{base}:{DOCUMENT}", DOCUMENT, ROOT, ignored.name, levels.name, checks)


def added(lines: list[str], base: str, cwd: Path) -> list[str]:
    """The lines of the exemption file the base does not hold: the breaks this change admits."""

    held = subprocess.run(["git", "show", f"{base}:{EXEMPTIONS}"], cwd=cwd, capture_output=True, text=True, check=False)
    before = set(held.stdout.splitlines()) if held.returncode == 0 else set()
    return [line for line in lines if line not in before]


def breaking(base: str, revision: str, cwd: Path, options: list[str]) -> tuple[subprocess.CompletedProcess, list]:
    """oasdiff's run of its breaking checks, and the changes it found at warning and above."""

    found = subprocess.run(
        ["oasdiff", "breaking", "--format", "json", *options, base, revision],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return found, json.loads(found.stdout or "[]") if found.returncode == 0 else []


def judge(base: str, revision: str, cwd: Path, exemptions: str, levels: str, checks: Sequence[str] = ()) -> int:
    """Fail on each change from ``base`` to ``revision`` that breaks a client, unless an entry in
    ``exemptions`` admits it or a severity line in ``levels`` lowers its check.

    Each lowered check's count of changes is the run without ``levels`` holding them. The errors
    print last, with their count, and the warnings only as a count, so a run cut to its tail still
    shows what it failed on. ``changelog --level ERR`` prints the errors alone, in the line an
    exemption copies; oasdiff's JSON gives each change's level to count."""

    ignore = ["--err-ignore", exemptions]
    options = [*ignore, "--severity-levels", levels]
    if checks:
        unlowered, changes = breaking(base, revision, cwd, ignore)
        if unlowered.returncode:
            print(unlowered.stderr, end="", file=sys.stderr)
            return unlowered.returncode
        counts = Counter(change["id"] for change in changes)
        for check in checks:
            print(
                f"lint:openapi-breaking: `{check} {LOWERED}` lowered {counts[check]} changes to {LOWERED}", flush=True
            )
    found, changes = breaking(base, revision, cwd, options)
    if found.returncode:
        print(found.stderr, end="", file=sys.stderr)
        return found.returncode
    levels_found = Counter(change["level"] for change in changes)
    if levels_found[WARN]:
        listing = f"oasdiff breaking --format singleline {base} {revision}"
        print(
            f"lint:openapi-breaking: {levels_found[WARN]} warnings, which fail nothing; `{listing}` lists them",
            flush=True,
        )
    errors = subprocess.run(
        [
            "oasdiff",
            "changelog",
            "--level",
            "ERR",
            "--fail-on",
            "ERR",
            "--format",
            "singleline",
            *options,
            base,
            revision,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if errors.returncode and not levels_found[ERR]:
        print(errors.stderr, end="", file=sys.stderr)
        return errors.returncode
    if levels_found[ERR]:
        print(errors.stdout.rstrip(), flush=True)
        print(
            f"lint:openapi-breaking: each of the {levels_found[ERR]} errors above breaks a client of /api/v2/. Keep the"
            f" old shape, or admit the break: copy its line into {EXEMPTIONS} and add {REASON} and why clients can take"
            f" it, or, for a correction one check finds across many operations, add `<check-id> {LOWERED} {REASON}`"
            " and why.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
