"""The shapes the containers, upgrades and app updates resources answer in."""

from typing import Annotated, Any, Literal, TypedDict

from .answer_fields import AsRead, PageUrl
from .describe import Describe, listed
from .json_schemas import Written
from .record_parts import ComposeProject, MeasurementPart
from .ui import ended
from .upgrades import HIGH, LOW, MAJOR, MEDIUM, MINOR, PATCH, UNKNOWN_CHANGE, Blocker

# What stands in the way of an upgrade, as ``Blocker`` says it.
WrittenBlocker = Annotated[dict[str, Any], Written(Blocker)]


class ContainerRow(TypedDict):
    address: Annotated[str, Describe("`machine:container`, which a read of one names it by.")]
    machine: Annotated[str, Describe("The name of the machine it runs on.")]
    name: Annotated[str, Describe("The container's name.")]
    compose_project: ComposeProject
    url: Annotated[str, Describe("Its page in HQ: its record's when HQ watches it, else its machine's.")]
    state: Annotated[str, Describe("Docker's state for it, such as `running` or `exited`.")]
    status: Annotated[str, Describe("Docker's status line for it, such as `Up 12 days (healthy)`.")]
    serves: Annotated[list[str], Describe("The hostnames it serves.")]


class ProjectPart(TypedDict):
    name: Annotated[str, Describe("The project's name.")]
    slug: Annotated[str, Describe("The project's slug, which its page is addressed by.")]
    url: PageUrl


# A pull request's title, as GitHub lists it.
type PullTitle = Annotated[str, Describe("The pull request's title.")]


class PullRequestPart(TypedDict):
    number: Annotated[int | None, Describe("The pull request's number; null when not read.")]
    title: PullTitle
    url: Annotated[str, Describe("The pull request's page.")]
    draft: Annotated[bool, Describe("Whether it is a draft.")]


class RepositoryPart(TypedDict):
    name: Annotated[str, Describe("The repository's name, `owner/name`.")]
    url: Annotated[str, Describe("The repository's page.")]
    default_branch: Annotated[str, Describe("The repository's default branch.")]
    head: Annotated[str, Describe("The commit the default branch is at; empty when not read.")]
    deployed: Annotated[str, Describe("The commit production runs, by its last deployment; empty when not read.")]
    undeployed: Annotated[bool, Describe("Whether the default branch is ahead of what production runs.")]
    verified: Annotated[bool, Describe("Whether the checks after its last deployment confirmed production.")]
    open_pull_requests: Annotated[list[PullRequestPart], Describe("Its open pull requests.")]


class UsagePart(TypedDict):
    memory_bytes: Annotated[int, Describe("The memory it uses, in bytes.")]
    memory_counted_as: Annotated[str, Describe("How `memory_bytes` was counted, as Docker reports it.")]
    memory_limit_bytes: Annotated[int | None, Describe("Its memory limit, in bytes; null for none.")]
    memory_of_limit_percent: Annotated[
        float | None, Describe("How much of its memory limit it uses, a percentage; null without a limit.")
    ]
    cpu_percent_of_one_core: Annotated[
        float | None,
        Describe(
            "The CPU it used since the read before, as a percentage of one core. Null until two reads "
            "have sampled one run of it."
        ),
    ]
    cpu_limit: Annotated[float | None, Describe("How many cores it may use; null for no limit.")]
    read_at: Annotated[str | None, Describe("When its use was read, an ISO 8601 date-time; null when not said.")]


