from datetime import timedelta

from hq.domains.analytics.models import AnalyticsSite, RumDaily

from ...connections import ConnectionInstance, ConnectionLink, ConnectionSpec
from ...plugin_testing import offering
from ...security import Capability
from . import AT, Example, Examples, Raised, connection

PERIMETER = "host.perimeter"


def an_edge() -> None:
    connection("example-edge", "ssh")


def perimeter(raises: tuple[Raised | str, ...] = (), **record) -> Example:
    """The edge's perimeter as its controller tested it: shut, by default."""

    return Example(
        readings={
            PERIMETER: (
                {
                    "record": "perimeter",
                    "connection_ref": "example-edge",
                    "firewall_unit": "active",
                    "public_addresses": ["203.0.113.5"],
                    "ports_checked": [22, 80, 443],
                    "answered_publicly": [],
                    **record,
                },
            )
        },
        given=an_edge,
        raises=raises,
    )


def reaching(*hosts: str, measured: tuple[str, ...] = (), raises: tuple[Raised | str, ...] = ()) -> Example:
    """A connection that reaches ``hosts``, and analytics counting visitors to those ``measured``."""

    def given():
        for host in measured:
            site = AnalyticsSite.objects.create(site_tag=f"tag-{host}", host=host)
            RumDaily.objects.create(
                site=site,
                date=AT.date() - timedelta(days=1),
                dimension=RumDaily.Dimension.PATH,
                value="/",
                pageviews=250,
                visits=200,
                sample_interval=1,
            )
        instance = ConnectionInstance(
            "one",
            "One",
            "example",
            "good",
            "Healthy",
            targets=tuple(ConnectionLink(host) for host in hosts),
            credential_model="none",
        )
        return offering(
            ConnectionSpec(
                "example.reach",
                "Reaching routes",
                "A synthetic connection family that reaches named hosts.",
                Capability.READ,
                lambda: (instance,),
            ),
            alone=True,
        )

    return Example(given=given, raises=raises)


EXAMPLES = (
    Examples(
        "perimeter-open",
        fires=(perimeter(answered_publicly=[443], raises=("example-edge is open to the internet on port 443",)),),
        quiet=(perimeter(),),
    ),
    Examples(
        "perimeter-unchecked",
        fires=(
            # An empty probe set is not a pass.
            perimeter(ports_checked=[], raises=("example-edge not tested for open ports",)),
            # No public address checked nothing either.
            perimeter(public_addresses=[], raises=("example-edge not tested for open ports",)),
        ),
        quiet=(perimeter(), perimeter(answered_publicly=[443])),
    ),
    Examples(
        "firewall-stopped",
        fires=(perimeter(firewall_unit="inactive", raises=("example-edge's firewall is not running",)),),
        quiet=(perimeter(),),
    ),
    Examples(
        "reached-but-unmeasured",
        fires=(
            # An unmeasured name beside a measured one.
            reaching(
                "counted.example.com",
                "uncounted.example.com",
                measured=("counted.example.com",),
                raises=("uncounted.example.com has no visitor counts",),
            ),
        ),
        quiet=(
            # A connection nothing measures is not a per-name gap.
            reaching("a.example.com", "b.example.com"),
            # A fully measured one.
            reaching("a.example.com", "b.example.com", measured=("a.example.com", "b.example.com")),
        ),
    ),
)
