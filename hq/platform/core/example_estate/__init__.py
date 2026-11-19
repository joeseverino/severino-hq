"""The example estate: a small, well-run homelab, the one HQ is shown with.

Used by the showcase and standard data sets, the rendered-page and export
tests and, with crowding laid on top, the browser gate's crowded set; HQ
itself never reads it. Each thing left to point out is a named constant beside
the records it seeds, so the browser gate reads the page it changes.
"""

from django.contrib.auth import get_user_model
from django.utils import timezone

from hq.platform.application import cadence, first_seen, freshness, pins, problems, roles
from hq.platform.application.linked_accounts import GITHUB, record_claimed_accounts
from hq.platform.core.network import SOCKET_PEER

from . import agents, automation, docker, history, infra, network, records, web, work
from .names import HQ, LAPTOP, OWNER, OWNER_NAME, OWNER_USERNAME, PROXY, SSO

# The services the operator starred, to keep at the top of the list.
FAVORITES = (HQ, "photos.example.com")

__all__ = ("REQUEST", "SSO", "build", "example_owner", "seed")

# A reader's browser on the laptop, forwarded by the proxy, over HTTPS, as the
# forwarded-header middleware hands it on: the caller resolved, the proxy kept
# as the peer.
REQUEST = {
    "HTTP_HOST": HQ,
    "REMOTE_ADDR": LAPTOP,
    SOCKET_PEER: PROXY,
    "HTTP_X_FORWARDED_FOR": LAPTOP,
    "HTTP_X_REAL_IP": LAPTOP,
    "HTTP_X_FORWARDED_PROTO": "https",
    "HTTP_X_FORWARDED_SCHEME": "https",
}


def example_owner():
    """The user a page is rendered for when no operator signed up: the estate's owner."""

    first, last = OWNER_NAME
    owner = get_user_model().objects.create_user(
        username=OWNER_USERNAME, email=OWNER, password=None, first_name=first, last_name=last
    )
    roles.give_operator(owner)
    return owner


def build(owner):
    """The estate, owned by ``owner``: its records, their sign-in naming their
    GitHub account as the identity provider says it, and their favorites."""

    with history.dated(owner):
        record_claimed_accounts(owner, {GITHUB: "example"})
    seed(owner=owner)
    for hostname in FAVORITES:
        pins.toggle(owner, pins.SERVICE, hostname)


def seed(owner=None):
    """Every record of the estate, written into an empty database, the hand-kept
    ones by ``owner`` (``history.dated`` says who when none is named)."""

    with automation.numbered():
        _seed(owner)


def _seed(owner):
    with history.dated(owner):
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
        web.pages()
        web.analytics()
        web.contact()
        agents.seen(owner)
    history.settle()
    agents.lately(owner)
    # Last, so no later step's claim takes the run left waiting.
    automation.runs(owner)
    # A running HQ looked at its queue a cadence ago, so each item open now says since when.
    first_seen.note_open((), now=timezone.now() - freshness.cadence(freshness.FIRST_SEEN).every)
    problems.note_open_problems()
