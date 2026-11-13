"""A callback a project's own ansible.cfg enables: it writes the file
``HQ_EXAMPLE_LOADED`` names once a playbook starts, so the smoke sees whether a
project's configuration or plugins reached a run."""

import os
from pathlib import Path
from typing import Any

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "example_loud"
    CALLBACK_NEEDS_ENABLED = True

    def v2_playbook_on_start(self, playbook: Any) -> None:
        marker = os.environ.get("HQ_EXAMPLE_LOADED")
        if marker:
            Path(marker).write_text("loaded\n", encoding="utf-8")
