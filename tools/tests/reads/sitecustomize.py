"""Appends each file this process opens to read to the file HQ_READS_LOG names, one absolute path a line.

Python imports ``sitecustomize`` from its path as it starts, so a directory holding this module on
PYTHONPATH reaches every Python process a command starts, however deep: test_drift_sources.py
puts it there to see what a drift gate reads. A bytecode cache read is its source file's, which
a fresh cache would have read instead."""

import importlib.util
import os
import sys
from pathlib import Path


def _install(log_path: str) -> None:
    # Opened before the hook is, so writing the log is never itself a read.
    log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)

    def hook(event: str, args: tuple[object, ...]) -> None:
        if event != "open":
            return
        target, _mode, flags = args
        if not isinstance(target, str | bytes | os.PathLike):
            return
        if isinstance(flags, int) and flags & os.O_ACCMODE == os.O_WRONLY:
            return
        path = str(Path(os.fsdecode(target)).resolve())
        if path.endswith(".pyc") and f"{os.sep}__pycache__{os.sep}" in path:
            path = importlib.util.source_from_cache(path)
        os.write(log, f"{path}\n".encode())

    sys.addaudithook(hook)


if os.environ.get("HQ_READS_LOG"):
    _install(os.environ["HQ_READS_LOG"])
