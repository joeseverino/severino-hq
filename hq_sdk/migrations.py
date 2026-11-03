"""Migration operations a plugin's own migrations use on rows the host stores for it."""

from hq.platform.application.capability_renames import CAPABILITY_ROWS, RenameCapabilities

__all__ = ["CAPABILITY_ROWS", "RenameCapabilities"]
