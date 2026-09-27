"""Continuous delivery: every extension's latest admission runs in production.

The running image's lock says which commit of each extension production runs;
GitHub says which commit each extension last admitted on its main branch, and
which composition run started after that. Anything between the two is drift,
worded by stage, so each new stage is a new reconcile and the same stage is
never acted on twice.

GitHub holds the record of what happened. A check run on each admitted commit
is the delivery's status where the change was made, and the compose run that
carries it is found by time rather than remembered.

HQ starts nothing here. An extension's admission dispatches the composition
itself, the moment it has signed a wheel (the host's ``admit-plugin`` action),
and the deploy waits for a person. HQ reports: each stage on the extension's
commit, and one comment on its merged pull request once production runs it,
read when HQ boots on a new image and whenever it sweeps while in use. The
controller never asks its app for a token that can start a workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from . import github_app, github_readings
from .contracts import ControllerIntegrationAdapter, ProviderResult, ProviderRuntime

KIND = "github.delivery"
# What reporting delivery writes: a check run on each extension commit, and one
# comment on its merged pull request. With ``github_readings.READ`` and the
# admission's Actions write, this is every permission HQ's app is registered
# with (``deploy/github-apps.json``).
REPORTS = {"checks": "write", "pull_requests": "write"}
CHECK_NAME = "Severino HQ · production"
CURRENT = "Every extension's latest admission, confirmed on GitHub"
COMPOSE_WORKFLOW = ".github/workflows/compose.yml"
_MARKER = "<!-- severino-hq-delivery:{sha} -->"
_RUNNING = frozenset({"queued", "in_progress", "requested", "pending"})


@dataclass(frozen=True)
class Extension:
    plugin: str
    repository: str
    workflow: str
    running: str
    admitted: str = ""
    admitted_at: str = ""
    # The composition run that started after the admission, when one has.
    run: Mapping[str, Any] | None = None
    # HQ's check run on the running commit, when one is still open.
    unreported: Mapping[str, Any] | None = None

    @property
    def behind(self) -> bool:
        return bool(self.admitted) and self.admitted != self.running

    @property
    def stage(self) -> str:
        run = self.run
        if run is None:
            return "no composition has started"
        number = run.get("id")
        status = str(run.get("status", ""))
        if status == "waiting":
            return f"composition run {number} is waiting for deploy approval"
        if status in _RUNNING:
            return f"composition run {number} is running"
        conclusion = str(run.get("conclusion") or "ended")
        if conclusion == "success":
            return f"composition run {number} finished without deploying it"
        return f"composition run {number} {'failed' if conclusion == 'failure' else conclusion}"

    def says(self) -> str:
        if self.behind:
            return (
                f"{self.plugin} {self.admitted[:7]} is admitted and production runs "
                f"{self.running[:7]}: {self.stage}"
            )
        return f"{self.plugin} {self.running[:7]} is live, not yet confirmed on GitHub"


def _basename(workflow: str) -> str:
    return str(workflow).rsplit("/", 1)[-1]


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_admission(runtime: ProviderRuntime, extension: Extension, every: tuple[str, ...]):
    owner, repo = github_app.repository(extension.repository)
    answer = github_app.call(
        runtime,
        f"/repos/{owner}/{repo}/actions/workflows/{_basename(extension.workflow)}/runs"
        "?branch=main&status=success&per_page=1",
        repositories=every,
        permissions={"actions": "read"},
    )
    runs = (answer or {}).get("workflow_runs") or []
    return runs[0] if runs else None


def _compose_runs(runtime: ProviderRuntime, spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    owner, repo = github_app.repository(spec["repository"])
    answer = github_app.call(
        runtime,
        f"/repos/{owner}/{repo}/actions/workflows/{_basename(spec['workflow'])}/runs"
        f"?branch={spec['branch']}&per_page=20",
        repositories=(spec["repository"],),
        permissions={"actions": "read"},
    )
    return list((answer or {}).get("workflow_runs") or [])


def _run_after(runs: list[Mapping[str, Any]], since: str) -> Mapping[str, Any] | None:
    """The newest composition run created at or after ``since``."""

    start = _when(since)
    if start is None:
        return None
    later = [run for run in runs if (_when(run.get("created_at")) or start) >= start]
    return max(later, key=lambda run: str(run.get("created_at", "")), default=None)


def _check_run(runtime: ProviderRuntime, repository: str, sha: str) -> Mapping[str, Any] | None:
    owner, repo = github_app.repository(repository)
    answer = github_app.call(
        runtime,
        f"/repos/{owner}/{repo}/commits/{sha}/check-runs"
        f"?check_name={github_app.quote(CHECK_NAME)}&app_id={github_app.app_id(runtime)}&filter=latest",
        repositories=(repository,),
        permissions={"checks": "read"},
    )
    runs = (answer or {}).get("check_runs") or []
    return runs[0] if runs else None


def delivery(runtime: ProviderRuntime, spec: Mapping[str, Any]) -> tuple[Extension, ...]:
    """Every extension, with how far its latest admission is from production."""

    found = tuple(
        Extension(
            plugin=item["plugin"],
            repository=item["source_repository"],
            workflow=item["source_workflow"],
            running=item["source_commit"],
        )
        for item in runtime.composition().get("extensions") or ()
    )
    if not found:
        return ()
    every = tuple(sorted({item.repository for item in found}))
    runs: list[Mapping[str, Any]] | None = None
    settled: list[Extension] = []
    for extension in found:
        admission = _latest_admission(runtime, extension, every)
        admitted = str((admission or {}).get("head_sha", ""))
        admitted_at = str((admission or {}).get("updated_at", ""))
        if admitted and admitted != extension.running:
            if runs is None:
                runs = _compose_runs(runtime, spec)
            settled.append(
                replace(
                    extension,
                    admitted=admitted,
                    admitted_at=admitted_at,
                    run=_run_after(runs, admitted_at),
                )
            )
            continue
        check = _check_run(runtime, extension.repository, extension.running)
        unreported = check if check and check.get("status") != "completed" else None
        settled.append(replace(extension, admitted=admitted, unreported=unreported))
    return tuple(settled)


def production(extensions: tuple[Extension, ...]) -> str:
    """``CURRENT``, or what stands between production and it, stage by stage."""

    pending = [item for item in extensions if item.behind or item.unreported]
    return "; ".join(item.says() for item in pending) if pending else CURRENT


def _record(spec: Mapping[str, Any], extensions: tuple[Extension, ...]) -> dict[str, Any]:
    return {
        "repository": spec["repository"],
        "workflow": spec["workflow"],
        "branch": spec["branch"],
        "production": production(extensions),
        "extensions": [
            {
                "plugin": item.plugin,
                "running": item.running,
                "admitted": item.admitted,
                "stage": item.stage if item.behind else "live",
                "run_url": str((item.run or {}).get("html_url", "")),
            }
            for item in extensions
        ],
    }


def _host_spec(runtime: ProviderRuntime) -> dict[str, Any] | None:
    repository = str(runtime.composition().get("repository") or "")
    if not repository:
        return None
    return {
        "repository": repository,
        "workflow": COMPOSE_WORKFLOW,
        "branch": "main",
        "production": CURRENT,
    }


def inventory(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    spec = _host_spec(runtime)
    if spec is None:
        return []
    return [_record(spec, delivery(runtime, spec))]


# ----- Reporting on GitHub ---------------------------------------------------


def _checks_page(spec: Mapping[str, Any]) -> str:
    return f"https://github.com/{spec['repository']}/actions/workflows/{_basename(spec['workflow'])}"


def _report(extension: Extension, spec: Mapping[str, Any], image: str) -> dict[str, Any]:
    """The check run for one extension's admitted or live commit."""

    run = extension.run
    if not extension.behind:
        return {
            "status": "completed",
            "conclusion": "success",
            "output": {
                "title": "Live in production",
                "summary": f"Production runs `{extension.running[:12]}` in `{image or 'the composed image'}`.",
            },
        }
    details = str((run or {}).get("html_url") or _checks_page(spec))
    body = {"details_url": details, "external_id": str((run or {}).get("id", ""))}
    status = str((run or {}).get("status", ""))
    if run is None:
        return {
            **body,
            "status": "queued",
            "output": {
                "title": "Waiting for its composition",
                "summary": "Its admission starts the composition. If that admission failed, re-run it.",
            },
        }
    if status == "waiting":
        return {**body, "status": "in_progress", "output": {"title": "Waiting for deploy approval", "summary": extension.stage}}
    if status == "queued":
        return {**body, "status": "queued", "output": {"title": "Composition queued", "summary": extension.stage}}
    if status in _RUNNING:
        return {**body, "status": "in_progress", "output": {"title": "Composing", "summary": extension.stage}}
    return {
        **body,
        "status": "completed",
        "conclusion": "failure",
        "output": {"title": "Not delivered", "summary": f"{extension.stage}. Re-run it from the run page."},
    }


