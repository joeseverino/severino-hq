"""What the GitHub App reads about each repository its installation covers.

One record per repository (``observations.github``). Every call runs under a
token scoped to that repository alone, with read permissions only: the App
may hold more, and a reading asks for none of it. One such token serves every
call for its repository in a sweep.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from ..observations.github import (
    BRANCH_RULES,
    CODE_SCANNING,
    DEPENDABOT,
    ENVIRONMENTS,
    LEAKED_CREDENTIALS,
    ACCESS,
    IMAGES,
    VARIABLES,
    REPOSITORY_KIND,
    RUNNERS,
)
from . import github_app
from .contracts import PERMISSION_REFUSAL, ProviderError, ProviderRuntime
from .parts import refuse_part

READ = {
    "metadata": "read",
    "contents": "read",
    "checks": "read",
    "pull_requests": "read",
    "actions": "read",
    "deployments": "read",
    "security_events": "read",
    "vulnerability_alerts": "read",
    "secret_scanning_alerts": "read",
    "administration": "read",
    "packages": "read",
    "actions_variables": "read",
}
REGISTRY_HOST = "ghcr.io"
_FAILED = frozenset({"failure", "timed_out", "action_required", "startup_failure"})
_RUNNING = frozenset({"queued", "in_progress", "requested", "pending", "waiting"})


def read_repositories(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    ref = github_app._ref(runtime, "")
    return [_repository(runtime, name, ref) for name in github_app.installation_repositories(runtime)]


def _repository(runtime: ProviderRuntime, name: str, ref: str) -> dict[str, Any]:
    def get(path: str) -> Any:
        return github_app.call(runtime, f"/repos/{name}{path}", repositories=[name], permissions=READ)

    repo = get("") or {}
    branch = str(repo.get("default_branch") or "main")
    head = get(f"/commits/{github_app.quote(branch)}") or {}
    sha = str(head.get("sha") or "")
    runs = (get("/actions/runs?per_page=50") or {}).get("workflow_runs") or []
    return {
        "connection_ref": ref,
        "repository": name,
        "private": bool(repo.get("private")),
        "url": str(repo.get("html_url") or ""),
        "default_branch": branch,
        "pushed_at": str(repo.get("pushed_at") or ""),
        "head": _head(head),
        "checks": _checks(get(f"/commits/{sha}/check-runs?per_page=100") if sha else None),
        "pull_requests": (pulls := _pulls(get("/pulls?state=open&per_page=30") or [])),
        # What runs on a pull request, the only checks a rule can require.
        "pull_request_checks": (
            _checks(get(f"/commits/{pulls[0]['head']}/check-runs?per_page=100"))["names"] if pulls and pulls[0]["head"] else []
        ),
        "runs": _latest_runs(runs, (get("/actions/workflows?per_page=100") or {}).get("workflows")),
        "waiting": _waiting(runs, get),
        "release": _release(get("/releases?per_page=1") or []),
        "deployments": _deployments(get("/deployments?per_page=20") or [], get),
        "alerts": _alerts(get, name, ref),
        "artifacts": _artifacts((get("/actions/artifacts?per_page=50") or {}).get("artifacts") or []),
        "rules": _part(BRANCH_RULES, name, ref, lambda: _rules(get(f"/rules/branches/{github_app.quote(branch)}") or [])),
        "environments": _part(ENVIRONMENTS, name, ref, lambda: _environments((get("/environments") or {}).get("environments") or []), []),
        "runners": _part(RUNNERS, name, ref, lambda: _runners((get("/actions/runners") or {}).get("runners") or []), []),
        "images": _images(runtime, name, ref),
        "access": _part(ACCESS, name, ref, lambda: _access(repo, get)),
        "variables": _part(VARIABLES, name, ref, lambda: [str(item.get("name")) for item in (get("/actions/variables?per_page=100") or {}).get("variables") or ()]),
    }


def _part(part, name: str, ref: str, read, empty: Any = None) -> Any:
    """One refusable part: what it read, or ``empty`` with the refusal reported."""

    try:
        return read()
    except ProviderError as exc:
        refuse_part(part.name, _as_repository_refusal(exc), scope=name, connection_ref=ref)
        return empty


def _as_repository_refusal(exc: ProviderError) -> ProviderError:
    """A refusal under a token that carries the permission is the repository's.

    Every read here runs under a token minted with all of ``READ``, and GitHub
    will not mint one for a permission the installation lacks. So a 403 is a
    feature the repository does not offer (code scanning or rulesets on a free
    private repository, Dependabot alerts turned off), never a permission to
    grant. Reporting it as one sends the operator to re-mint a key that
    already has everything.
    """

    if getattr(exc, "refusal", "") != PERMISSION_REFUSAL:
        return exc
    return ProviderError(
        "Not offered on this repository: its plan or its settings leave it off."
    )


def _access(repo: Mapping[str, Any], get) -> dict[str, Any]:
    """Who can reach the repository and how its Actions may run: the facts a
    posture standard is checked against."""

    security = repo.get("security_and_analysis") or {}
    actions = get("/actions/permissions") or {}
    workflow = get("/actions/permissions/workflow") or {}
    fixes = get("/automated-security-fixes") or {}
    return {
        "visibility": str(repo.get("visibility") or ("private" if repo.get("private") else "public")),
        "collaborators": [
            {"login": str(item.get("login") or ""), "role": str(item.get("role_name") or "")}
            for item in get("/collaborators?per_page=100") or ()
            if isinstance(item, Mapping)
        ],
        "deploy_keys": [
            {"title": str(item.get("title") or ""), "read_only": bool(item.get("read_only")), "last_used": str(item.get("last_used") or ""), "created_at": str(item.get("created_at") or "")}
            for item in get("/keys?per_page=100") or ()
            if isinstance(item, Mapping)
        ],
        "allowed_actions": str(actions.get("allowed_actions") or ""),
        "pinning_required": bool(actions.get("sha_pinning_required")),
        "token": str(workflow.get("default_workflow_permissions") or ""),
        "token_approves_reviews": bool(workflow.get("can_approve_pull_request_reviews")),
        "security_fixes": bool(fixes.get("enabled")),
        # Absent on a plan that offers none of these, which is not "off".
        "security": {key: (value or {}).get("status") for key, value in security.items()} if security else None,
    }


def _rules(rules: list[Any]) -> dict[str, Any]:
    """What the default branch requires, from every ruleset that applies to it."""

    found: dict[str, Any] = {"pull_request": False, "reviews": 0, "required_checks": [], "blocks_force_push": False, "blocks_deletion": False}
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        kind, parameters = rule.get("type"), rule.get("parameters") or {}
        if kind == "pull_request":
            found["pull_request"] = True
            found["reviews"] = max(found["reviews"], int(parameters.get("required_approving_review_count") or 0))
        elif kind == "required_status_checks":
            found["required_checks"] += [str(item.get("context")) for item in parameters.get("required_status_checks") or ()]
        elif kind == "non_fast_forward":
            found["blocks_force_push"] = True
        elif kind == "deletion":
            found["blocks_deletion"] = True
    found["required_checks"] = sorted(set(found["required_checks"]))
    return found


def _environments(environments: list[Any]) -> list[dict[str, Any]]:
    found = []
    for item in environments:
        if not isinstance(item, Mapping):
            continue
        reviewers, wait = [], 0
        for rule in item.get("protection_rules") or ():
            if rule.get("type") == "required_reviewers":
                reviewers += [str((entry.get("reviewer") or {}).get("login") or (entry.get("reviewer") or {}).get("name") or "") for entry in rule.get("reviewers") or ()]
            elif rule.get("type") == "wait_timer":
                wait = int(rule.get("wait_timer") or 0)
        policy = item.get("deployment_branch_policy") or {}
        found.append(
            {
                "name": str(item.get("name") or ""),
                "reviewers": sorted(set(reviewers) - {""}),
                "wait_minutes": wait,
                "branches": "protected" if policy.get("protected_branches") else "custom" if policy.get("custom_branch_policies") else "any",
            }
        )
    return found


def _runners(runners: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or ""),
            "status": str(item.get("status") or ""),
            "busy": bool(item.get("busy")),
            "labels": sorted(str(label.get("name")) for label in item.get("labels") or () if isinstance(label, Mapping)),
        }
        for item in runners
        if isinstance(item, Mapping)
    ]


def _images(runtime: ProviderRuntime, name: str, ref: str) -> list[dict[str, Any]]:
    """The container images this controller's own composition names for ``name``:
    the host's image and the composition it runs. Read from the registry, which
    takes the App's token where GitHub's package API does not."""

    composed = runtime.composition() or {}
    if str(composed.get("repository") or "") != name:
        return []
    names = {name.lower()}
    running = str(composed.get("image") or "")
    if running.startswith(f"{REGISTRY_HOST}/"):
        names.add(running.removeprefix(f"{REGISTRY_HOST}/").split("@")[0].split(":")[0].lower())
    minted = github_app.token(runtime, [name], READ)
    # Each image on its own: a package the App may not read is that image
    # refused, never the loss of the others.
    found = []
    for image in sorted(names):
        read = _part(IMAGES, f"{name}:{image}", ref, lambda image=image: _image(runtime, image, minted))
        if read is not None:
            found.append(read)
    return found


def _image(runtime: ProviderRuntime, image: str, minted: str) -> dict[str, Any]:
    """An image's tags, and which digests carry a cosign signature: cosign names
    its signature ``sha256-<digest>.sig``, so the tag list alone says which."""

    basic = base64.b64encode(f"x-access-token:{minted}".encode()).decode()
    registry = runtime.request(
        f"https://{REGISTRY_HOST}/token?scope=repository:{image}:pull&service={REGISTRY_HOST}",
        headers={"Authorization": f"Basic {basic}"},
    ) or {}
    listed = runtime.request(
        f"https://{REGISTRY_HOST}/v2/{image}/tags/list?n=1000",
        headers={"Authorization": f"Bearer {registry.get('token', '')}"},
    ) or {}
    tags = [str(tag) for tag in listed.get("tags") or ()]
    signed = sorted(
        tag.removeprefix("sha256-").removesuffix(".sig") for tag in tags if tag.startswith("sha256-") and tag.endswith(".sig")
    )
    return {"name": image, "tags": sum(1 for tag in tags if not tag.startswith("sha256-")), "signed": signed}


def _head(commit: Mapping[str, Any]) -> dict[str, Any]:
    detail = commit.get("commit") or {}
    return {
        "sha": str(commit.get("sha") or ""),
        "message": str(detail.get("message") or "").splitlines()[0] if detail.get("message") else "",
        "author": str((commit.get("author") or {}).get("login") or (detail.get("author") or {}).get("name") or ""),
        "date": str((detail.get("committer") or detail.get("author") or {}).get("date") or ""),
        "url": str(commit.get("html_url") or ""),
    }


def _checks(answer: Any) -> dict[str, Any]:
    runs = (answer or {}).get("check_runs") or []
    failing = sorted({str(run.get("name")) for run in runs if run.get("conclusion") in _FAILED})
    running = sum(1 for run in runs if run.get("status") in _RUNNING)
    state = "failure" if failing else "pending" if running else "success" if runs else ""
    names = sorted({str(run.get("name")) for run in runs if run.get("name")})
    return {"state": state, "total": len(runs), "failing": failing, "running": running, "names": names}


def _pulls(pulls: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "number": pull.get("number"),
            "title": str(pull.get("title") or ""),
            "url": str(pull.get("html_url") or ""),
            "author": str((pull.get("user") or {}).get("login") or ""),
            "draft": bool(pull.get("draft")),
            "updated_at": str(pull.get("updated_at") or ""),
            "head": str((pull.get("head") or {}).get("sha") or ""),
        }
        for pull in pulls
        if isinstance(pull, Mapping)
    ]


def _run(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "name": str(run.get("name") or ""),
        "status": str(run.get("status") or ""),
        "conclusion": str(run.get("conclusion") or ""),
        "event": str(run.get("event") or ""),
        "branch": str(run.get("head_branch") or ""),
        "sha": str(run.get("head_sha") or ""),
        "url": str(run.get("html_url") or ""),
        "created_at": str(run.get("created_at") or ""),
    }


def _latest_runs(runs: list[Any], workflows: list[Any] | None = None) -> list[dict[str, Any]]:
    """The newest run of each workflow that exists now, under its name now.

    A run keeps the name its workflow had when it ran, so grouping by that
    name makes every rename a workflow of its own, and a deleted workflow
    lives on in the history. Grouped by the workflow's ID instead, and named
    and filtered by the repository's active workflows when GitHub lists them.
    """

    current = {
        item.get("id"): str(item.get("name") or "")
        for item in workflows or ()
        if isinstance(item, Mapping) and item.get("state") == "active"
    }
    seen: dict[Any, dict[str, Any]] = {}
    for run in runs:
        # GitHub's own dynamic runs (Dependabot's graph updates) are named per
        # run, so each would read as a workflow of its own. They are not the
        # repository's workflows.
        if not isinstance(run, Mapping) or run.get("event") == "dynamic":
            continue
        key = run.get("workflow_id") or str(run.get("name") or "")
        if key in seen or (current and key not in current):
            continue
        seen[key] = {**_run(run), **({"name": current[key]} if key in current else {})}
    return list(seen.values())


def _waiting(runs: list[Any], get) -> list[dict[str, Any]]:
    """Runs held for a person's approval, and the environment each waits on."""

    found = []
    for run in runs:
        if not isinstance(run, Mapping) or run.get("status") != "waiting":
            continue
        pending = get(f"/actions/runs/{run.get('id')}/pending_deployments") or []
        environments = sorted(
            {str((item.get("environment") or {}).get("name") or "") for item in pending if isinstance(item, Mapping)} - {""}
        )
        found.append({**_run(run), "environments": environments})
    return found


