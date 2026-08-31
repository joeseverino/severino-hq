"""Gateways to the two public registries HQ does not run itself.

In the Django app rather than in ``application/``, matching ``projects.github``:
the outbound call, the scheme guard, the timeout and the one exception type
live together, and the service above injects these as defaults so no test
opens a socket.

A resolver answers what the public internet returns for a name and what name an
address gives for itself. RDAP answers who holds an address, from the
registries rather than from a copy of them.

Bounded because this runs in the process holding the session store: https only,
a short timeout, and the only caller-supplied value is one the service has
already checked is a hostname or an address. No credential travels with it,
which is what makes the call acceptable here rather than controller work.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
import urllib.request

from django.conf import settings

from application.connection_contracts import (
    ConnectionAbility,
    ConnectionInstance,
    ConnectionSpec,
)
from application.security import Capability


def connection_specs():
    """Emit the keyless public registries used by lookup capabilities."""

    def configured_instance(
        *, identifier: str, label: str, kind: str, setting: str, abilities: tuple[str, ...]
    ):
        endpoint = str(getattr(settings, setting, "") or "").strip()
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        return ConnectionInstance(
            id=identifier,
            label=label,
            kind=kind,
            status="good",
            status_label="configured",
            detail="Keyless public API; reachability is checked only when a lookup runs.",
            endpoint=f"https://{parsed.netloc}",
            ability_names=abilities,
        )

    def instances():
        emitted = (
            configured_instance(
                identifier="public-dns-resolver",
                label="Public DNS resolver",
                kind="public_dns",
                setting="SEVERINO_LOOKUP_ENDPOINT",
                abilities=("lookup.public_dns", "lookup.reverse_dns"),
            ),
            configured_instance(
                identifier="public-rdap",
                label="Public address registry",
                kind="rdap",
                setting="SEVERINO_RDAP_ENDPOINT",
                abilities=("lookup.address_registry",),
            ),
        )
        return tuple(instance for instance in emitted if instance is not None)

    return (
        ConnectionSpec(
            name="hq.public_registries",
            label="Public lookup registries",
            summary="Keyless DNS and address registries used for external truth.",
            required_capability=Capability.LOOK_UP_PUBLIC_RECORDS,
            instance_provider=instances,
            abilities=(
                ConnectionAbility(
                    "lookup.public_dns",
                    "Public DNS lookup",
                    "Resolve a hostname from outside HQ's internal DNS rewrites.",
                    capability="lookup.name",
                ),
                ConnectionAbility(
                    "lookup.reverse_dns",
                    "Reverse DNS lookup",
                    "Read the public name an address publishes for itself.",
                    capability="lookup.address",
                ),
                ConnectionAbility(
                    "lookup.address_registry",
                    "Address ownership lookup",
                    "Read the public allocation and registrant for an address.",
                    capability="lookup.address",
                ),
            ),
            web_route="control_plane:tools",
            documentation_url="https://www.rfc-editor.org/rfc/rfc9082",
        ),
    )


class LookupUnavailable(RuntimeError):
    """A registry could not be reached, or answered with nothing usable."""


class _HTTPSOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a registry's redirect, but never down to plain HTTP.

    RDAP is a bootstrap service: `rdap.org` answers with a redirect to whichever
    regional registry actually holds the allocation, so redirects have to be
    followed for it to work at all. Following one to `http://` would put the
    address being asked about on the wire in clear, which is a strange way to
    answer a question about privacy.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise LookupUnavailable("A registry redirected away from HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HTTPSOnlyRedirects)


def _base(name: str) -> str:
    """A configured provider base, parsed rather than concatenated.

    So a misconfigured setting cannot become a request somewhere unintended:
    the scheme has to be https and the host has to be present.
    """

    configured = str(getattr(settings, name, "") or "").strip()
    parsed = urlsplit(configured)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LookupUnavailable("That lookup provider is not configured.")
    return f"https://{parsed.netloc}"


def _get(url: str, *, timeout: int | None = None, accept: str) -> dict:
    """One JSON reading, or ``LookupUnavailable``.

    Every failure collapses to one exception deliberately. The resolver reports
    a missing parameter as a 400 with prose, a name that does not exist as a
    200 with an empty list, and an address with no PTR record as a 200 carrying
    an error beside a full body -- distinctions too inconsistent to hand
    upward. A caller can act on "no answer"; it could not act on which flavour
    of no answer this was.
    """

    seconds = timeout or int(getattr(settings, "SEVERINO_LOOKUP_TIMEOUT_SECONDS", 6))
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": "severino-hq"}
    )
    try:
        with _opener.open(request, timeout=seconds) as response:
            if response.status != 200:
                raise LookupUnavailable("The registry answered unexpectedly.")
            payload = json.loads(response.read().decode("utf-8"))
    except LookupUnavailable:
        raise
    except HTTPError as exc:
        # 404 from RDAP means the address is not allocated to anyone the
        # registries know, which is an answer rather than a fault -- but it
        # arrives as an exception, and the service above reads the absence.
        raise LookupUnavailable("The registry has no record of that.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise LookupUnavailable("The registry could not be reached.") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise LookupUnavailable("The registry answered with nothing usable.") from exc
    if not isinstance(payload, dict):
        raise LookupUnavailable("The registry answered with nothing usable.")
    return payload


def resolve(path: str, params: dict[str, str], *, timeout: int | None = None) -> dict:
    """Ask the public resolver. ``path`` is a literal chosen by the caller."""

    url = f"{_base('SEVERINO_LOOKUP_ENDPOINT')}/{path.lstrip('/')}?{urlencode(params)}"
    return _get(url, timeout=timeout, accept="application/json")


def registry(address: str, *, timeout: int | None = None) -> dict:
    """Ask RDAP who holds an address.

    The address is interpolated into the path because that is the shape of the
    protocol, and it is safe to do here for one reason: the service above has
    already parsed it with ``ipaddress``, so by this point it is the
    normalised text of a real address and cannot carry a path segment.
    """

    url = f"{_base('SEVERINO_RDAP_ENDPOINT')}/ip/{address}"
    return _get(url, timeout=timeout, accept="application/rdap+json, application/json")
