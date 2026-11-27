"""HQ's own side: the repositories it reads, the units it ships, and months of
scheduled work behind it."""

from datetime import timedelta

from django.utils import timezone

from hq.domains.content.content_sync import sync_content_index
from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.observations.github import (
    DEPENDENCY_RUN_KIND,
    PULL_REQUEST_KIND,
    REPOSITORY_KIND,
    WORKFLOW_RUN_KIND,
)
from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND
from hq.domains.control_plane.provider_adapters import github as delivery
from hq.domains.jobs import runner
from hq.domains.jobs.models import Job
from hq.platform.application import outbound_work
from hq.platform.application.adoption import adopt_discovered
from hq.platform.application.conditions import READY
from hq.platform.application.images import ImageRef
from hq.platform.application.security import internal_principal
from hq.platform.application.sweep import ADOPT
from hq.platform.core import bindings
from hq.platform.core.audit import stamped

from . import doors
from .names import (
    BUILT,
    CONTAINERS,
    GITHUB,
    HQ_DIGEST,
    HQ_REVISION,
    MACHINE,
    NEWER,
    OWNER_NAME,
    SOURCES,
    back,
    digest,
    hexed,
    iso,
    share,
    stamp,
)
from .sweep import store

# The repositories HQ's GitHub App reads, each kept the way the repo checks ask.
REPOSITORIES = ("hq", "homelab", "photo-library", "media-server", "git-server", "backups", "dotfiles")
# The repository whose dependency update and alerts are open.
UPDATED = "photo-library"
# The repository whose default branch fails a check, so the queue holds GitHub's item.
FAILING = "homelab"
# The repository each app's Compose files live in, under ``compose/<app>/``,
# and the app its open update moves to the newer release.
APPS = "homelab"
UPDATED_APP = "home-automation"
# The app whose update the apps repository holds open beside it once each app
# stands in a state of its own: the DNS server every name resolves through.
RESOLVER_APP = "adguard"
# The repositories HQ opens dependency updates for: each with an update open.
OPTED_IN = (UPDATED, APPS)
# The apps whose Compose files the apps repository holds: every container but
# HQ, which its own pipeline releases.
HELD_APPS = tuple(name for name, _port, _image in CONTAINERS if name != "hq")
# How many runs of each piece of HQ's own work on a clock to show.
RUNS = 3
# The states the latest run of the first scheduled kinds is left in, one each.
LIVE = (Job.State.RUNNING, Job.State.QUEUED)
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
    "severino-hq-automation.service": "Severino HQ automation worker",
    "severino-hq-automation.timer": "Run the Severino HQ automation worker",
    "var-lib-severino\\x2dhq-automation.mount": "Severino HQ automation journal",
}


