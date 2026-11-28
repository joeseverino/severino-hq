"""The examples of a run's evidence, the part {% run_evidence %} draws."""

lazy from datetime import UTC, datetime

from hq.platform.core.template_parts import Example
lazy from hq.domains.jobs.models import JobState
lazy from hq.platform.application import tables
lazy from hq.platform.application.tables import Cell, DataTable, TableColumn
lazy from hq.platform.application.work_runs import RunEvidence


def _event(
    task: str, outcome: str, *, host: str = "example-01", message: str = "", warnings: tuple = (), data: str = ""
) -> dict:
    """A task's outcome as a run reports it (the bridge's ``RunEvent``)."""

    return {
        "host": host,
        "task": task,
        "outcome": outcome,
        "message": message,
        "warnings": list(warnings),
        "data": data,
    }


_LONG_HOST = "example-machine-with-a-long-name-01.example.com"


def _example_runs():
    return (
        (
            "Run example.clear_cache",
            "Running",
            JobState.RUNNING.tone,
            "Applying the example playbook.",
            RunEvidence(
                hosts=("example-01", "example-02"),
                correlation="example-investigation-7",
                worker="example-automation-worker",
                lease_until=datetime(2026, 3, 2, 9, 35, tzinfo=UTC),
                events=(
                    _event("Flush the example cache", "changed"),
                    _event(
                        "Say the example cache's settings",
                        "ok",
                        data='{"listen": 8080, "names": ["cache.example.com", "www.example.com"]}',
                    ),
                    _event("Check the example cache", "ok", host="example-02"),
                    _event(
                        "Find the example cache's files",
                        "ok",
                        host="example-02",
                        message="Not all paths examined, check warnings for details",
                        warnings=(
                            (
                                "Skipped '/srv/example/cache' path due to this access issue: '/srv/example/cache' "
                                "is not a directory"
                            ),
                            (
                                "Skipped '/srv/example/spool' path due to this access issue: '/srv/example/spool' "
                                "is not a directory"
                            ),
                        ),
                    ),
                    _event("Restart every example service the cache feeds", "started"),
                ),
            ),
        ),
        ("Scan playbooks", "Queued", JobState.QUEUED.tone, "Waiting for HQ to start it.", RunEvidence()),
        (
            "Run example.upgrade_packages",
            "Failed",
            JobState.FAILED.tone,
            "The example upgrade failed on one machine.",
            RunEvidence(
                hosts=(_LONG_HOST,),
                correlation="example-investigation-with-a-correlation-id-longer-than-a-phone-is-wide-0123456789",
                events=(
                    _event(
                        "Upgrade every example package to the release the example repository pins",
                        "failed",
                        host=_LONG_HOST,
                        message="example-package-with-a-long-name 2.0.1 conflicts with "
                        "example-other-package-with-a-long-name 1.4.0, which the example machine also holds.",
                    ),
                ),
            ),
        ),
        (
            "Run example.report_disks",
            "Done",
            JobState.SUCCEEDED.tone,
            "Reported every example disk.",
            RunEvidence(
                hosts=("example-01",),
                stated=(
                    {
                        "host": "example-01",
                        "kind": "host.filesystem",
                        "records": [
                            {"mount": "/", "free_bytes": 365226196992, "total_bytes": 494345830400},
                            {"mount": "/srv/example-data-with-a-longer-name", "free_bytes": 0, "total_bytes": 1024},
                        ],
                    },
                    {
                        "host": "example-01",
                        "kind": "apps.pull",
                        "records": [
                            {"project": "example-app", "result": "ok", "health": "healthy", "changed": True},
                            {"project": "example-app-left-alone", "result": "ok", "changed": False},
                            {
                                "project": "example-app-with-a-longer-name",
                                "result": "rolled_back",
                                "health": "unhealthy: restarted 3 times in its settle window",
                                "changed": True,
                            },
                        ],
                    },
                    {
                        "host": "example-01",
                        "kind": "apps.receipt",
                        "records": [{"revision": "c" * 40, "last_attempt": "c" * 40, "ok": False}],
                    },
                    {"host": "example-01", "kind": "example.reading_with_a_longer_name", "records": [{}, {}]},
                ),
                events=tuple(
                    _event(f"Example task {number}", outcome)
                    for number, outcome in enumerate(("ok", "changed", "skipped", "unreachable") * 3, start=1)
                ),
            ),
        ),
    )


# A run's evidence ({% run_evidence %}) in its row's open line, as Jobs,
# a machine's page and a procedure's page draw it: a run with every field,
# one with none, one whose names and messages run long, and one with many
# tasks that stated readings: a swept kind's, the applier's, and one HQ
# declares nothing of.
# A run's own page: its evidence as its facts.
RUN_EVIDENCE = Example(
    '{% facts %}{% run_evidence evidence layout="facts" %}{% endfacts %}',
    context=lambda: {"evidence": _example_runs()[0][4]},
)
# In each run's row of a declared table, its open line.
RUN_EVIDENCE_IN_ROWS = Example(
    '{% section "Example runs" %}{% data_table table %}{% endsection %}',
    context=lambda: {
        "table": DataTable(
            "Example runs",
            (TableColumn("Playbook"), TableColumn("State", kind="status"), TableColumn("Result")),
            tuple(
                tables.Row((Cell(title), Cell(word, tone=tone), Cell(note)), evidence=evidence)
                for title, word, tone, note, evidence in _example_runs()
            ),
        )
    },
)
