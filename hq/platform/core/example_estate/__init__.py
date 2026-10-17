"""The example estate: a small, well-run homelab, the one HQ is shown with.

It seeds the public demo, a dev database (``manage.py seed_demo``), the
rendered-page and export tests, and the layout gate's realistic pass. The
gate's stress estate (``test_browser_fixtures.build_estate``) breaks things on
purpose so layouts are tested at their worst. This one is the opposite: an
HQ that has run for months. Every record is read, every service is set up,
every container is declared, running and current, scheduled work has run on
time, and the reader arrives the way they would in production, over the
tailnet through the proxy, on HTTPS, signed in with single sign-on. What is
left for HQ to point out is what a real homelab would have: an expense with no
receipt yet.

Everything is written through HQ's own models and the same helpers its tests
use; nothing here is read by HQ itself. Names stay in the ``example.*``
namespace and addresses in private or documentation ranges.
"""

import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings

from hq.domains.receipts.models import Receipt
from hq.platform.application import cadence

from . import docker, infra, network, records, work
from .names import HQ, HQ_REVISION, HQ_SOURCE, LAN, LAPTOP, OWNER, PROXY, SSO, container_id

__all__ = ("REQUEST", "SSO", "as_production", "build_showcase", "seed", "throwaway_media")

# A reader's browser on the laptop, forwarded by the proxy, over HTTPS.
REQUEST = {
    "HTTP_HOST": HQ,
    "REMOTE_ADDR": PROXY,
    "HTTP_X_FORWARDED_FOR": LAPTOP,
    "HTTP_X_REAL_IP": LAPTOP,
    "HTTP_X_FORWARDED_PROTO": "https",
    "HTTP_X_FORWARDED_SCHEME": "https",
}


@contextmanager
def as_production():
    """The settings and host facts a production request is answered under."""

    heartbeat = Path(tempfile.mkdtemp()) / "controller-heartbeat"
    with ExitStack() as stack:
        stack.enter_context(
            override_settings(
                ALLOWED_HOSTS=[HQ],
                SEVERINO_SITE_HOST=HQ,
                SEVERINO_TRUSTED_PROXIES=["172.18.0.0/16"],
                AUTHENTICATION_BACKENDS=[SSO],
                SESSION_COOKIE_SECURE=True,
                SESSION_COOKIE_NAME="__Host-sessionid",
                SECURE_SSL_REDIRECT=True,
                SECURE_SSL_HOST=HQ,
                SEVERINO_CONTROLLER_HEARTBEAT=str(heartbeat),
                # The commit this build says it is, and where it came from.
                SEVERINO_HQ_REVISION=HQ_REVISION,
                SEVERINO_HQ_SOURCE=HQ_SOURCE,
                # A saved copy: what a page says about the reader is an example, not the reader's.
                SEVERINO_RENDERED_COPY=True,
            )
        )
        # Facts about the host HQ runs on, which no database row holds.
        stack.enter_context(mock.patch("hq.platform.application.hq_self.host_addresses", return_value=frozenset({LAN})))
        stack.enter_context(
            mock.patch("hq.platform.application.request_path.own_container_id", return_value=container_id("hq"))
        )
        stack.enter_context(throwaway_media())
        yield


@contextmanager
def throwaway_media():
    """Receipt files go to a throwaway folder, never the configured one."""

    storage = Receipt._meta.get_field("file").storage
    media = tempfile.mkdtemp()
    with mock.patch.object(storage, "base_location", media), mock.patch.object(storage, "location", media):
        yield


def build_showcase():
    """Seed the estate and return the user its pages are rendered for."""

    user = get_user_model().objects.create_user(username="example", email=OWNER, password=None, first_name="Example")
    seed()
    return user


def seed():
    """Every record of the estate, written into an empty database."""

    # The controller checked in a moment ago; a fresh install's never has.
    cadence.note_controller()
    records.projects()
    infra.declare()
    infra.connections()
    infra.proxy()
    infra.host()
    docker.containers()
    network.adguard()
    network.zone()
    network.tailnet()
    work.repositories()
    work.units()
    work.jobs()
    records.calendar()
    records.money()