def repositories():
    at = timezone.now()
    access = {
        "collaborators": [{"login": "example", "role": "admin"}],
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
            # Each repository's deploy key last used when its machine last pulled it.
            "access": {
                **access,
                "deploy_keys": [
                    {
                        "title": "deploy",
                        "read_only": True,
                        "last_used": back(share(timedelta(days=6), name), name).isoformat(),
                    }
                ],
            },
            "variables": [],
            "pins": [],
        }
        if name == "hq":
            record["deployments"] = [
                {
                    "environment": "production",
                    "sha": head,
                    "image": f"ghcr.io/example/hq@{HQ_DIGEST}",
                    "state": "success",
                    "created_at": iso(timedelta(days=1)),
                    "url": "https://github.com/example/hq/deployments/production",
                    "verified": [{"name": "Verify the image signature", "conclusion": "success"}],
                }
            ]
            record["environments"] = [
                {"name": "production", "reviewers": ["example"], "wait_minutes": 0, "branches": ["main"]}
            ]
            # A deploy waiting on production's reviewer, approved on GitHub.
            record["waiting"] = [
                {
                    "id": 4100,
                    "name": "Deploy",
                    "environments": ["production"],
                    "sha": hexed("release", name, length=40),
                    "created_at": iso(timedelta(minutes=20)),
                    "url": "https://github.com/example/hq/actions/runs/4100",
                }
            ]
            record["images"] = [{"name": "example/hq", "signed": [HQ_DIGEST.removeprefix("sha256:")]}]
            # Its default branch's rules, with checks its pull requests run
            # that they do not require, and the runner its deploys run on.
            required = ["lint", "test (3.13)", "test (3.14)"]
            record["rules"] = {"pull_request": True, "required_checks": required, "blocks_force_push": True}
            record["pull_request_checks"] = [*required, "CodeQL", "Analyze (python)", "browser (webkit)"]
            record["runners"] = [{"name": "docker-host-01", "status": "online", "busy": False}]
        if name == UPDATED:
            record.update(_updates(name))
        if name == FAILING:
            # Its weekly scan failed on an older commit, and no run of it has
            # passed since.
            scan = {
                "name": "Scan",
                "url": f"https://github.com/example/{name}/actions/runs/201",
                "status": "completed",
                "conclusion": "failure",
                "event": "schedule",
                "branch": "main",
                "sha": hexed("older", name, length=40),
                "created_at": iso(timedelta(days=2)),
            }
            # Its log names the standard library release that fixes what it found.
            failure = {
                "step": "govulncheck",
                "error": "Process completed with exit code 3.",
                "log": [
                    "Vulnerability #1: GO-2026-6859",
                    "  Standard library",
                    "    Found in: net/http@go1.27.3",
                    "    Fixed in: net/http@go1.27.4",
                    "Your code is affected by 1 vulnerability from the Go standard library.",
                ],
            }
            # A check no workflow owns says why in its own output.
            denied = {
                "error": "License denied",
                "log": ["example-lib 1.2.0: GPL-3.0"],
            }
            record["checks"] = {
                "state": "failure",
                "total": 5,
                "failing": ["License", "Scan"],
                "running": 0,
                "failures": {"License": denied, "Scan": failure},
            }
            record["runs"] = [scan]
        if name == APPS:
            record["pull_requests"] = [_app_update(name)]
            # Renovate's Dashboard lists the updates it has not opened yet.
            record["compose"] = [f"compose/{app}/compose.yaml" for app in sorted(HELD_APPS)]
            record["compose_images"] = [_built()]
            record["dashboard"] = f"https://github.com/example/{name}/issues/12"
            record["updater"] = UPDATER
        return record

    _store_repositories([repository(name) for name in REPOSITORIES])
    # This morning's update run of the opted-in repositories, finished cleanly.
    started = timedelta(hours=5)
    store(
        DEPENDENCY_RUN_KIND,
        {
            "connection_ref": GITHUB,
            "owner": "example",
            "repositories": [f"example/{name}" for name in OPTED_IN],
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
        conditions=[{"type": READY, "status": True, "reason": "Reconciled", "message": ""}],
    )
    store(delivery.KIND, {**deploys, "connection_ref": GITHUB, "extensions": []})
    # The App's newest deliveries refused by the events Worker, its secret
    # out of step with the App's.
    store(
        "github.webhook",
        {
            "connection_ref": GITHUB,
            "app": "example-hq",
            "owner": "example",
            "events": ["push", "workflow_run", "check_suite", "pull_request", "deployment_status", "deployment_review"],
            "deliveries": 40,
            "failing": 3,
            "newest": {
                "event": "workflow_run",
                "action": "completed",
                "status": "Unauthorized",
                "status_code": 401,
                "delivered_at": iso(timedelta(minutes=12)),
            },
        },
    )
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
            "company": "Example Co",
            "location": "Fairview, IL",
            "website": "https://example.com",
            "social": "example",
            "public_gists": 3,
            "hireable": True,
            "starred": 38,
            "watched": _watched(),
        },
    )


