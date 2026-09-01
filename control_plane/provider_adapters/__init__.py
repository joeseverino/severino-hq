"""Statically admitted controller provider adapters."""

from .adguard import build_adapter as build_adguard_adapter
from .caddy import build_adapter as build_caddy_adapter


def build_controller_provider_adapters(
    *, provider_model, provider_spec, applies, normalized_hostname
):
    return (
        build_caddy_adapter(
            provider_model=provider_model,
            provider_spec=provider_spec,
            applies=applies,
            normalized_hostname=normalized_hostname,
        ),
        build_adguard_adapter(
            provider_model=provider_model,
            provider_spec=provider_spec,
            applies=applies,
        ),
    )


__all__ = ["build_controller_provider_adapters"]
