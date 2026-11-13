"""HQ's own side: the repositories it reads, the units it ships, and months of
scheduled work behind it."""

from datetime import timedelta

from django.utils import timezone

from hq.domains.content.content_sync import sync_content_index
from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations.github import DEPENDENCY_RUN_KIND
from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND
from hq.domains.control_plane.provider_adapters import github as delivery
from hq.domains.jobs import runner
from hq.domains.jobs.models import Job
from hq.platform.application import scheduled_work
from hq.platform.application.images import ImageRef
from hq.platform.core.audit import stamped

from .names import CONTAINERS, GITHUB, HQ_DIGEST, HQ_REVISION, NEWER, OWNER_NAME, SOURCES, hexed, iso, stamp
from .sweep import store

# The repositories HQ's GitHub App reads, each kept the way the repo checks ask.
REPOSITORIES = ("hq", "homelab", "photo-library", "media-server", "git-server", "backups", "dotfiles")
# The repository HQ opens dependency updates for.
UPDATED = "photo-library"
# The scheduled work HQ declares that runs on a clock, and how many runs to show.
SCHEDULED = tuple((work.name, work.every) for work in scheduled_work.SCHEDULED if work.every is not None)
RUNS = 3
CONTENT_SYNC = "content.sync"
# What the site's index says of each writeup: its description and what it uses.
DESCRIBED = {
    "why-a-control-panel": ("One place that knows every host, route and certificate.", ["Django", "Go"]),
    "photos-off-the-cloud": ("Forty thousand photos moved home, and how they stay backed up.", ["Immich"]),
    "media-server-on-the-tv": ("A media server the living room finds without a setup screen.", ["Jellyfin"]),
    "mirroring-private-git": ("A self-hosted git server that mirrors to GitHub on every push.", ["Forgejo"]),
    "tailnet-only": ("Every service reachable only over the tailnet.", ["Tailscale"]),
    "restored-backups": ("A backup counts once it has been restored.", ["restic", "Backblaze B2"]),
}
# The units deploy/systemd ships: each timer or path and what it starts.
# When each last started it, and how long until it next does; a path unit
# starts its service when a file changes, so has no next time.
STARTERS = (
    ("severino-hq-backup.timer", "severino-hq-backup.service", timedelta(hours=9), timedelta(hours=15)),
    ("severino-hq-controller.timer", "severino-hq-controller.service", timedelta(minutes=4), timedelta(minutes=1)),
    ("severino-hq-controller.path", "severino-hq-controller.service", timedelta(minutes=4), None),
    ("severino-hq-due.timer", "severino-hq-job@due.service", timedelta(minutes=31), timedelta(minutes=29)),
    ("severino-hq-script-drift.timer", "severino-hq-script-drift.service", timedelta(hours=7), timedelta(hours=17)),
    ("severino-hq-secrets.timer", "severino-hq-secrets.service", timedelta(minutes=22), timedelta(minutes=38)),
)


# What each unit's file under deploy/systemd calls it, as systemd reports it:
# a template's instance name stands for its %i.
TITLES = {
    "run-severino\\x2dhq\\x2dsecrets.mount": "Severino HQ credential tmpfs",
    "severino-hq-backup.service": "Create and verify a Severino HQ backup",
    "severino-hq-backup.timer": "Create a nightly verified Severino HQ backup",
    "severino-hq-controller.path": "Start the Severino HQ controller when HQ has queued work",
    "severino-hq-controller.service": "Severino HQ infrastructure controller",
    "severino-hq-controller.timer": "Run Severino HQ infrastructure controller",
    "severino-hq-due.timer": "Run due Severino HQ work",
    "severino-hq-job@due.service": "Severino HQ scheduled work: due",
    "severino-hq-script-drift.service": "Check the host matches its Severino HQ release",
    "severino-hq-script-drift.timer": "Daily check for stale root-owned Severino HQ scripts",
    "severino-hq-secrets.service": "Write Severino HQ secrets from 1Password Connect",
    "severino-hq-secrets.timer": "Periodically refresh Severino HQ secrets from 1Password",
    "severino-hq-unit.socket": "Severino HQ's requests to stop, start or restart a container's unit",
    "severino-hq-web.service": "Severino HQ",
}


