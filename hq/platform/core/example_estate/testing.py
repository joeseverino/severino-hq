"""The example estate's stand-ins for what no database row holds: the host a
production request is answered on, and where receipt files are written."""

import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from django.test import override_settings

from hq.domains.receipts.models import Receipt

from .names import HQ, HQ_REVISION, HQ_SOURCE, LAN, SSO, container_id

__all__ = ("as_production", "throwaway_media")


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
