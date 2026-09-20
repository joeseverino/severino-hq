"""Authentication boundaries for controller subprocesses."""

import os


def command_environment(overrides: dict[str, str] | None) -> dict[str, str] | None:
    if not overrides:
        return None
    environment = {**os.environ, **overrides}
    if "OP_SERVICE_ACCOUNT_TOKEN" in overrides:
        # Connect takes precedence in op, even when a service account is passed.
        for name in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            environment.pop(name, None)
    return environment
