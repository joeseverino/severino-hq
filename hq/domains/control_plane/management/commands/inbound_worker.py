from pathlib import Path
from typing import override

from hq.platform.core.management.derived import DerivedFileCommand
lazy from hq.domains.control_plane import inbound_worker


class Command(DerivedFileCommand):
    help = "Write the inbound-events Worker's wrangler.jsonc and routes.json from INBOUND and its arrival.json from the bridge contract, or --check them for drift."
    source = "the providers' INBOUND declarations and the bridge contract's InboundArrival"

    @override
    def emitted(self) -> dict[Path, str]:
        return inbound_worker.emitted()
