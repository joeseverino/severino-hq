"""What a provider's declarations share with HQ: why a read failed, and the
facts an observed record states about its names."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Why a provider refused a read: the credential itself (invalid, expired,
# locked out, used from a refused location), or one permission it lacks.
CREDENTIAL_REFUSAL = "credential"
PERMISSION_REFUSAL = "permission"
REFUSALS = (CREDENTIAL_REFUSAL, PERMISSION_REFUSAL)

# Why a read got no API answer at all: the address answered as something other
# than the API (a sign-in page, a web page, a redirect elsewhere), or nothing
# answered (no route, refused connection, timeout).
ADDRESS_FAILURE = "address"
NETWORK_FAILURE = "network"
# Every cause a failed read is stored with: a refusal, or one of these.
FAILURES = (*REFUSALS, ADDRESS_FAILURE, NETWORK_FAILURE)


@dataclass(frozen=True, slots=True)
class IngressPolicy:
    """The source policy an observed proxy record applies to its names.

    ``rules`` are ``(directive, address)`` in order, lowercased; None when the
    record carries no rule-level reading. ``authorizations`` is None when unknown.
    """

    hostnames: tuple[str, ...]
    restricted: bool
    rules: tuple[tuple[str, str], ...] | None = None
    implicit_deny: bool = False
    satisfy_any: bool = True
    passes_auth: bool = True
    authorizations: int | None = None


@dataclass(frozen=True, slots=True)
class ServedCertificate:
    """The certificate an observed record serves its names with.

    ``unread`` says why the record cannot name it; ``certificate`` is then empty.
    """

    hostnames: tuple[str, ...]
    certificate: Mapping[str, Any]
    unread: str = ""