# The projects the estate runs, starred on GitHub to follow their releases:
# image, what it is, its language, its stars, and how long ago it was
# starred; newest first, as GitHub lists an account's stars.
STARRED = (
    ("home-automation", "Home automation server images, rebuilt weekly.", "Shell", 410, 260),
    ("forgejo", "A self-hosted lightweight software forge.", "Go", 1_900, 290),
    ("immich", "Self-hosted photo and video management.", "TypeScript", 58_400, 300),
    ("jellyfin", "The free software media system.", "C#", 39_200, 330),
    ("adguard", "Network-wide ads and trackers blocking DNS server.", "Go", 27_600, 380),
    ("nginx-proxy-manager", "Expose your services easily and securely.", "TypeScript", 25_100, 395),
    ("portainer", "Making Docker and Kubernetes management easy.", "TypeScript", 32_300, 410),
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


# The login the example estate's update run opens its pull requests as.
UPDATER = "example-hq[bot]"


def _store_repositories(records: list[dict]) -> None:
    """Each repository's record, and its ``pull_requests`` and ``runs`` as the
    readings taken in its pass, each naming the repository."""

    pulls, runs = [], []
    for record in records:
        named = {"connection_ref": record["connection_ref"], "repository": record["repository"]}
        pulls += [{**named, **pull} for pull in record.pop("pull_requests", ())]
        runs += [{**named, **run} for run in record.pop("runs", ())]
    store(REPOSITORY_KIND, *records)
    store(PULL_REQUEST_KIND, *pulls)
    store(WORKFLOW_RUN_KIND, *runs)


def _app_update(name, app=UPDATED_APP, number=58, newer=NEWER):
    """Renovate's update of ``app``'s pin to the release ``newer`` names,
    checks passing, as GitHub lists the pull request and the reading loads
    its Compose file at base and head."""

    image = next(image for container, _port, image in CONTAINERS if container == app)
    ref = ImageRef.parse(image)
    after = f"{ref.name}:{newer[app]}@{digest(ref.name, newer[app])}"
    url = f"https://github.com/example/{name}"
    return {
        "number": number,
        "title": f"Update {ref.repository} Docker tag to {newer[app]}",
        "url": f"{url}/pull/{number}",
        "author": UPDATER,
        "draft": False,
        "created_at": iso(timedelta(days=2)),
        "updated_at": iso(timedelta(hours=4)),
        "head": hexed("update", name, app, length=40),
        "base": hexed("head", name, length=40),
        "branch": f"renovate/{app}",
        "head_repository": f"example/{name}",
        "labels": ["dependencies"],
        "checks": {"state": "success", "total": 2, "failing": [], "running": 0, "names": ["validate", "scan"]},
        "files": [
            {
                "path": f"compose/{app}/compose.yaml",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "app": app,
            }
        ],
        "changed_files": 1,
        "apps": [
            {
                "app": app,
                "read": True,
                "images": [{"service": app, "before": f"{image}@{digest(ref.name, ref.tag)}", "after": after}],
            }
        ],
    }


def _updates(name):
    """One update open with its checks passing and one failing, an alert no update answers
    yet, and a serious one no version fixes yet, which the queue leaves out."""

    url = f"https://github.com/example/{name}"
    return {
        "pull_requests": [
            {
                "number": 41,
                "title": "Update dependency example-thumbnailer to v3.2.1",
                "url": f"{url}/pull/41",
                "author": UPDATER,
                "draft": False,
                "created_at": iso(timedelta(days=3)),
                "updated_at": iso(timedelta(days=1)),
                "head": hexed("update", name, length=40),
                "branch": "renovate/example-thumbnailer-3.x",
                "labels": ["dependencies"],
                "checks": {"state": "success", "total": 4, "failing": [], "running": 0, "names": ["lint", "test"]},
            },
            {
                "number": 42,
                "title": "Update dependency example-uploader to v2",
                "url": f"{url}/pull/42",
                "author": UPDATER,
                "draft": False,
                "created_at": iso(timedelta(days=2)),
                "updated_at": iso(timedelta(days=1)),
                "head": hexed("uploader", name, length=40),
                "branch": "renovate/example-uploader-2.x",
                "labels": ["dependencies"],
                # Its lock did not move with it, as the update run said.
                "checks": {
                    "state": "failure",
                    "total": 4,
                    "failing": ["test"],
                    "running": 0,
                    "names": ["lint", "test"],
                    "failures": {
                        "test": {
                            "step": "Install tools",
                            "error": "The process '/home/runner/.local/bin/mise' failed with exit code 1",
                            "log": ["mise ERROR Failed to install example-uploader@2.0.0: not in the lockfile"],
                        }
                    },
                },
                "artifact_problem": ["mise ERROR cannot lock example-uploader: not configured in this project"],
            },
            {
                "number": 43,
                "title": "Read the thumbnail size from the library's settings",
                "url": f"{url}/pull/43",
                "author": "example",
                "draft": True,
                "created_at": iso(timedelta(days=2)),
                "updated_at": iso(timedelta(hours=6)),
                "head": hexed("draft", name, length=40),
                "branch": "thumbnail-size",
                "labels": [],
            },
        ],
        # Each workflow's newest run: the default branch's build passing, a
        # release still going, and a nightly check that passed on an older commit.
        "runs": [
            {
                "name": "CI",
                "url": f"{url}/actions/runs/101",
                "status": "completed",
                "conclusion": "success",
                "branch": "main",
                "sha": hexed("head", name, length=40),
                "created_at": iso(timedelta(days=4)),
            },
            {
                "name": "Release",
                "url": f"{url}/actions/runs/102",
                "status": "in_progress",
                "conclusion": "",
                "branch": "main",
                "sha": hexed("head", name, length=40),
                "created_at": iso(timedelta(minutes=20)),
            },
            {
                "name": "Nightly checks",
                "url": f"{url}/actions/runs/99",
                "status": "completed",
                "conclusion": "success",
                "branch": "main",
                "sha": hexed("older", name, length=40),
                "created_at": iso(timedelta(days=5)),
            },
        ],
        "alerts": {"dependabot": {"high": 1, "medium": 1}},
        "dependabot_alerts": [
            {
                "number": 7,
                "package": "example-exif",
                "ecosystem": "pip",
                "manifest_path": "uv.lock",
                "severity": "medium",
                "first_patched_version": "1.8.2",
                "url": f"{url}/security/dependabot/7",
            },
            {
                "number": 8,
                "package": "example-tiff",
                "ecosystem": "pip",
                "manifest_path": "uv.lock",
                "severity": "high",
                "first_patched_version": None,
                "url": f"{url}/security/dependabot/8",
            },
        ],
    }


def jobs():
    """Each piece of scheduled work, done on time and done well, a few times over.
    The latest content sync found the site's index describing each writeup."""

    clocked = [work for work in outbound_work.host_work() if work.every is not None]
    for declared in clocked:
        for run in range(RUNS, 0, -1):
            work = _described if declared.name == CONTENT_SYNC and run == 1 else _nothing
            _ran(declared, declared.every * run - timedelta(minutes=7), work)
    # A timer's run under way and one waiting its turn, so a page draws work in both states.
    now = timezone.now()
    for declared, state in zip(clocked, LIVE, strict=False):
        running = state == Job.State.RUNNING
        Job.objects.create(
            kind=declared.name,
            label=declared.label,
            actor=runner.SCHEDULER,
            request={outbound_work.SUBJECT_FIELD: ""},
            state=state,
            started_at=now if running else None,
            heartbeat_at=now if running else None,
        )


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


def _ran(declared, ago, work):
    """One run the scheduler asked for, ended through the runner so its row in
    the log is the one a real run writes, and every row its work wrote beneath
    it, dated to when it finished."""

    finished = timezone.now() - ago
    with stamped(lambda *_: {"created_at": finished}):
        job = runner.run(
            declared.name,
            declared.label,
            work,
            actor=runner.SCHEDULER,
            request={outbound_work.SUBJECT_FIELD: ""},
        )
    took = timedelta(seconds=2) + share(timedelta(seconds=40), declared.name, ago)
    Job.objects.filter(pk=job.pk).update(
        created_at=finished - took - timedelta(seconds=1),
        started_at=finished - took,
        finished_at=finished,
        heartbeat_at=finished,
    )


def _take_on_units() -> None:
    """Each unit the release ships declared, as the controller's sweep takes
    them on (``sweep.record_sweep``), so a unit's verbs are presses."""

    hq = internal_principal(doors.CONTROLLER)
    with doors.controller("sweep"), bindings.audit_operation(operation=ADOPT, principal=hq):
        adopt_discovered(UNIT_KIND, principal=hq)


def units(*more: dict):
    """systemd's word on each unit HQ ships and on a service it keeps
    restarting, then on ``more``, and the last credentials render."""

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
    found.extend(more)
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
    _take_on_units()
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


def worker_image_missing() -> None:
    """HQ's machine as a release that stopped short leaves it: the playbook
    worker's image never pulled, so its unit failed, and the journal's mount
    never started."""

    automation = (
        {
            "unit": "severino-hq-automation.timer",
            "load": "loaded",
            "file_state": "enabled",
            "active": "active",
            "sub": "waiting",
            "activates": "severino-hq-automation.service",
            "last_trigger_at": stamp(timedelta(minutes=4)),
            "next_elapse_at": stamp(-timedelta(minutes=11)),
            "read_at": stamp(),
        },
        {
            "unit": "severino-hq-automation.service",
            "load": "loaded",
            "file_state": "static",
            "active": "failed",
            "sub": "failed",
            "result": "exit-code",
            "main_code": 1,
            "main_status": 1,
            "started_at": stamp(timedelta(minutes=4)),
            "ended_at": stamp(timedelta(minutes=4) - timedelta(seconds=2)),
            "requires": ["sysinit.target", "var-lib-severino\\x2dhq-automation.mount"],
            "status": "the automation worker's image ghcr.io/example/hq-runner@sha256:"
            + "0" * 64
            + " is not on this host: `hq-host deploy` verifies and pulls it",
            "read_at": stamp(),
        },
        {
            "unit": "var-lib-severino\\x2dhq-automation.mount",
            "load": "loaded",
            "file_state": "static",
            "active": "inactive",
            "sub": "dead",
            "read_at": stamp(),
        },
    )
    units(*automation)


# How the app update's approval ends in each seed that draws one: its preview
# waiting for the playbook worker, refused on the host, its merge made and the
# apply refused, held on the pull unit it started, or applied.
PREVIEW_QUEUED, PREVIEW_FAILED, NOT_APPLIED, HELD = "queued", "preview-failed", "not-applied", "held"
# The steps a run stops on when the host refuses to start the pull.
_REFUSED = (
    ("Ask sudo whether the deploy login may start the pull unit", "ok", ""),
    ("Require sudo to let the deploy login start the pull unit", "failed", "sudo refuses to start the pull unit."),
)


# The apps repository's apps whose update stands where home-automation's
# open one does not: merged and not applied, held by Renovate, and a Compose
# file HQ can't match; and the app it no longer holds, moved by hand.
MERGED_APP, HELD_APP, UNMATCHED_APP, UNHELD_APP = "immich", "jellyfin", "nginx-proxy-manager", "portainer"


def unlisted() -> None:
    """The open app update's new pin as a publisher that attaches no package
    list leaves it: read, with nothing a scan can check."""

    from hq.domains.control_plane.observations.public_registry import DIGEST_KIND, VULNERABILITY_KIND

    image = ImageRef.parse(next(image for name, _port, image in CONTAINERS if name == UPDATED_APP))
    pinned = f"{image.name}@{digest(image.name, NEWER[UPDATED_APP])}"
    held = {kind: ProviderInventory.objects.get(kind=kind).records for kind in (DIGEST_KIND, VULNERABILITY_KIND)}
    store(DIGEST_KIND, *({**each, "packages": []} if each["digest"] == pinned else each for each in held[DIGEST_KIND]))
    store(VULNERABILITY_KIND, *(each for each in held[VULNERABILITY_KIND] if each["digest"] != pinned))


def update_states(newer: dict[str, str]) -> None:
    """The apps repository as Renovate and its merges leave it beside the
    open update, the newer releases ``newer`` names out."""

    held = ProviderInventory.objects.get(kind=REPOSITORY_KIND).records
    store(
        REPOSITORY_KIND,
        *(_states(record, newer) if record["repository"] == f"example/{APPS}" else record for record in held),
    )
    named = {"connection_ref": GITHUB, "repository": f"example/{APPS}"}
    store(
        PULL_REQUEST_KIND,
        *ProviderInventory.objects.get(kind=PULL_REQUEST_KIND).records,
        named | resolver_update(newer),
    )


def resolver_update(newer: dict[str, str]) -> dict:
    """The apps repository's open update of the DNS server, to the release ``newer`` names."""

    return _app_update(APPS, RESOLVER_APP, 59, newer)


def _built() -> dict:
    """The service of the apps repository whose image its host builds."""

    image = next(image for name, _port, image in CONTAINERS if name == BUILT)
    return {"path": f"compose/{BUILT}/compose.yaml", "service": BUILT, "image": image, "build": True}


def _states(record: dict, newer: dict[str, str]) -> dict:
    ref = ImageRef.parse(next(image for name, _port, image in CONTAINERS if name == MERGED_APP))
    kept = [path for path in record["compose"] if path.split("/")[1] not in (UNMATCHED_APP, UNHELD_APP)]
    return {
        **record,
        "compose": [*kept, f"compose/{UNMATCHED_APP}/docker-compose.yml"],
        "compose_images": [
            _built(),
            {
                "path": f"compose/{MERGED_APP}/compose.yaml",
                "service": MERGED_APP,
                "image": f"{ref.name}:{newer[MERGED_APP]}",
            },
        ],
        "held_branches": [{"branch": f"renovate/{HELD_APP}", "marker": "unpend", "apps": [HELD_APP]}],
    }


def _held(applied, merged: str) -> None:
    """``applied`` held on the pull unit its playbook started, a minute in,
    while the updated app's container is recreated on HQ's machine."""

    from hq.domains.control_plane.models import InboundEvent, InboundOutcome, InboundSource, OperationRequest
    from hq.domains.control_plane.observations.host import DOCKER_CONTAINER
    from hq.platform.application.resource_operations import HANDOFF

    now = timezone.now()
    claimed = now - timedelta(minutes=1)
    OperationRequest.objects.filter(pk=applied.pk).update(
        state=OperationRequest.State.CLAIMED,
        claimed_by="example-worker",
        claimed_at=claimed,
        completed_at=None,
        lease_expires_at=claimed + timedelta(seconds=1500),
        result={HANDOFF: {MACHINE: f"example-pull@{merged}.service"}},
    )
    for number, (event, ago) in enumerate((("destroy", 40), ("start", 35), ("health_status: healthy", 20)), start=1):
        InboundEvent.objects.create(
            provider="host",
            event=f"{DOCKER_CONTAINER}.{event}",
            subject=UPDATED_APP,
            delivery_id=f"00000000-0000-4000-9000-{number:012d}",
            source=InboundSource.HOST,
            received_at=now - timedelta(seconds=ago),
            applied_at=now - timedelta(seconds=ago),
            outcome=InboundOutcome.APPLIED,
        )


def approval(owner, ended: str) -> None:
    """The open app update approved and ended as ``ended`` says, each step
    queued as its approval queues it and ended as its worker leaves it."""

    from hq.domains.control_plane.models import OperationRequest
    from hq.domains.control_plane.provider_adapters.automation import OPERATION_KIND
    from hq.platform.application.automation_runs import CHECK
    from hq.platform.application.resource_operations import THEN, asked_fields, enqueue
    from hq.platform.application.security import web_principal

    update = _app_update(APPS)
    repository = f"example/{APPS}"
    # A playbook with no run waiting, since a record queues one at a time.
    waiting = (OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)
    playbook = (
        ManagedResource.objects.filter(kind=OPERATION_KIND)
        .exclude(operations__state__in=waiting)
        .order_by("key")
        .first()
    )
    hosts = [MACHINE]
    applies = {
        "operation": playbook.spec["operation"],
        "revision": playbook.spec["revision"],
        "hosts": hosts,
        "correlation": f"{repository}#{update['number']}@{update['head'][:12]}",
    }
    events = [{"host": MACHINE, "task": task, "outcome": outcome, "message": said} for task, outcome, said in _REFUSED]
    at = timezone.now() - timedelta(minutes=5)

    def record(resource, action, state, *, asked, result):
        fields = asked_fields(web_principal(owner), reason="", **asked)
        operation, _ = enqueue(action, lambda: (resource, fields))
        finished = {"state": state, "result": result, "claimed_at": at, "completed_at": at}
        OperationRequest.objects.filter(pk=operation.pk).update(**finished)
        return operation

    merging = {"head_sha": update["head"], "apps": [UPDATED_APP], THEN: applies}
    previewed = {**applies, "parameters": {"revision": update["head"]}, CHECK: True, THEN: merging}
    if ended == PREVIEW_QUEUED:
        enqueue("run", lambda: (playbook, asked_fields(web_principal(owner), reason="", **previewed)))
        return
    if ended == PREVIEW_FAILED:
        record(playbook, "run", "failed", asked=previewed, result={"message": "A task failed.", "events": events})
        return
    pull = ManagedResource.objects.create(
        key=f"example-pull-request-{APPS}-{update['number']}",
        kind=delivery.PULL_REQUEST_KIND,
        spec={"repository": repository, "number": update["number"], "head_sha": update["head"]},
    )
    merged = hexed("merged", APPS, length=40)
    failed = ended == NOT_APPLIED
    applied = record(
        playbook,
        "run",
        "failed" if failed else "succeeded",
        asked={**applies, "parameters": {"revision": merged}},
        result={"message": "A task failed.", "events": events}
        if failed
        else {
            "stated": {
                "readings": {MACHINE: {"apps.receipt": [{"revision": merged, "last_attempt": merged, "ok": True}]}}
            }
        },
    )
    if ended == HELD:
        _held(applied, merged)
    record(
        pull,
        "merge",
        "succeeded",
        asked={**merging, "correlation": applies["correlation"]},
        result={"status": {"merge_sha": merged}, THEN: {"run": str(applied.pk)}},
    )
