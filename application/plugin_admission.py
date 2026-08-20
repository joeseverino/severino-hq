"""Fail-closed runtime enforcement for Cordon-verified plugin compositions."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import re
from typing import Any, NoReturn, Protocol

from django.core.exceptions import ImproperlyConfigured


class AdmittedPlugin(Protocol):
    """The admission-facing slice of a plugin manifest."""

    id: str
    version: str
    distribution: str
    api_version: int
    source_repository: str
    source_workflow: str

SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SIGNER_ISSUER = "https://token.actions.githubusercontent.com"
HOST = "severino-hq"
LOCK_KEYS = {
    "ok",
    "schema_version",
    "plugin",
    "version",
    "distribution",
    "host",
    "plugin_api_version",
    "source_repository",
    "source_workflow",
    "source_commit",
    "signer_identity",
    "oidc_issuer",
    "artifact_sha256",
    "policy_sha256",
}


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def admission_required() -> bool:
    explicit = os.environ.get("SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION")
    if explicit is not None:
        return _enabled(explicit)
    return not _enabled(os.environ.get("DJANGO_DEBUG"))


def _fail(message: str) -> NoReturn:
    raise ImproperlyConfigured(f"Plugin admission failed: {message}")


def _require(digest: Any, pattern: re.Pattern[str], message: str) -> None:
    """A field that must be a hex digest of exactly the right shape."""

    if not isinstance(digest, str) or not pattern.fullmatch(digest):
        _fail(message)


def _load_lock() -> list[dict[str, Any]]:
    lock_path = os.environ.get("SEVERINO_HQ_PLUGIN_LOCK", "").strip()
    if not lock_path:
        _fail("SEVERINO_HQ_PLUGIN_LOCK is required")
    try:
        document = json.loads(Path(lock_path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"cannot read lock {lock_path!r}")
        raise AssertionError from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "host",
        "plugins",
    }:
        _fail("lock envelope is not canonical")
    if document["schema_version"] != 1 or document["host"] != "severino-hq":
        _fail("lock schema or host is incompatible")
    plugins = document["plugins"]
    if not isinstance(plugins, list) or not all(isinstance(item, dict) for item in plugins):
        _fail("lock plugins must be a list of objects")
    return plugins


def _expected_policy() -> str:
    """The Cordon policy digest every approval must have been signed under."""

    digest = os.environ.get("SEVERINO_HQ_PLUGIN_POLICY_SHA256", "").strip()
    _require(
        digest, SHA256, "SEVERINO_HQ_PLUGIN_POLICY_SHA256 must be a lowercase SHA-256"
    )
    return digest


def _admitted_id(approval: dict[str, Any], *, expected_policy: str, seen: set) -> str:
    """Check one approval on its own terms; return the plugin it admits.

    Order is load-bearing: the shape check runs first because every later line
    indexes fields it guarantees are present. Checks are written out rather than
    driven from a table so the requirements can be read in order.
    """

    if set(approval) != LOCK_KEYS:
        _fail("approval fields are not canonical")
    if approval["ok"] is not True or approval["schema_version"] != 1:
        _fail("approval verdict or schema is incompatible")
    plugin_id = approval["plugin"]
    if not isinstance(plugin_id, str) or plugin_id in seen:
        _fail("approval plugin IDs must be unique strings")
    if approval["host"] != HOST:
        _fail(f"{plugin_id!r} targets another host")
    # Built from the approval's own claimed source, then compared. An extension
    # cannot widen who may sign for it by editing its own repository, because
    # the identity it must match is derived from the fields being checked.
    expected_identity = (
        f"https://github.com/{approval['source_repository']}/"
        f"{approval['source_workflow']}@refs/heads/main"
    )
    if approval["signer_identity"] != expected_identity:
        _fail(f"{plugin_id!r} used an unexpected signer identity")
    if approval["oidc_issuer"] != SIGNER_ISSUER:
        _fail(f"{plugin_id!r} used an unexpected OIDC issuer")
    if approval["policy_sha256"] != expected_policy:
        _fail(f"{plugin_id!r} used an unexpected Cordon policy")
    for field in ("artifact_sha256", "policy_sha256"):
        _require(approval[field], SHA256, f"{plugin_id!r} has an invalid {field}")
    _require(
        approval["source_commit"], COMMIT, f"{plugin_id!r} has an invalid source commit"
    )
    return plugin_id


def _agrees_with_approval(manifest: AdmittedPlugin, approval: dict[str, Any]) -> None:
    """The running manifest must be the artifact that was approved."""

    approved = {
        "version": manifest.version,
        "distribution": manifest.distribution,
        "plugin_api_version": manifest.api_version,
        "source_repository": manifest.source_repository,
        "source_workflow": manifest.source_workflow,
    }
    for field, value in approved.items():
        if approval[field] != value:
            _fail(f"{manifest.id!r} {field} does not match its approval")
    # And the wheel actually installed must be that version too: a manifest
    # agreeing with its approval says nothing about what pip resolved.
    try:
        installed = package_version(manifest.distribution)
    except PackageNotFoundError:
        _fail(f"distribution {manifest.distribution!r} is not installed")
    if installed != manifest.version:
        _fail(f"installed {manifest.distribution!r} version does not match")


def enforce_plugin_admission(manifests: tuple[AdmittedPlugin, ...]) -> None:
    if not manifests or not admission_required():
        return
    expected_policy = _expected_policy()

    by_id: dict[str, dict[str, Any]] = {}
    for approval in _load_lock():
        plugin_id = _admitted_id(
            approval, expected_policy=expected_policy, seen=set(by_id)
        )
        by_id[plugin_id] = approval

    # Exactly, in both directions. An approval without a plugin is a stale lock;
    # a plugin without an approval is the thing this whole path exists to refuse.
    if set(by_id) != {manifest.id for manifest in manifests}:
        _fail("lock inventory does not exactly match enabled plugins")
    for manifest in manifests:
        _agrees_with_approval(manifest, by_id[manifest.id])