def repositories():
    at = timezone.now()
    access = {
        "collaborators": [{"login": "example", "role": "admin"}],
        "deploy_keys": [{"title": "deploy", "read_only": True, "last_used": at.isoformat()}],
        "token": "read",
        "token_approves_reviews": False,
        "pinning_required": True,
        "vulnerability_alerts": True,
        "security": None,
    }

    def repository(name):
        head = HQ_REVISION if name == "hq" else hexed("head", name, length=40)
        record = {
            "connection_ref": GITHUB,
            "repository": f"example/{name}",
            "url": f"https://github.com/example/{name}",
            "default_branch": "main",
            "pushed_at": iso(timedelta(days=REPOSITORIES.index(name) + 1)),
            "head": {"sha": head},
            "checks": {"state": "success", "total": 4, "failing": [], "running": 0},
            "alerts": {"dependabot": {}},
            "private": True,
            "access": access,
            "variables": [],
            "pins": [],
        }
        if name == "hq":
            record["deployments"] = [
                {
                    "environment": "production",
                    "sha": head,
                    "state": "success",
                    "created_at": iso(timedelta(days=1)),
                    "url": "https://github.com/example/hq/deployments/production",
                    "verified": [{"name": "Verify the image signature", "conclusion": "success"}],
                }
            ]
            record["environments"] = [
                {"name": "production", "reviewers": ["example"], "wait_minutes": 0, "branches": ["main"]}
            ]
            record["images"] = [{"name": "example/hq", "signed": [HQ_DIGEST.removeprefix("sha256:")]}]
        if name == UPDATED:
            record.update(_updates(name))
        return record

    store("github.repository", *(repository(name) for name in REPOSITORIES))
    # This morning's update run of the opted-in repository, finished cleanly.
    started = timedelta(hours=5)
    store(
        DEPENDENCY_RUN_KIND,
        {
            "connection_ref": GITHUB,
            "owner": "example",
            "repositories": [f"example/{UPDATED}"],
            "outcome": "ok",
            "exit_code": 0,
            "started_at": iso(started),
            "finished_at": iso(started - timedelta(minutes=6)),
        },
        age=started,
    )
    # Production runs what was last approved, as the deploy workflow reports it.
    deploys = {
        "repository": "example/hq",
        "workflow": delivery.COMPOSE_WORKFLOW,
        "branch": delivery.MAIN_BRANCH,
        "production": delivery.CURRENT,
    }
    ManagedResource.objects.create(
        key="delivery",
        kind=delivery.KIND,
        spec=deploys,
        status={**deploys, "extensions": []},
        generation=1,
        observed_generation=1,
        last_observed_at=at,
        conditions=[{"type": "Ready", "status": True, "reason": "Reconciled", "message": ""}],
    )
    store(delivery.KIND, {**deploys, "connection_ref": GITHUB, "extensions": []})
    store(
        "github.profile",
        {
            "login": "example",
            "name": " ".join(OWNER_NAME),
            "bio": "Runs a small homelab.",
            "url": "https://github.com/example",
            "public_repos": 4,
            "followers": 12,
            "following": 9,
            "created_at": "2016-05-21T18:04:00Z",
            "location": "Fairview, IL",
            "starred": 38,
            "watched": _watched(),
        },
    )


# The projects the estate runs, starred on GitHub to follow their releases:
# image, what it is, its language, its stars, and how long ago it was starred.
STARRED = (
    ("immich", "Self-hosted photo and video management.", "TypeScript", 58_400, 300),
    ("jellyfin", "The free software media system.", "C#", 39_200, 330),
    ("forgejo", "A self-hosted lightweight software forge.", "Go", 1_900, 290),
    ("home-automation", "Home automation server images, rebuilt weekly.", "Shell", 410, 260),
    ("adguard", "Network-wide ads and trackers blocking DNS server.", "Go", 27_600, 380),
    ("nginx-proxy-manager", "Expose your services easily and securely.", "TypeScript", 25_100, 380),
    ("portainer", "Making Docker and Kubernetes management easy.", "TypeScript", 32_300, 380),
)


def _watched():
    """Each starred project's latest release: the version that runs, or the
    newer one where there is one, as the registries report them."""

    tags = {name: ImageRef.parse(image).tag for name, _port, image in CONTAINERS}
    watched = []
    for name, about, language, stars, days in STARRED:
        source = SOURCES[name]
        tag = NEWER.get(name, tags[name])
        watched.append(
            {
                "name": source.removeprefix("https://github.com/"),
                "url": source,
                "description": about,
                "language": language,
                "stars": stars,
                "starred_at": stamp(timedelta(days=days)),
                "release": {
                    "tag": tag,
                    "url": f"{source}/releases/tag/{tag}",
                    "published_at": stamp(timedelta(days=4 if name in NEWER else 20)),
                },
                "advisories": [],
            }
        )
    return watched


def _updates(name):
    """One update open with its checks passing, and an alert no update answers yet."""

    url = f"https://github.com/example/{name}"
    return {
        "pull_requests": [
            {
                "number": 41,
                "title": "Update dependency example-thumbnailer to v3.2.1",
                "url": f"{url}/pull/41",
                "author": "renovate[bot]",
                "draft": False,
                "created_at": iso(timedelta(days=3)),
                "updated_at": iso(timedelta(days=1)),
                "head": hexed("update", name, length=40),
                "branch": "renovate/example-thumbnailer-3.x",
                "labels": ["dependencies"],
                "checks": {"state": "success", "total": 4, "failing": [], "running": 0, "names": ["lint", "test"]},
            }
        ],
        "alerts": {"dependabot": {"medium": 1}},
        "dependabot_alerts": [
            {
                "number": 7,
                "package": "example-exif",
                "ecosystem": "pip",
                "manifest_path": "uv.lock",
                "severity": "medium",
                "first_patched_version": "1.8.2",
                "url": f"{url}/security/dependabot/7",
            }
        ],
    }