def _release(releases: list[Any]) -> dict[str, Any] | None:
    first = releases[0] if releases and isinstance(releases[0], Mapping) else None
    if first is None:
        return None
    return {
        "tag": str(first.get("tag_name") or ""),
        "url": str(first.get("html_url") or ""),
        "published_at": str(first.get("published_at") or ""),
    }


def _deployments(deployments: list[Any], get) -> list[dict[str, Any]]:
    """The newest deployment to each environment, with its latest status."""

    newest: dict[str, Mapping[str, Any]] = {}
    for item in deployments:
        if isinstance(item, Mapping) and str(item.get("environment") or "") not in newest:
            newest[str(item.get("environment") or "")] = item
    found = []
    for environment, item in newest.items():
        statuses = get(f"/deployments/{item.get('id')}/statuses?per_page=1") or []
        status = statuses[0] if statuses and isinstance(statuses[0], Mapping) else {}
        url = str(status.get("log_url") or status.get("target_url") or "")
        found.append(
            {
                "environment": environment,
                "sha": str(item.get("sha") or ""),
                "created_at": str(item.get("created_at") or ""),
                "state": str(status.get("state") or ""),
                "url": url,
                "verified": _verifications(url, get),
            }
        )
    return found


def _verifications(url: str, get) -> list[dict[str, str]]:
    """What the job that made a deployment verified before it deployed: its
    steps named for verifying, with their results. The deployment's own log
    link names the job, so nothing here guesses which run deployed."""

    _, marker, job = url.partition("/job/")
    job = job.split("/")[0].split("?")[0]
    if not marker or not job.isdigit():
        return []
    steps = (get(f"/actions/jobs/{job}") or {}).get("steps") or []
    return [
        {"name": str(step.get("name") or ""), "conclusion": str(step.get("conclusion") or "")}
        for step in steps
        if isinstance(step, Mapping) and str(step.get("name") or "").lower().startswith("verify")
    ]


