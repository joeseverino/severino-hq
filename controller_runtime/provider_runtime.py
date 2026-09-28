"""The one runtime every admitted adapter is bound to, and the registry compiled from them."""

from __future__ import annotations

from typing import Any

from control_plane.providers import controller_id
from control_plane.provider_adapters import CONTROLLER_PROVIDER_ADAPTERS
from control_plane.provider_adapters.contracts import compile_controller_adapters
from .signing import SigningRuntime
from .handlers import acts, lists, probes, reads
from . import commands, connection_env, portainer, provider_http


class _ProviderRuntime(SigningRuntime):
    """Bind adapters to the controller's narrow, patchable I/O boundary."""

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return provider_http.request_json(url, method=method, headers=headers, payload=payload)

    def required(self, prefix: str, name: str) -> str:
        return provider_http.required(prefix, name)

    def connection_prefix(self, provider: str, connection_ref: str = "") -> str:
        return connection_env.connection_prefix(provider, connection_ref)

    def snapshot_value(self, key, load):
        return provider_http.snapshot_value(key, load)

    def condition(
        self, condition_type: str, status: bool, reason: str, message: str
    ) -> dict[str, Any]:
        return provider_http.condition(condition_type, status, reason, message)

    def ssh_connection_refs(self) -> tuple[str, ...]:
        return connection_env.ssh_connection_refs()

    def connection_refs(self, provider: str) -> tuple[str, ...]:
        return connection_env.provider_connection_refs(provider)

    def connection_refs_for_role(self, role: str) -> tuple[str, ...]:
        return connection_env.connection_refs_for_role(role)

    def controller_id(self) -> str:
        return controller_id()

    def own_run(self, container) -> bool:
        return portainer.is_this_run(dict(container))

    def ssh(
        self, connection_ref: str, operation: str, payload: bytes | None = None
    ) -> bytes:
        return commands.run_ssh(connection_ref, operation, payload)

    def run(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        step: str = "command",
    ) -> bytes:
        return commands.run_command(command, step=step, env=env)


RUNTIME = _ProviderRuntime()
_ADAPTER_REGISTRY = compile_controller_adapters(CONTROLLER_PROVIDER_ADAPTERS, RUNTIME)


def _register_adapters() -> None:
    """Every admitted adapter's readers, inventory, probes and actions, bound to the runtime."""

    for kind, reader in _ADAPTER_REGISTRY.readings.items():
        reads(kind)(reader)
    for kind, lister in _ADAPTER_REGISTRY.inventory.items():
        lists(kind)(lister)
    for provider, probe in _ADAPTER_REGISTRY.connection_probes.items():
        probes(provider)(probe)
    for (kind, action), handler in _ADAPTER_REGISTRY.actions.items():
        acts(kind, action)(handler)


_register_adapters()
