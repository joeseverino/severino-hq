"""A certificate spec and the three places it is installed, shared by the control plane's tests."""

from ..models import ManagedResource

DOMAINS = [
    "example.com",
    "*.example.com",
    "example.net",
    "*.example.net",
    "example.org",
    "*.example.org",
    "example.test",
    "*.example.test",
]


def certificate_spec():
    return {
        "certificate_name": "example",
        "domains": list(DOMAINS),
        "install_on": ["example-npm", "edge", "a-shared-host"],
        "renewal_window_days": 30,
    }


TARGETS = (
    {
        "kind": "npm",
        "connection_ref": "example-npm",
        "name": "example-wildcard",
        "certificate_resource": "example-wildcard",
        "verify_domains": ["dev.example.com"],
        "discover_covered_hosts": False,
    },
    {
        "kind": "caddy",
        "connection_ref": "edge",
        "name": "edge-caddy",
        "certificate_resource": "example-wildcard",
        "verify_domains": ["health.example.com"],
        "certificate_directory": "/opt/apps/caddy/certs",
    },
    {
        "kind": "cpanel",
        "connection_ref": "a-shared-host",
        "name": "namecheap-shared-hosting",
        "certificate_resource": "example-wildcard",
        "verify_domains": ["example.com", "quiz.example.net"],
        "install_domains": ["example.com", "example.net"],
    },
)


def declare_targets():
    """The three places the certificate in these tests is installed."""

    return [
        ManagedResource.objects.create(
            key=f"{spec['connection_ref']}-certificate-target",
            kind="tls.delivery_target",
            spec=dict(spec),
        )
        for spec in TARGETS
    ]