def jobs():
    """Each piece of scheduled work, done on time and done well, a few times over.
    The latest content sync found the site's index describing each writeup."""

    for kind, every in SCHEDULED:
        for run in range(RUNS, 0, -1):
            work = _described if kind == CONTENT_SYNC and run == 1 else _nothing
            _ran(kind, every * run - timedelta(minutes=7), work)


def _nothing(_progress):
    return {}


def _described(_progress):
    """The site's index, each writeup now with its description and what it uses."""

    return sync_content_index(
        {
            "items": [
                {
                    "slug": item.slug,
                    "title": item.title,
                    "url": item.published_url,
                    "published_at": item.published_at.isoformat(),
                    "description": DESCRIBED[item.slug][0],
                    "technologies": DESCRIBED[item.slug][1],
                }
                for item in ContentItem.objects.filter(slug__in=DESCRIBED).order_by("published_at")
            ]
        }
    )


def _ran(kind, ago, work):
    """One run the scheduler asked for, ended through the runner so its row in
    the log is the one a real run writes, and every row its work wrote beneath
    it, dated to when it finished."""

    finished = timezone.now() - ago
    with stamped(lambda *_: {"created_at": finished}):
        job = runner.run(kind, scheduled_work.named(kind).label, work, actor=runner.SCHEDULER)
    Job.objects.filter(pk=job.pk).update(
        created_at=finished - timedelta(seconds=5),
        started_at=finished - timedelta(seconds=4),
        finished_at=finished,
        heartbeat_at=finished,
    )


def units():
    """systemd's word on each unit HQ ships and on a service it keeps
    restarting, and the last credentials render."""

    found = [
        {
            "unit": "severino-hq-web.service",
            "load": "loaded",
            "file_state": "enabled",
            "active": "active",
            "sub": "running",
            "started_at": stamp(timedelta(days=12)),
            "read_at": stamp(),
        },
        # Units that do their work while active: a socket listening for the
        # next connection, and the tmpfs the credentials are rendered to.
        *(
            {
                "unit": unit,
                "load": "loaded",
                "file_state": "enabled",
                "active": "active",
                "sub": sub,
                "started_at": stamp(timedelta(days=12)),
                "read_at": stamp(),
            }
            for unit, sub in (
                ("severino-hq-unit.socket", "listening"),
                ("run-severino\\x2dhq\\x2dsecrets.mount", "mounted"),
            )
        ),
    ]
    started = {}
    for starter, service, last, after in STARTERS:
        unit = {
            "unit": starter,
            "load": "loaded",
            "file_state": "enabled",
            "active": "active",
            "sub": "waiting",
            "activates": service,
            "read_at": stamp(),
        }
        if after is not None:
            unit.update(last_trigger_at=stamp(last), next_elapse_at=stamp(-after))
        found.append(unit)
        started[service] = min(last, started.get(service, last))
    found.extend(
        {
            "unit": service,
            "load": "loaded",
            "file_state": "static",
            "active": "inactive",
            "sub": "dead",
            "result": "success",
            "main_code": 1,
            "started_at": stamp(last),
            "ended_at": stamp(last - timedelta(seconds=6)),
            "condition": "yes",
            "condition_at": stamp(last),
            "read_at": stamp(),
        }
        for service, last in started.items()
    )
    # A service HQ does not ship that systemd has kept restarting for days,
    # read beside HQ's own units because it restarts.
    looping = {
        "unit": "example-upload.service",
        "title": "Example log upload",
        "load": "loaded",
        "file_state": "enabled",
        "active": "activating",
        "sub": "auto-restart",
        "result": "exit-code",
        "main_code": 1,
        "main_status": 1,
        "restarts": 8641,
        "active_at": stamp(timedelta(seconds=40)),
        "ended_at": stamp(timedelta(seconds=10)),
        "foreign": True,
        "read_at": stamp(),
    }
    store(UNIT_KIND, *({**unit, "title": TITLES[unit["unit"]]} for unit in found), looping)
    store(
        RENDER_STATUS_KIND,
        {
            "renderer": "hq",
            "state": "read",
            "status": {
                "last_attempt": {"at": stamp(timedelta(minutes=22)), "outcome": "current"},
                "last_success": {
                    "at": stamp(timedelta(minutes=22)),
                    "rendered_at": stamp(timedelta(hours=5)),
                    "counts": {
                        "items_read": 7,
                        "connections": 7,
                        "app_variables": 4,
                        "identities": 1,
                        "signing_keys": 1,
                    },
                },
            },
        },
    )