def _severities(items: list[Any], severity) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if isinstance(item, Mapping):
            level = str(severity(item) or "unknown").lower()
            counts[level] = counts.get(level, 0) + 1
    return counts


def _alerts(get, name: str, ref: str) -> dict[str, Any]:
    """Open alerts by severity. A kind the repository cannot answer (code
    scanning off, a free private repository) is that part refused, never zero."""

    readers = (
        (CODE_SCANNING, "/code-scanning/alerts?state=open&per_page=100",
         lambda item: (item.get("rule") or {}).get("security_severity_level") or (item.get("rule") or {}).get("severity")),
        (DEPENDABOT, "/dependabot/alerts?state=open&per_page=100",
         lambda item: (item.get("security_advisory") or {}).get("severity")),
        (LEAKED_CREDENTIALS, "/secret-scanning/alerts?state=open&per_page=100", lambda item: "leaked"),
    )
    found: dict[str, Any] = {}
    for part, path, severity in readers:
        try:
            found[part.name] = _severities(get(path) or [], severity)
        except ProviderError as exc:
            refuse_part(part.name, _as_repository_refusal(exc), scope=name, connection_ref=ref)
    return found


def _artifacts(artifacts: list[Any]) -> list[dict[str, Any]]:
    """Artifacts that have not expired, soonest to expire first."""

    live = [
        {
            "name": str(item.get("name") or ""),
            "expires_at": str(item.get("expires_at") or ""),
            "sha": str((item.get("workflow_run") or {}).get("head_sha") or ""),
        }
        for item in artifacts
        if isinstance(item, Mapping) and not item.get("expired") and item.get("expires_at")
    ]
    return sorted(live, key=lambda item: item["expires_at"])[:20]


READINGS = {REPOSITORY_KIND: read_repositories}
