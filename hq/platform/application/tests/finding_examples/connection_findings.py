from hq.domains.control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL
from hq.platform.core.testing import connection, declare

from ...finding_steps import Do
from ...report_testing import report_connections
from . import Example, Examples, Raised, stored

OPERATOR = "someone@example.test"
# HQ's machine, which took the reading, and an edge that admits it on 80 alone.
TAILNET = {
    "tailscale.device": (
        {"name": "a-controller", "user": OPERATOR, "addresses": ["100.64.0.1"], "self": True},
        {
            "name": "an-edge",
            "addresses": ["100.64.0.2"],
            "reach": [{"port": 80, "who": [OPERATOR], "rules": []}, {"port": 443, "who": ["tag:admin"], "rules": []}],
        },
    )
}
RECONCILE = "infrastructure.resource.reconcile"


def ssh(*, reachable: bool, raises: tuple[Raised | str, ...] = ()) -> Example:
    failed = {} if reachable else {"detail": "Timed out", "failure": "network"}
    return Example(
        given=lambda: connection("ssh", "example-ssh", endpoint="192.0.2.9:22", reachable=reachable, **failed),
        raises=raises,
    )


def npm(detail: str, failure: str) -> None:
    """A Nginx Proxy Manager connection whose last probe failed for ``failure``."""

    report_connections(
        [
            {
                "connection_ref": "example-npm",
                "provider": "npm",
                "endpoint": "https://proxy.example.com",
                "ok": False,
                "detail": detail,
                "failure": failure,
            }
        ],
        controller_id="example-controller",
    )


def signed_in() -> None:
    """A connection whose address answered with a sign-in page, not the API."""

    npm("The address answered with a sign-in page at sso.example.com, not the API.", "address")


def certificate(*consumers: dict, title: str = "", presses=(RECONCILE,), readings=None) -> Example:
    """A certificate whose last sweep could not reach ``consumers``."""

    return Example(
        readings=readings or {},
        given=lambda: declare("a-certificate", "tls.certificate", status={"unreachable_consumers": list(consumers)}),
        raises=(Raised(title=title, presses=presses),) if title else (),
    )


EXAMPLES = (
    Examples(
        "connection-not-answering",
        fires=(
            # A probe that got no answer asks to check the machine it named.
            ssh(
                reachable=False,
                raises=(
                    Raised(
                        title="example-ssh is not answering",
                        steps=(Do("Check that 192.0.2.9 is up and the controller can reach it"),),
                    ),
                ),
            ),
            Example(
                given=lambda: npm("Provider request failed: URLError.", "network"),
                raises=(
                    Raised(
                        title="example-npm is not answering",
                        steps=(Do("Check that proxy.example.com is up and the controller can reach it"),),
                    ),
                ),
            ),
            # A credential refused says refused, in the provider's words.
            Example(
                readings={"cloudflare.pages_project": stored(reachable=False, refusal=CREDENTIAL_REFUSAL, error="x")},
                given=lambda: connection("cloudflare_api", "example-cf"),
                raises=("example-cf: credential refused",),
            ),
            # An address that is not the API is said to be one.
            Example(given=signed_in, raises=("example-npm: the address is not Nginx Proxy Manager's API",)),
        ),
        quiet=(ssh(reachable=True),),
    ),
    Examples(
        "unreachable-consumer",
        fires=(
            certificate({"domain": "health.example.com"}, title="Could not check a-certificate on health.example.com"),
            # Every name it could not read, since each is a place it may be stale.
            certificate(
                {"domain": "one.example.com"},
                {"domain": "two.example.com"},
                {"domain": "three.example.com"},
                title="Could not check a-certificate in 3 places",
            ),
            # The tailnet refusing the path is offered the amendment to it.
            certificate(
                {"domain": "app.example.com", "endpoint": "100.64.0.2", "port": "443"},
                readings=TAILNET,
                title="Could not check a-certificate on app.example.com",
                presses=("tailnet.reach.allow", RECONCILE),
            ),
            # A path the policy admits is not sent at the policy.
            certificate(
                {"domain": "app.example.com", "endpoint": "100.64.0.2", "port": "80"},
                readings=TAILNET,
                title="Could not check a-certificate on app.example.com",
            ),
        ),
        quiet=(certificate(),),
    ),
)
