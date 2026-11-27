from hq.platform.core.testing import declare

from . import Example, Examples, Raised

HQ = "hq.example.com"
# HQ's own name, as a deployment declares it.
SERVED = {"SEVERINO_SITE_HOST": HQ, "ALLOWED_HOSTS": [HQ, "testserver"]}


def proxy(limit: int, *, names: tuple[str, ...] = (HQ,), raises: tuple[Raised, ...] = ()) -> Example:
    """A proxy host in front of ``names`` letting through bodies of ``limit`` MB."""

    spec = {
        "domain_names": list(names),
        "forward_scheme": "http",
        "forward_host": "192.0.2.10",
        "forward_port": 8000,
        "force_ssl": False,
        "client_max_body_size": limit,
    }
    return Example(given=lambda: declare("example-proxy", "npm.proxy_host", spec), settings=SERVED, raises=raises)


EXAMPLES = (
    Examples(
        "proxy-refuses-uploads",
        fires=(
            # A receipt is 15 MB, so a proxy letting through 1 MB refuses it.
            proxy(
                1,
                raises=(
                    Raised(
                        title="example-proxy refuses uploads over 1 MB",
                        presses=("infrastructure.resource.uploads.allow",),
                    ),
                ),
            ),
        ),
        quiet=(
            # Nginx Proxy Manager's own limit, which is far above any upload.
            proxy(0),
            # Above anything HQ takes.
            proxy(100_000),
            # A proxy for another name is not in front of HQ.
            proxy(1, names=("shop.example.com",)),
        ),
    ),
)
