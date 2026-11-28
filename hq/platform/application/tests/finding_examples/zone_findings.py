from hq.platform.core.testing import declare

from ...display_words import Code
from ...finding_steps import Do
from ..test_paths import record
from . import Example, Examples, Raised

ZONE = "cloudflare.zone"
RECORD = "cloudflare.dns_record"
SPF = record("example.com", "TXT", '"v=spf1 include:_spf.example.net -all"', proxied=False)


def zone(name: str = "example.com", **posture: str) -> dict:
    return {"zone": name, "connection_ref": "example-dns", **({"posture": posture} if posture else {})}


def records(*found: dict, raises: tuple[Raised | str, ...] = (), given=None, zones=None) -> Example:
    """A swept domain holding ``found``."""

    return Example(readings={ZONE: zones or (zone(),), RECORD: found}, given=given, raises=raises)


def caa(issuer: str, tag: str = "issue") -> dict:
    return record("example.com", "CAA", f'0 {tag} "{issuer}"', proxied=False)


def certificate(kind: str = "tls.certificate"):
    """A certificate covering example.com, which HQ renews unless it was uploaded."""

    def given() -> None:
        declare(
            "example-wildcard",
            kind,
            {
                "certificate_name": "example-wildcard",
                "domains": ["example.com", "*.example.com"],
                "install_on": ["edge"],
            },
            status={"not_after": "2099-01-01T00:00:00+00:00"},
            enabled=True,
        )

    return given


def includes(count: int) -> dict:
    senders = " ".join(f"include:_spf{number}.example.net" for number in range(count))
    return record("example.com", "TXT", f'"v=spf1 {senders} -all"', proxied=False)


def dmarc(policy: str) -> dict:
    return record("_dmarc.example.com", "TXT", f'"v=DMARC1; p={policy}; rua=mailto:dmarc@example.com"', proxied=False)


EXAMPLES = (
    Examples(
        "acme-challenge-left-over",
        fires=(
            records(
                SPF,
                record("_acme-challenge.example.com", "TXT", '"example-token"', proxied=False),
                record("_acme-challenge.www.example.com", "TXT", '"example-token"', proxied=False),
                raises=(
                    Raised(
                        title="example.com has 2 left-over ACME challenges",
                        evidence=(
                            ("Record", Code("_acme-challenge.example.com")),
                            ("Record", Code("_acme-challenge.www.example.com")),
                        ),
                        steps=(Do("Delete each record named here at the DNS provider."),),
                    ),
                ),
            ),
        ),
        quiet=(records(SPF),),
    ),
    Examples(
        "caa-refuses-renewal",
        fires=(
            records(
                caa("digicert.com"),
                given=certificate(),
                raises=(
                    Raised(
                        title="example.com's CAA records refuse letsencrypt.org",
                        explanation="The next renewal of example-wildcard will be refused, and the certificate lapses.",
                        steps=(Do("Add this CAA record to example.com.", value='0 issue "letsencrypt.org"'),),
                    ),
                ),
            ),
        ),
        quiet=(
            # iodef names where to report a violation, never who may issue.
            records(caa("letsencrypt.org"), caa("mailto:security@example.com", "iodef"), given=certificate()),
            # HQ never chose who signed an uploaded certificate, so it cannot predict its renewal.
            records(caa("digicert.com"), given=certificate("tls.uploaded_certificate")),
        ),
    ),
    Examples(
        "encryption-weaker-than-elsewhere",
        fires=(
            records(
                zones=(
                    zone(ssl="full", min_tls_version="1.0"),
                    zone("example.net", ssl="strict", min_tls_version="1.2"),
                ),
                raises=(
                    Raised(
                        title="example.com accepts weaker TLS than example.net",
                        evidence=(
                            ("Posture", "Full, TLS 1.0"),
                            ("Stronger on", "example.net (Full (strict), TLS 1.2)"),
                        ),
                        steps=(
                            Do(
                                "In Cloudflare's SSL/TLS settings for example.com, set the encryption mode to "
                                "Full (strict) and the minimum TLS version to 1.2, as on example.net."
                            ),
                        ),
                    ),
                ),
            ),
        ),
        quiet=(
            records(
                zones=(
                    zone(ssl="strict", min_tls_version="1.2"),
                    zone("example.net", ssl="strict", min_tls_version="1.2"),
                )
            ),
        ),
    ),
    Examples(
        "spf-over-lookup-limit",
        fires=(
            records(
                includes(11),
                raises=(Raised(title="example.com's SPF needs 11 DNS lookups", evidence=(("Lookups", "11 of 10"),)),),
            ),
        ),
        quiet=(records(includes(10)),),
    ),
    Examples(
        "dmarc-not-enforcing",
        fires=(records(SPF, dmarc("none"), raises=("example.com's DMARC delivers mail that fails it",)),),
        quiet=(records(SPF, dmarc("reject")),),
    ),
)
