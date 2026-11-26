from pathlib import Path
from typing import override

from hq.platform.core.management.derived import DerivedFileCommand
lazy from hq.domains.control_plane import bridge_contract


class Command(DerivedFileCommand):
    help = (
        "Write the controller's contracts from the registry: the bridge contract, the "
        "connections document's schema and the renderer's connection shapes, or --check them for drift."
    )
    source = "the registry"
    then = ", then `go generate ./...` in controller/ for the Go types generated from these"

    @override
    def emitted(self) -> dict[Path, str]:
        return bridge_contract.emitted()