class ContainerRecord(ContainerRow):
    """One running container whole: its image and standing, how it runs and what it uses."""

    image: Annotated[
        dict[str, Any],
        Describe(
            "What it runs: reference, registry, repository, tag, digest, image id, whether pinned by digest, "
            "build time and source."
        ),
    ]
    standing: Annotated[
        dict[str, Any],
        Describe(
            "Whether what it runs is current and safe: its state and summary, newer tags, the latest release "
            "and advisories. It also says when the registry and upstream were last read."
        ),
    ]
    project: Annotated[ProjectPart | None, Describe("The project it belongs to; null for none.")]
    repository: Annotated[
        RepositoryPart | None,
        Describe("The repository whose pipeline deploys it; null when none deploys it."),
    ]
    runtime: Annotated[
        AsRead | None, Describe("How it is run, as Docker's inspect reports it; null when that was not read.")
    ]
    usage: Annotated[
        UsagePart | None, Describe("The memory and CPU it uses beside its limits; null when that was not read.")
    ]
    posture: MeasurementPart
    supply_chain: Annotated[
        dict[str, Any],
        Describe(
            "Where its image comes from: source, provenance, packages, SBOM, attestations, and known "
            "vulnerabilities with when they were read. It also says how it meets the supply chain standard."
        ),
    ]


class ImageAt(TypedDict):
    tag: Annotated[str, Describe("The image's tag.")]
    digest: Annotated[
        str,
        Describe(
            "The image's digest, `sha256:` and its hash; empty when not read yet.",
            examples=("sha256:50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c",),
        ),
    ]


UpgradeRow = TypedDict(
    "UpgradeRow",
    {
        "address": Annotated[str, Describe("The container's `machine:container`, which a read of one names it by.")],
        "viable": Annotated[bool, Describe("Whether nothing blocks it: true when `blockers` is empty.")],
        "recommended": Annotated[
            bool,
            Describe(
                "Whether the move is worth making: the new version brings no known vulnerability of its own and, "
                "where the running one has some, fixes at least one."
            ),
        ],
        "risk": Annotated[
            str, Describe(ended(f"How likely the upgrade is to go wrong: {listed((LOW, MEDIUM, HIGH))}"))
        ],
        "change": Annotated[
            str,
            Describe(
                ended(
                    "The size of the version change, by the first number that differs: "
                    f"{listed((PATCH, MINOR, MAJOR, UNKNOWN_CHANGE))}"
                )
            ),
        ],
        "from": Annotated[ImageAt, Describe("The image it runs now.")],
        "to": Annotated[ImageAt, Describe("The image the upgrade would move it to.")],
        "blockers": Annotated[list[WrittenBlocker], Describe("Each reason it cannot go ahead yet; empty when none.")],
    },
)


class ReleasePart(TypedDict):
    tag: Annotated[str, Describe("The release's tag.")]
    url: Annotated[str, Describe("The release's page.")]
    published: Annotated[str | None, Describe("When it was published, an ISO 8601 date-time; null when not said.")]


class AdvisoryPart(TypedDict):
    id: Annotated[str, Describe("The advisory's or vulnerability's id, such as a GHSA or CVE id.")]
    severity: Annotated[str, Describe("How serious it is, as its source rates it.")]
    summary: Annotated[str, Describe("What the advisory warns of, in a line.")]
    url: Annotated[str, Describe("Its page.")]


class MountPart(TypedDict):
    type: Annotated[str, Describe("What sort of mount it is, such as `bind` or `volume`.")]
    source: Annotated[str, Describe("Where its data is kept on the machine, or the volume's name.")]
    destination: Annotated[str, Describe("Where it is mounted in the container.")]
    read_only: Annotated[bool, Describe("Whether the container may only read it.")]


class UpgradeRecord(UpgradeRow):
    """One upgrade whole: what it fixes and introduces, and its data."""

    release: Annotated[ReleasePart | None, Describe("The release of the version it moves to; null for none.")]
    fixes: Annotated[
        list[AdvisoryPart],
        Describe(
            "Of the running version's known vulnerabilities (its advisories and its serious package findings that "
            "have a fix), those the new version is clear of."
        ),
    ]
    introduces: Annotated[
        list[AdvisoryPart],
        Describe(
            "What the new version would bring of its own: an advisory against it alone, or a serious package finding."
        ),
    ]
    stateful: Annotated[bool, Describe("Whether it keeps data in writable mounts, which `data` lists.")]
    data: Annotated[
        list[MountPart],
        Describe("The writable mounts holding its data."),
    ]
    verified_by: Annotated[
        list[str],
        Describe("What would show the new version works, such as its health check; empty when nothing would."),
    ]
    not_automatic: Annotated[
        list[WrittenBlocker],
        Describe(
            "Why it would not apply on its own, though an operator may approve it. Every blocker comes first, "
            "then the bar a patch must clear."
        ),
    ]


