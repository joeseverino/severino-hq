"""A callback from a collection a project's own ansible.cfg puts on the
collections path and enables: it writes the file ``HQ_EXAMPLE_LOADED`` names
once a playbook starts, so the smoke sees whether a project's configuration
reached a run."""

import os
from pathlib import Path
from typing import Any

from ansible.plugins.callback import CallbackBase  # type: ignore[import-not-found]  # ansible-core is the runner group's alone


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "example.smoke.loud"
    CALLBACK_NEEDS_ENABLED = True

    def v2_playbook_on_start(self, playbook: Any) -> None:
        marker = os.environ.get("HQ_EXAMPLE_LOADED")
        if marker:
            Path(marker).write_text("loaded\n", encoding="utf-8")