def _sha(extension: Extension) -> str:
    return extension.admitted if extension.behind else extension.running


def _upsert_check(runtime: ProviderRuntime, extension: Extension, report: dict[str, Any]) -> None:
    owner, repo = github_app.repository(extension.repository)
    grant = {"repositories": (extension.repository,), "permissions": {"checks": REPORTS["checks"]}}
    existing = _check_run(runtime, extension.repository, _sha(extension))
    if existing and existing.get("id"):
        github_app.call(
            runtime,
            f"/repos/{owner}/{repo}/check-runs/{existing['id']}",
            method="PATCH",
            payload=report,
            **grant,
        )
        return
    github_app.call(
        runtime,
        f"/repos/{owner}/{repo}/check-runs",
        method="POST",
        payload={"name": CHECK_NAME, "head_sha": _sha(extension), **report},
        **grant,
    )


def _announce(runtime: ProviderRuntime, extension: Extension, image: str) -> None:
    """One comment on the merged pull request, the first time its commit is live."""

    owner, repo = github_app.repository(extension.repository)
    read = {"repositories": (extension.repository,), "permissions": {"pull_requests": "read"}}
    pulls = github_app.call(
        runtime, f"/repos/{owner}/{repo}/commits/{extension.running}/pulls", **read
    )
    merged = next(
        (pull for pull in pulls or () if isinstance(pull, Mapping) and pull.get("merged_at")),
        None,
    )
    if merged is None:
        return
    marker = _MARKER.format(sha=extension.running)
    comments = github_app.call(
        runtime, f"/repos/{owner}/{repo}/issues/{merged['number']}/comments?per_page=100", **read
    )
    if any(marker in str((comment or {}).get("body", "")) for comment in comments or ()):
        return
    github_app.call(
        runtime,
        f"/repos/{owner}/{repo}/issues/{merged['number']}/comments",
        method="POST",
        payload={
            "body": f"{marker}\nLive in production: `{extension.running[:12]}` in "
            f"`{image or 'the composed image'}`."
        },
        repositories=(extension.repository,),
        permissions={"pull_requests": REPORTS["pull_requests"]},
    )


