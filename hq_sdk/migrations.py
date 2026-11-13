"""For an extension's own migrations: move the rows the host keeps for it, under a renamed capability."""

from hq.platform.application.capability_renames import CAPABILITY_ROWS, RenameCapabilities

__all__ = ["CAPABILITY_ROWS", "RenameCapabilities"]
