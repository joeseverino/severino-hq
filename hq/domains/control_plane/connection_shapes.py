"""The shapes a connection's credential arrives in, declared once.

A shape is the settings a vault item is projected into. The renderer's
registry (``hq/config/controller-connections.json``), the schema of the
connections document and the controller's typed settings are all emitted from
these (``bridge_registry``), so a setting's name, whether it is required and
whether it is secret are stated here and nowhere else.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .provider_spec import ConnectionShape, Setting

ACME = ConnectionShape(
    "acme",
    (
        Setting("DIRECTORY_URL", label="website"),
        Setting("EMAIL", label="email"),
    ),
)
API_TOKEN = ConnectionShape(
    "api_token",
    (
        Setting("API_TOKEN", item_field="credential", secret=True),
        Setting("URL", label="website"),
    ),
)
GITHUB_APP = ConnectionShape(
    "github_app",
    (
        Setting("APP_ID", label="app_id"),
        # The name of the key item, never the key.
        Setting("SIGNING_KEY", label="signing_key"),
    ),
    provider="github_app",
)
LOGIN = ConnectionShape(
    "login",
    (
        Setting("PASSWORD", item_field="password", secret=True),
        Setting("URL", url=0),
        Setting("USERNAME", item_field="username"),
    ),
)
OAUTH_CLIENT = ConnectionShape(
    "oauth_client",
    (
        Setting("CLIENT_ID", item_field="username"),
        Setting("CLIENT_SECRET", item_field="credential", secret=True),
    ),
)
SERVICE_ACCOUNT = ConnectionShape(
    "service_account",
    (Setting("API_TOKEN", item_field="credential", secret=True),),
)
SSH_TRANSPORT = ConnectionShape(
    "ssh_transport",
    (
        Setting("HOST", label="host"),
        Setting("HOST_KEY", label="host_key"),
        # The name of the identity item, never the key.
        Setting("IDENTITY", label="identity"),
        Setting("PORT", label="port"),
        Setting("ROLE", label="role", optional=True),
        Setting("USER", label="user"),
    ),
    # A transport is a way in to a machine; what is read through it says so.
    default_provider="ssh",
)


def _shapes(*shapes: ConnectionShape) -> Mapping[str, ConnectionShape]:
    found: dict[str, ConnectionShape] = {}
    for shape in shapes:
        if shape.name in found:
            raise ValueError(f"Two connection shapes are named {shape.name!r}.")
        found[shape.name] = shape
    return MappingProxyType(dict(sorted(found.items())))


# Every shape, by name. ``acme`` belongs to no provider: it is the certificate
# authority account the TLS provider issues through.
SHAPES = _shapes(
    ACME, API_TOKEN, GITHUB_APP, LOGIN, OAUTH_CLIENT, SERVICE_ACCOUNT, SSH_TRANSPORT
)


def _entry(setting: Setting) -> dict[str, Any]:
    entry: dict[str, Any]
    if setting.url is not None:
        entry = {"source": "url", "index": setting.url}
    elif setting.item_field:
        entry = {"source": "field", "id": setting.item_field}
    else:
        entry = {"source": "field", "label": setting.label}
    if setting.optional:
        entry["optional"] = True
    return entry


def _projection(shape: ConnectionShape) -> dict[str, dict[str, Any]]:
    provider: dict[str, Any] = (
        {"source": "constant", "value": shape.provider}
        if shape.provider
        else {"source": "field", "label": "provider", "optional": True}
    )
    if shape.default_provider:
        provider["default"] = shape.default_provider
    entries: dict[str, dict[str, Any]] = {
        "CONNECTION_REF": {"source": "connection_ref"},
        "MANAGES": {"source": "field", "label": "manages", "optional": True},
        "PROVIDER": provider,
        **{setting.name: _entry(setting) for setting in shape.settings},
    }
    return dict(sorted(entries.items()))


def projections() -> dict[str, dict[str, dict[str, Any]]]:
    """Every shape as the renderer's registry states it: each variable and
    where on the vault item it comes from."""

    return {name: _projection(shape) for name, shape in SHAPES.items()}


def renderer_registry() -> dict[str, Any]:
    """``hq/config/controller-connections.json``, which the renderer reads."""

    return {"schema_version": 1, "projections": projections()}