def reconcile(
    runtime: ProviderRuntime,
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    del observed
    extensions = delivery(runtime, spec)
    image = str(runtime.composition().get("image") or "")
    reporting = [item for item in extensions if item.behind or item.unreported]
    if apply:
        for item in reporting:
            _upsert_check(runtime, item, _report(item, spec, image))
            if not item.behind:
                _announce(runtime, item, image)
    status = _record(spec, extensions)
    failed = [
        item
        for item in extensions
        if item.behind and item.run is not None and str(item.run.get("status")) == "completed"
    ]
    if failed:
        conditions = [
            runtime.condition(
                "Degraded",
                True,
                "NotDelivered",
                "; ".join(item.says() for item in failed)
                + ". Re-run the composition from its run page.",
            )
        ]
    else:
        conditions = [
            runtime.condition(
                "Ready",
                True,
                "Delivering" if reporting else "Current",
                status["production"] + ".",
            )
        ]
    return ProviderResult(
        changed=bool(reporting),
        status=status,
        conditions=conditions,
        message="Delivery reported.",
    )


def probe(runtime: ProviderRuntime, connection_ref: str) -> dict[str, Any]:
    return github_app.probe(runtime, connection_ref)


def _from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": record["repository"],
        "workflow": record["workflow"],
        "branch": record["branch"],
        "production": record["production"],
    }


def _readout(spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    rows = [("Production", spec.get("production", ""), status.get("production", ""))]
    rows.extend(
        (item.get("plugin", ""), item.get("admitted", "")[:7], item.get("running", "")[:7])
        for item in status.get("extensions") or ()
        if isinstance(item, Mapping)
    )
    return tuple(rows)


def build_adapter(*, provider_model, provider_spec, applies):
    class GitHubDeliverySpec(provider_model):
        repository: str = Field(
            pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
            title="Host repository",
            description="The repository whose composition workflow deploys HQ.",
        )
        workflow: str = Field(
            default=COMPOSE_WORKFLOW,
            pattern=r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$",
            title="Composition workflow",
        )
        branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_./-]+$", title="Branch")
        production: Literal[CURRENT] = Field(  # type: ignore[valid-type]
            default=CURRENT, title="Production runs"
        )

    definition = provider_spec(
        KIND,
        "Starts the composition when an extension's latest admission is not in "
        "production, and reports each stage on the extension's commit.",
        GitHubDeliverySpec,
        actions={"reconcile": applies(automatic=True)},
        label="Continuous delivery",
        connection_providers=(github_app.PROVIDER,),
        from_record=_from_record,
        identity=lambda spec: (spec["repository"],),
        key_hint=lambda record: "delivery",
        adopts=lambda record: record.get("production") == CURRENT,
        sample_record={
            "repository": "example/host",
            "workflow": COMPOSE_WORKFLOW,
            "branch": "main",
            "production": CURRENT,
            "extensions": [],
        },
        readout=_readout,
        advanced_fields=("workflow", "branch", "production"),
        declaration_only=True,
        removal_note=lambda spec: (
            "HQ stops starting compositions. The hourly schedule in "
            f"{spec.get('repository', 'the host repository')} still delivers."
        ),
    )
    return ControllerIntegrationAdapter(
        definitions=(definition,),
        inventory={KIND: inventory},
        readings=github_readings.READINGS,
        connection_probes={github_app.PROVIDER: probe},
        actions={(KIND, "reconcile"): reconcile},
    )
