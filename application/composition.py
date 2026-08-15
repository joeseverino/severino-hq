"""Merging independently admitted plugins into one deployable composition.

Each extension is verified and admitted on its own, producing a single-entry
approval. A deployment runs one image, so those approvals have to become one
lock covering every extension in it.

This module owns that merge because it is the security-sensitive half: the
runtime already refuses to start unless the lock inventory *exactly* matches the
enabled extensions, so a merge that quietly drops, duplicates, or mixes
approvals is the one way to weaken admission without tripping it. Keeping it
here -- importable, unit-tested, no shell quoting -- means the build cannot grow
its own subtly different copy.
"""

from __future__ import annotations

from typing import Any, Iterable

SCHEMA_VERSION = 1
HOST = "severino-hq"

# Fields whose value must be identical across every approval in a composition.
# The policy hash is the load-bearing one: the runtime compares each approval
# against a single expected policy, so approvals issued under different policies
# can never be satisfied together and must be rejected at merge time with a
# clear message rather than at boot with a confusing one.
SHARED_FIELDS = ("schema_version", "host", "policy_sha256")


class CompositionError(Exception):
    """A set of approvals cannot form one trustworthy composition."""


def merge_admissions(approvals: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-extension approvals into the lock a composed image carries.

    Approvals are used verbatim -- this never edits an approval's contents, only
    decides whether the set may travel together. Rewriting a field here would
    mean the lock no longer describes what was actually signed.
    """
    entries = list(approvals)
    if not entries:
        raise CompositionError("a composition needs at least one admitted plugin")

    for entry in entries:
        if not isinstance(entry, dict):
            raise CompositionError("each approval must be an object")
        if entry.get("ok") is not True:
            raise CompositionError(
                f"{entry.get('plugin', '<unknown>')!r} is not an approving verdict"
            )
        if entry.get("host") != HOST:
            raise CompositionError(
                f"{entry.get('plugin', '<unknown>')!r} targets host "
                f"{entry.get('host')!r}, not {HOST!r}"
            )

    for field in SHARED_FIELDS:
        values = {entry.get(field) for entry in entries}
        if len(values) > 1:
            raise CompositionError(
                f"approvals disagree on {field}: {sorted(map(repr, values))}. "
                "Re-admit every plugin against the same policy before composing."
            )

    ids = [entry.get("plugin") for entry in entries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise CompositionError(f"duplicate plugin ids in composition: {duplicates}")

    # Sorted so the same set of approvals always produces a byte-identical lock,
    # which keeps image digests reproducible and diffs readable.
    return {
        "schema_version": SCHEMA_VERSION,
        "host": HOST,
        "plugins": sorted(entries, key=lambda entry: entry["plugin"]),
    }


def composition_plugin_references(lock: dict[str, Any]) -> str:
    """The ``SEVERINO_HQ_PLUGINS`` value for a composed image.

    Derived from the lock rather than configured separately: the enabled set and
    the approved set have to be identical or the runtime refuses to boot, so
    deriving one from the other removes the chance of them drifting apart.
    """
    return ",".join(
        f"{entry['distribution'].replace('-', '_')}.plugin:plugin"
        for entry in lock["plugins"]
    )
