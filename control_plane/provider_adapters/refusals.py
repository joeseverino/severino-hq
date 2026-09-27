"""A failed provider read, named as the credential or one permission refused.

Providers with a coarse credential (a login, an API key acting as its user)
answer 401 when the credential is refused and 403 when its user lacks one
area. The permission named is the provider's own.
"""

from __future__ import annotations

import urllib.error

from .contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
    ProviderError,
)


def _status(exc: BaseException) -> int:
    """The HTTP status a failure carries, its response closed."""

    for found in (exc, exc.__cause__):
        if isinstance(found, urllib.error.HTTPError):
            found.close()
            return int(found.code)
    return 0


def refused(exc: BaseException, *, what: str, needs: str) -> ProviderError:
    """``exc`` as a ProviderError carrying its refusal, where it is one."""

    status = _status(exc)
    if status == 401:
        return ProviderError(
            f"{what}: the credential was refused.",
            refusal=CREDENTIAL_REFUSAL,
            reason="The credential was refused.",
        )
    if status == 403:
        return ProviderError(f"{what} needs {needs}.", refusal=PERMISSION_REFUSAL)
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(f"{what} failed: {type(exc).__name__}.")
