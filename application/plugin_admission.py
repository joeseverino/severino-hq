"""Fail-closed runtime enforcement for Cordon-verified plugin compositions."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol

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


def _fail(message: str) -> None:
    raise ImproperlyConfigured(f"Plugin admission failed: {message}")


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


def enforce_plugin_admission(manifests: tuple[AdmittedPlugin, ...]) -> None:
    if not manifests or not admission_required():
        return
    expected_policy = os.environ.get(
        "SEVERINO_HQ_PLUGIN_POLICY_SHA256", ""
    ).strip()
    if not SHA256.fullmatch(expected_policy):
        _fail("SEVERINO_HQ_PLUGIN_POLICY_SHA256 must be a lowercase SHA-256")

    approvals = _load_lock()
    by_id: dict[str, dict[str, Any]] = {}
    for approval in approvals:
        if set(approval) != LOCK_KEYS:
            _fail("approval fields are not canonical")
        if approval["ok"] is not True or approval["schema_version"] != 1:
            _fail("approval verdict or schema is incompatible")
        plugin_id = approval["plugin"]
        if not isinstance(plugin_id, str) or plugin_id in by_id:
            _fail("approval plugin IDs must be unique strings")
        if approval["host"] != "severino-hq":
            _fail(f"{plugin_id!r} targets another host")
        expected_identity = (
            f"https://github.com/{approval['source_repository']}/"
            f"{approval['source_workflow']}@refs/heads/main"
        )
        if approval["signer_identity"] != expected_identity:
            _fail(f"{plugin_id!r} used an unexpected signer identity")
        if approval["oidc_issuer"] != "https://token.actions.githubusercontent.com":
            _fail(f"{plugin_id!r} used an unexpected OIDC issuer")
        if approval["policy_sha256"] != expected_policy:
            _fail(f"{plugin_id!r} used an unexpected Cordon policy")
        for field in ("artifact_sha256", "policy_sha256"):
            if not isinstance(approval[field], str) or not SHA256.fullmatch(
                approval[field]
            ):
                _fail(f"{plugin_id!r} has an invalid {field}")
        if not isinstance(approval["source_commit"], str) or not re.fullmatch(
            r"[0-9a-f]{40}", approval["source_commit"]
        ):
            _fail(f"{plugin_id!r} has an invalid source commit")
        by_id[plugin_id] = approval

    expected_ids = {manifest.id for manifest in manifests}
    if set(by_id) != expected_ids:
        _fail("lock inventory does not exactly match enabled plugins")
    for manifest in manifests:
        approval = by_id[manifest.id]
        expected = {
            "version": manifest.version,
            "distribution": manifest.distribution,
            "plugin_api_version": manifest.api_version,
            "source_repository": manifest.source_repository,
            "source_workflow": manifest.source_workflow,
        }
        for field, value in expected.items():
            if approval[field] != value:
                _fail(f"{manifest.id!r} {field} does not match its approval")
        try:
            installed_version = package_version(manifest.distribution)
        except PackageNotFoundError:
            _fail(f"distribution {manifest.distribution!r} is not installed")
            raise AssertionError
        if installed_version != manifest.version:
            _fail(f"installed {manifest.distribution!r} version does not match")
