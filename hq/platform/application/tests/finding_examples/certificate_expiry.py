from datetime import timedelta

from ...tone import Tone
from . import Ago, Example, Examples, Raised

EDGE = "cloudflare.edge_certificate"


def edge(days: float, *hosts: str, authority: str = "lets_encrypt", ref: str = "example-cf", id: str = "") -> dict:
    """An edge certificate ``days`` from expiring, serving ``hosts``."""

    return {
        "connection_ref": ref,
        "zone": "example.com",
        "id": id or f"example-{days}-{authority}-{'-'.join(hosts)}",
        "type": "universal",
        "hosts": list(hosts),
        "status": "expired" if days < 0 else "active",
        "certificate_authority": authority,
        "expires_on": Ago(-timedelta(days=days, hours=12)),
    }


def edges(*records: dict, raises: tuple[Raised | str, ...] = ()) -> Example:
    return Example(readings={EDGE: records}, raises=raises)


def npm(number: int, provider: str) -> dict:
    return {
        "connection_ref": "example-npm",
        "id": number,
        "name": "lapsed.example.com",
        "provider": provider,
        "domains": ["lapsed.example.com"],
        "expires_on": Ago(timedelta(days=3, hours=12 - number)),
        "serves": [],
    }


EXAMPLES = (
    Examples(
        "certificate-expiring",
        fires=(
            # Two weeks out.
            edges(edge(14, "app.example.com"), raises=("Edge certificate example.com expires in 15\xa0days",)),
            # Past expiry.
            edges(
                edge(-3, "app.example.com"),
                raises=(Raised(title="Edge certificate example.com expired 2\xa0days ago", severity=Tone.SERIOUS),),
            ),
            # A name held twice is raised once, by the one that runs out last.
            edges(
                edge(-3, "app.example.com"),
                edge(5, "app.example.com"),
                raises=(Raised(title="Edge certificate example.com expires in 6\xa0days", severity=Tone.SERIOUS),),
            ),
            # Certificates that serve no name are each their own, told apart by
            # issuer. One title read at one moment is one certificate, so these
            # run out hours apart.
            edges(
                edge(-3, authority="google"),
                edge(-2.8, authority="lets_encrypt"),
                raises=(
                    "Edge certificate example.com from Google Trust Services expired 2\xa0days ago",
                    "Edge certificate example.com from Let's Encrypt expired 2\xa0days ago",
                ),
            ),
            # From one issuer, told apart by the names they serve.
            edges(
                edge(4, "a.example.com"),
                edge(4.2, "b.example.com"),
                raises=(
                    Raised(
                        title="Edge certificate example.com for a.example.com expires in 5\xa0days",
                        severity=Tone.SERIOUS,
                    ),
                    Raised(
                        title="Edge certificate example.com for b.example.com expires in 5\xa0days",
                        severity=Tone.SERIOUS,
                    ),
                ),
            ),
            # One held in two places, told apart by where.
            edges(
                edge(4, "app.example.com", ref="example-cf-a"),
                edge(4, "app.example.com", ref="example-cf-b"),
                raises=(
                    Raised(
                        title="Edge certificate example.com in example-cf-a expires in 5\xa0days", severity=Tone.SERIOUS
                    ),
                    Raised(
                        title="Edge certificate example.com in example-cf-b expires in 5\xa0days", severity=Tone.SERIOUS
                    ),
                ),
            ),
            # One issued and one uploaded, told apart by how each came.
            Example(
                readings={"npm.certificate": (npm(1, "letsencrypt"), npm(2, "other"))},
                raises=(
                    "NPM certificate lapsed.example.com from Let's Encrypt expired 3\xa0days ago",
                    "Uploaded NPM certificate lapsed.example.com expired 3\xa0days ago",
                ),
            ),
        ),
        quiet=(
            edges(edge(60, "app.example.com")),
            # A replaced certificate that lapsed, its successor far off.
            edges(edge(-3, "app.example.com"), edge(60, "app.example.com")),
        ),
    ),
)
