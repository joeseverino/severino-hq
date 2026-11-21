"""`mise run lint:file-lines [files]`: no source file is longer than the ceiling mise.toml declares.

A file grows freely below it; one past it is split by responsibility, a test
module by the behavior it holds. mise.toml's ``SEVERINO_FILE_LINE_*``, which
the launch-time brief check reads too, name the ceiling and the files it
holds. Handed no file, it reads every file git tracks or would.
"""

import os
import subprocess
import sys
from pathlib import Path, PurePath

LIMIT = int(os.environ["SEVERINO_FILE_LINE_LIMIT"])
HELD, EXEMPT, COUNTED = (os.environ[f"SEVERINO_FILE_LINE_{name}"].split() for name in ("HELD", "EXEMPT", "COUNTED"))


def matches(path: PurePath, globs: list[str]) -> bool:
    return any(path.full_match(glob) for glob in globs)


def main(named: list[str]) -> int:
    listed = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    every = named or subprocess.run(listed, capture_output=True, text=True, check=True).stdout.split("\0")
    # A test module is held like the code it tests.
    paths = [
        path
        for path in map(PurePath, every)
        if matches(path, HELD) and (matches(path, COUNTED) or not matches(path, EXEMPT)) and Path(path).is_file()
    ]
    # A glob that holds no file of the whole checkout is a wrong glob, not a clean tree.
    unheld = [] if named else [glob for glob in HELD if not any(path.full_match(glob) for path in paths)]
    failures = [f"SEVERINO_FILE_LINE_HELD's {glob} holds no file of the checkout" for glob in unheld]
    sizes = {path: len(Path(path).read_text(encoding="utf-8").splitlines()) for path in paths}
    failures += [
        f"{path}: {n} lines, past the ceiling of {LIMIT}: split it by responsibility"
        for path, n in sizes.items()
        if n > LIMIT
    ]
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