class PinPart(TypedDict):
    service: Annotated[str, Describe("The compose service the image line is under; empty when the diff does not say.")]
    before: Annotated[
        str | None, Describe("The image the line named before, as `registry/repository:tag`; null for a new line.")
    ]
    after: Annotated[str, Describe("The image the line names now, as `registry/repository:tag`.")]
    digest: Annotated[
        str, Describe("The digest the new image names, as pinned or as its registry says; empty when not read.")
    ]
    scanned: Annotated[bool, Describe("Whether the new image's packages were checked against OSV.")]


class AppUpdateRow(TypedDict):
    key: Annotated[str, Describe("`owner:repository:number:app`, which a read of one names it by.")]
    repository: Annotated[str, Describe("The repository the pull request is open in, as `owner/name`.")]
    number: Annotated[int, Describe("The pull request's number.")]
    url: Annotated[str, Describe("The pull request on GitHub.")]
    head: Annotated[str, Describe("The pull request's head commit.")]
    app: Annotated[str, Describe("The app it updates: the directory under `compose/` its files are in.")]
    containers: Annotated[list[str], Describe("Each running container it moves, as `machine:container`.")]
    readable: Annotated[bool, Describe("Whether GitHub gave every Compose file's diff; false leaves `pins` empty.")]
    scanned: Annotated[
        bool, Describe("Whether every new image was checked against OSV: until then, `brings` is not known.")
    ]


class ChecksPart(TypedDict):
    state: Annotated[
        Literal["success", "pending", "failure", ""],
        Describe("How the head's checks stand: any failing, any still running, all passed, or none ran."),
    ]
    total: Annotated[int, Describe("How many checks ran on the head.")]
    failing: Annotated[list[str], Describe("The name of each check that failed.")]
    running: Annotated[int, Describe("How many checks are still running.")]
    why: Annotated[str, Describe("Why they fail, in the failing tool's own words; empty while none fail.")]


class MergePart(TypedDict):
    words: Annotated[
        str, Describe("How the approval asked at the head stands (its preview, merge and apply), in the page's words.")
    ]
    tone: Annotated[str, Describe("Its status tone: `attention` while it works, `good` once applied.")]
    run_url: Annotated[str, Describe("The page of the run it stands on, the preview or the apply; empty for none.")]
    taken: Annotated[
        bool, Describe("Whether it previews, merges, applies or merged, so no second approval is offered.")
    ]
    lines: Annotated[
        list[str],
        Describe(
            "Its detail: what it waits on, how long its run has run, or of a preview or an apply that ended,"
            " the step it stopped on, what that said and each app the run did not release."
        ),
    ]


class AppUpdateRecord(AppUpdateRow):
    """One open update of an app whole: its pins, checks, what it fixes and
    brings, and where approving it stands (``app_update_approval.approval_of``)."""

    title: PullTitle
    pins: Annotated[list[PinPart], Describe("Each image line it moves.")]
    checks: Annotated[ChecksPart, Describe("The head's checks.")]
    merge: Annotated[MergePart | None, Describe("The merge an approval asked at the head; null before one is asked.")]
    held: Annotated[
        str, Describe("Why `app_update.approve` would be refused now, as the page says it; empty when it would run.")
    ]
    fixes: Annotated[
        list[AdvisoryPart],
        Describe("Of the moved containers' known vulnerabilities, those the new images are clear of."),
    ]
    brings: Annotated[
        list[AdvisoryPart],
        Describe(
            "What the new images bring of their own: an advisory against them alone, or a serious package finding."
        ),
    ]
