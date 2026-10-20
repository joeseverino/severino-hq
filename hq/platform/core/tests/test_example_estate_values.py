"""Every value the example estate writes is made up or the same for everyone.

The estate is public, so an address or a name in it must not be anyone's own:
an address is in a range set aside for examples or one every network shares,
and a name is under a reserved example domain or is a public service the
estate reads, the same for every reader. Read from the estate's source, as
every string literal in it.
"""

import ast
import re
from ipaddress import ip_address, ip_network
from pathlib import Path

from django.test import SimpleTestCase, tag

ESTATE = Path(__file__).resolve().parents[1] / "example_estate"

ADMITTED_RANGES = (
    # Set aside for documentation (RFC 5737, RFC 3849).
    ip_network("192.0.2.0/24"),
    ip_network("198.51.100.0/24"),
    ip_network("203.0.113.0/24"),
    ip_network("2001:db8::/32"),
    # Every home LAN and Docker bridge is numbered from these (RFC 1918). Not
    # 192.168.0.0/16: a host there reads as somebody's own machine, as the
    # repository-wide check (test_architecture's reachable endpoints) holds.
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    # The range Tailscale numbers every tailnet from, and its IPv6 prefix.
    ip_network("100.64.0.0/10"),
    ip_network("fd7a:115c:a1e0::/48"),
)
# Reserved for examples (RFC 2606, RFC 6761).
RESERVED = ("example", "test", "invalid", "example.com", "example.net", "example.org")
# Public services the estate reads, named the same for everyone who reads them.
PUBLIC = {
    # The providers' own API endpoints.
    "api.cloudflare.com",
    "api.github.com",
    "api.tailscale.com",
    "my.1password.com",
    # Where public images and their sources are published.
    "github.com",
    "ghcr.io",
    "codeberg.org",
    # A public resolver, a public certificate authority, and the formats a
    # build record names itself by.
    "dns.quad9.net",
    "letsencrypt.org",
    "slsa.dev",
    "token.actions.githubusercontent.com",
    # Tailscale's MagicDNS domain: the tailnet's own label under it is made up.
    "ts.net",
}
IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\d.])")
IPV6 = re.compile(r"(?<![\w:])[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:/\d{1,3})?(?![\w:])", re.IGNORECASE)
# Dotted words are also record kinds (`tailscale.device`), units and files, so
# a name is a host where it is used as one, in a URL or an address, or where it
# ends in a top-level domain: any country's, or one of the generic ones.
NAME = r"(?:[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?\.)+[a-z][a-z0-9-]+"
USED_AS_HOST = re.compile(rf"(?:://|@)({NAME})")
GENERIC_TLDS = frozenset(
    {"com", "net", "org", "io", "dev", "app", "cloud", "ai", "co", "info", "me", "sh", "xyz", "goog", "page", "site"}
    | {"local", "lan", "home", "internal", "arpa", "example", "test", "invalid", "localhost"}
)
NOT_A_HOST = re.compile(r"\.(ya?ml|sock|json|txt|md|py|pem|csv|pdf|service|timer|path)$")


def literals(source: str) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def addresses(text: str) -> list[str]:
    found = IPV4.findall(text)
    for candidate in IPV6.findall(text):
        try:
            ip_network(candidate, strict=False)
        except ValueError:
            continue
        found.append(candidate)
    return found


def admitted_address(address: str) -> bool:
    network = ip_network(address, strict=False)
    return any(network.version == each.version and network.subnet_of(each) for each in ADMITTED_RANGES)


def hosts(text: str) -> list[str]:
    text = text.lower()
    found = set(USED_AS_HOST.findall(text)) | {name for name in re.findall(NAME, text) if _tld(name)}
    return sorted(name for name in found if not NOT_A_HOST.search(name) and not _numeric(name))


def _tld(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return last in GENERIC_TLDS or (len(last) == 2 and last.isalpha())


def _numeric(name: str) -> bool:
    try:
        ip_address(name)
    except ValueError:
        return name.replace(".", "").replace("-", "").isdigit()
    return True


def admitted_host(name: str) -> bool:
    return any(name == each or name.endswith(f".{each}") for each in (*RESERVED, *PUBLIC))


def unadmitted(source: str) -> list[str]:
    found = []
    for text in literals(source):
        found += [f"address {each}" for each in addresses(text) if not admitted_address(each)]
        found += [f"name {each}" for each in hosts(text) if not admitted_host(each)]
    return found


@tag("source")
class ExampleEstateValueTests(SimpleTestCase):
    maxDiff = None

    def test_every_address_and_name_is_made_up_or_everyones(self):
        files = sorted(ESTATE.glob("*.py"))
        self.assertTrue(files, f"nothing to read under {ESTATE}")
        found = {path.name: said for path in files if (said := unadmitted(path.read_text(encoding="utf-8")))}
        self.assertEqual(found, {})

    def test_an_address_or_name_of_someones_own_is_found(self):
        self.assertEqual(
            unadmitted('LAN = "8.8.8.8"\nV6 = "2606:4700::6810:84e5"\nHOST = "nas.someone.org"'),
            ["address 8.8.8.8", "address 2606:4700::6810:84e5", "name nas.someone.org"],
        )

    def test_what_the_estate_may_say_passes(self):
        self.assertEqual(
            unadmitted(
                'A = "198.51.100.7"\nB = "[2001:db8::24]:41641"\nC = "photos.example.com"\n'
                'D = "laptop.tailc0ffee.ts.net"\nE = "/opt/apps/hq/compose.yaml"\nF = "at 12:30:00"'
            ),
            [],
        )
