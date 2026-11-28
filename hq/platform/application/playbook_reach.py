"""Where a playbook can run now: the machines it is written for that the
automation worker has a login to as its profile.

The one answer every surface asks, the run's own check among them, as
Kubernetes' SelfSubjectAccessReview answers a UI from the authorizer the
action meets: a page offers a run, or names a machine one runs on, only where
``reached`` would let it through.
"""

from hq.domains.control_plane.names import normalized_hostname
lazy from hq.domains.control_plane.connection_shapes import READ_PROFILE
lazy from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND

from .automation_manifest import ProcedureTerms
from .record_errors import PolicyError, RecordNotFound
lazy from .automation_adoption import scan_result
lazy from .machines import machine_catalog
lazy from .projection import read_once


def login_profile(terms: ProcedureTerms) -> str:
    """The profile of the login a run goes through: its privilege, or the
    read login's for one that names none."""

    return terms.privilege or READ_PROFILE


def runner_reaches() -> frozenset[tuple[str, str]]:
    """Each machine and profile the worker has a login for, as its newest scan said."""

    newest = scan_result()
    return (
        frozenset((normalized_hostname(found["machine"]), found["profile"]) for found in newest.reaches)
        if newest
        else frozenset()
    )


def _machines() -> dict[str, tuple[str, ...]]:
    """Each machine HQ knows, by its name in HQ, to every name it is known by."""

    return {
        normalized_hostname(machine.name): tuple(normalized_hostname(name) for name in (machine.name, *machine.aliases))
        for machine in read_once("automation.machines", machine_catalog)
    }


def reachable(terms: ProcedureTerms) -> tuple[str, ...]:
    """The machines a run can be asked on now, each by its name in HQ, A to Z:
    those it is written for that the worker has a login to as its profile, by
    any name the machine is known by."""

    profile = login_profile(terms)
    reaches = runner_reaches()
    return tuple(
        sorted(
            host
            for host, names in _machines().items()
            if terms.admits(MACHINE_KIND, host) and any((name, profile) in reaches for name in names)
        )
    )


def resolved_hosts(terms: ProcedureTerms, hosts: tuple[str, ...]) -> tuple[str, ...]:
    """Each host named, as the machine HQ knows it by, held to what the playbook admits.

    A machine is found by its name or any name it is also known by, so a run
    asked by an alias binds to the one machine it is, and two runs naming one
    machine two ways are seen to share it.
    """

    known: dict[str, str] = {}
    for canonical, names in _machines().items():
        for name in names:
            known.setdefault(name, canonical)
    unknown = [host for host in hosts if host not in known]
    if unknown:
        raise RecordNotFound(
            "A host this names is no machine HQ knows; details.unknown lists them.", details={"unknown": unknown}
        )
    resolved = tuple(sorted({known[host] for host in hosts}))
    refused = [host for host in resolved if not terms.admits(MACHINE_KIND, host)]
    if refused:
        raise PolicyError(
            "This playbook is not written for a machine this names; details.refused lists them, and "
            "details.admits what it is written for.",
            details={
                "refused": refused,
                "admits": {"target_kinds": list(terms.target_kinds), "hosts": list(terms.hosts)},
            },
        )
    return resolved


def reached(terms: ProcedureTerms, hosts: tuple[str, ...]) -> None:
    """Refuse a run on a machine ``reachable`` does not name.

    The worker's own word, so it can make this check wrong and nothing more:
    the worker refuses at the claim a machine it cannot reach as the profile.
    """

    profile, now = login_profile(terms), reachable(terms)
    missing = [{"machine": host, "profile": profile} for host in hosts if host not in now]
    if missing:
        raise PolicyError(
            "HQ has no runner connection to a machine this names, as this playbook's profile; "
            "details.missing lists each. A connection added since the last scan shows once a scan reads it.",
            details={"missing": missing},
        )
