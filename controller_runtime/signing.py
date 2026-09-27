"""The controller's signing keys and what its image was composed from.

A signing key is rendered beside the SSH identities under its connection's own
name, so it is reachable only through that connection, and only openssl reads
it: the controller receives the signature and never the key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from control_plane.provider_adapters.contracts import ProviderError


def signing_key(connection_ref: str, *, public: bool = False) -> Path:
    from .providers import _required, connection_prefixes

    if not connection_ref or "/" in connection_ref or connection_ref.startswith("."):
        raise ProviderError("Invalid signing connection.")
    if connection_ref not in connection_prefixes():
        raise ProviderError(
            f"No connection named {connection_ref!r} was supplied to the controller."
        )
    ssh_dir = Path(_required("HQ_CONTROLLER", "SSH_DIR"))
    return ssh_dir / f"{connection_ref}.key{'.pub' if public else ''}"


class SigningRuntime:
    """The ``ProviderRuntime`` methods for signing and composition."""

    def composition(self) -> dict[str, Any]:
        from application.plugin_admission import admitted_sources

        return {
            "repository": os.environ.get("SEVERINO_HQ_SOURCE_REPOSITORY", "").strip(),
            "image": os.environ.get("HQ_CONTROLLER_IMAGE", "").strip(),
            "extensions": admitted_sources(),
        }

    def sign(self, connection_ref: str, data: bytes) -> bytes:
        from .providers import _run

        return _run(
            ["openssl", "dgst", "-sha256", "-sign", str(signing_key(connection_ref))],
            input_bytes=data,
            step=f"sign for {connection_ref}",
            subject=connection_ref,
        )

    def signing_public_key(self, connection_ref: str) -> str:
        try:
            return signing_key(connection_ref, public=True).read_text()
        except OSError as exc:
            raise ProviderError(
                f"No signing key was rendered for {connection_ref!r}."
            ) from exc
