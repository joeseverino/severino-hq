from pathlib import Path
from typing import override

from django.conf import settings

from hq.platform.core.management.derived import DerivedFileCommand
lazy from hq.platform.application.posture import STANDARD
lazy from hq.platform.application.request_standard import REQUEST_STANDARD

HEAD = """# Posture

Every HQ enforces the checks in the first table below. The second table is
HQ's standard: shown on every HQ, on the connection panel and the Connections
page, each with what it stops. Written by `manage.py posture_reference` from
each check's declaration, a `standards.Check`; a test fails when this file
falls behind.

**Advanced HQ posture** lists each unmet check of the standard in Needs you,
with the step that meets it. The panel then reads **Advanced HQ posture
enabled**, with a lock.

## Turning it on

Set `SEVERINO_POSTURE=advanced` in HQ's settings
([Deployment](Deployment.md#configuration)). HQ will not start on any other
value.

## Every HQ

Every request is measured against these, at HQ's own step of its path
(`REQUEST_STANDARD`, `hq/platform/application/request_standard.py`):

"""

GUARANTEES = """
The deploy and the host enforce the rest, which no request can show:

| Guarantee | Stops | Enforced by |
| --- | --- | --- |
| Sign-in is required | an anonymous visitor | HQ's access rules |
| HQ trusts only the proxies it lists | a forged client address | the trusted-proxy list |
| HQ never holds a credential | a compromised web process leaking keys | the renderer and controller split |
| Secrets are read from a loopback Connect server | a stolen Connect token used from anywhere | `hq-host` refuses any other address in `host.json` |
| Images are signed and pinned by digest | running a tampered build | the deploy's signature check |
| No extension is dropped by a release | a release silently removing a domain | the deploy refuses an undeclared drop |

## HQ's standard

Declared in `STANDARD` (`hq/platform/application/posture.py`). The panel, the
findings, the Connections page and its API read it.

"""

TAIL = """
A check with no reading yet is shown as not read, never as met. A login, such
as Nginx Proxy Manager's, has no scoped form, so it meets the token check
with its full account access named.

The proxy and firewall checks are drawn on the connection panel too, from
the same reading. Two checks are shown only on the connection panel, because
they describe one visit: whether the device's owner is the person signed in,
and whether HQ's own machine measured the link.

Single sign-on belongs to the standard, since not every homelab runs an
identity provider.
"""


def posture_doc() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "Posture.md"


def _table(checks) -> str:
    rows = "".join(f"| {check.label} | {check.why} | {check.mechanism} |\n" for check in checks)
    return f"| Check | Stops | Enforced by |\n| --- | --- | --- |\n{rows}"


def reference_markdown() -> str:
    """docs/Posture.md, each table from the checks' declarations: those every
    request is measured against at HQ's own hop, and HQ's standard."""

    every = tuple(check for check in REQUEST_STANDARD if check.hop == "hq" and check not in STANDARD)
    return f"{HEAD}{_table(every)}{GUARANTEES}{_table(STANDARD)}{TAIL}"


class Command(DerivedFileCommand):
    help = "Write docs/Posture.md from the checks' declarations, or --check it for drift."
    source = "the checks' declarations"

    @override
    def emitted(self) -> dict[Path, str]:
        return {posture_doc(): reference_markdown()}
