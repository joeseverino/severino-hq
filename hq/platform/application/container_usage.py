"""What a running container uses beside the limits it is run with, from its
runtime reading: the one answer the container page and the containers
resource give to what it costs."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hq.domains.control_plane.observations.portainer import MEMORY_BASES

from .timestamps import moment


@dataclass(frozen=True, slots=True)
class Usage:
    # Bytes in use, and how they were counted (MEMORY_BASES' words).
    memory: int
    memory_basis: str
    # Bytes and CPUs it is limited to; zero is no limit.
    memory_limit: int
    cpu_limit: float
    # A share of one core since the sweep before; None until two sweeps
    # sampled one run of it.
    cpu_percent: float | None
    read_at: datetime | None

    @property
    def memory_of_limit(self) -> float | None:
        """Its memory as a percentage of its limit; None without one."""

        return round(self.memory / self.memory_limit * 100, 1) if self.memory_limit else None


def usage_of(runtime: Mapping[str, Any] | None) -> Usage | None:
    """What one runtime record says the container uses; None where its sweep
    took no sample (it was not running, or the controller predates it)."""

    if runtime is None:
        return None
    read_at = moment(runtime.get("usage_read_at"))
    basis = str(runtime.get("memory_usage_basis") or "")
    if read_at is None and not basis:
        return None
    share = runtime.get("cpu_percent")
    return Usage(
        memory=int(runtime.get("memory_usage") or 0),
        memory_basis=MEMORY_BASES.get(basis, basis),
        memory_limit=int(runtime.get("memory_limit") or 0),
        cpu_limit=float(runtime.get("cpu_limit") or 0),
        cpu_percent=float(share) if share is not None else None,
        read_at=read_at,
    )
