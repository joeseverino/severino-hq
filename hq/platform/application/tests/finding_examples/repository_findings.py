from ...github_posture import EXAMPLES as POSTURE
from ...tone import Tone
from . import NOW, Ago, Example, Examples, Raised, ago

KIND = "github.repository"
ALPHA = {
    "connection_ref": "github",
    "repository": "example/alpha",
    "url": "https://github.com/example/alpha",
    "default_branch": "main",
    "head": {"sha": "abc"},
}


def alpha(raises: tuple[Raised | str, ...] = (), **record) -> Example:
    """example/alpha, as its last read stated it, with ``record`` said of it."""

    return Example(readings={KIND: ({**ALPHA, **record},)}, raises=raises)


def waiting(since: Ago) -> list[dict]:
    return [{"id": 9, "name": "Compose", "environments": ["production"], "created_at": since, "url": "u"}]


def admission(*lapses: float) -> list[dict]:
    """The admission every run uploads under one name, lapsing in each of ``lapses`` days."""

    return [{"name": "admission-example.alpha", "expires_at": ago(days=-days, hours=-1)} for days in lapses]


def access(**changes) -> dict:
    """The access of the standard's kept repository, with ``changes``."""

    return {**POSTURE.kept["access"], **changes}


QUIET = alpha(checks={"state": "success"}, alerts={"code_scanning": {"low": 2}})
SERIOUS = Tone.SERIOUS
WAITS = "Compose waits for your approval to deploy to production"

EXAMPLES = (
    Examples(
        "github-waiting",
        # Held for hours, it is serious.
        fires=(
            alpha(waiting=waiting(ago(hours=3)), raises=(Raised(title=WAITS, severity=SERIOUS),)),
            alpha(waiting=waiting(ago(minutes=5)), raises=(WAITS,)),
        ),
        quiet=(QUIET,),
    ),
    Examples(
        "github-failing",
        fires=(
            alpha(checks={"state": "failure", "failing": ["lint"]}, raises=("alpha is failing on main",)),
            # A job of a workflow run says the step it failed at.
            alpha(
                checks={"state": "failure", "failing": ["lint", "test"], "steps": {"test": "Run tests"}},
                raises=(
                    Raised(title="alpha is failing on main", explanation="Failing jobs: lint, test (at Run tests)"),
                ),
            ),
        ),
        quiet=(QUIET,),
    ),
    Examples(
        "github-unverified",
        fires=(
            alpha(
                deployments=[
                    {
                        "environment": "production",
                        "url": "u",
                        "verified": [{"name": "Verify the image was signed", "conclusion": "failure"}],
                    }
                ],
                raises=("alpha's last deploy failed verification",),
            ),
        ),
        quiet=(QUIET,),
    ),
    Examples(
        "github-alerts",
        fires=(
            alpha(
                alerts={"code_scanning": {"critical": 1, "low": 4}},
                raises=(Raised(title="1 serious alert in alpha", severity=SERIOUS),),
            ),
            alpha(
                alerts={"leaked_credentials": {"leaked": 1}},
                raises=(Raised(title="1 leaked credential in alpha", severity=SERIOUS),),
            ),
        ),
        quiet=(QUIET, alpha(alerts={"leaked_credentials": {"leaked": 0}, "dependabot": {}})),
    ),
    Examples(
        "github-artifact",
        # An admission uploaded by two runs lapses with the newer.
        fires=(alpha(artifacts=admission(2, 6), raises=("alpha's admission lapses in 6 days",)),),
        quiet=(QUIET,),
    ),
    Examples(
        "github-posture",
        # An archived repository is held to nothing.
        fires=(
            Example(
                readings={
                    KIND: tuple(
                        {
                            **ALPHA,
                            "repository": f"example/{name}",
                            "url": f"https://github.com/example/{name}",
                            "private": True,
                            "access": access(vulnerability_alerts=False),
                            "variables": [],
                            "archived": name == "retired",
                        }
                        for name in ("kept", "retired")
                    )
                },
                raises=(Raised(title="Vulnerability alerts are off", evidence=(("Not met in", "kept"),)),),
            ),
        ),
        quiet=(alpha(private=True, access=access(), variables=[], read_at=NOW),),
    ),
)
