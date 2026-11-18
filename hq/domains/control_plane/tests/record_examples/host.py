from hq.domains.control_plane.observations.host import (
    FILESYSTEM_KIND,
    REBOOT_KIND,
    RENDER_REASONS,
    RENDER_STATUS_KIND,
    UNIT_KIND,
    UNREADABLE_WORD,
)

from . import ABSENT, Kept, Refused

EXAMPLES = {
    UNIT_KIND: (
        Refused("unit", "/etc/example/credentials"),
        Refused("unit", "example.service; id"),
        Refused("active", "failed with EXAMPLE_TOKEN=sentinel-value"),
        Refused("result", "Exit Code"),
        Refused("ended_at", "yesterday afternoon"),
        Refused("activates", "sentinel-value"),
        Refused("main_status", -1),
        Refused("title", "a" * 121),
    ),
    FILESYSTEM_KIND: (
        Refused("mount", "relative"),
        Refused("mount", "/a|b"),
    ),
    REBOOT_KIND: (
        Refused("packages", ["linux image"]),
        Refused("packages", ["a|b"]),
        Refused("packages", ["Libc6"]),
    ),
    RENDER_STATUS_KIND: (
        Refused("renderer", ABSENT),
        Refused("renderer", "/run/example/status.json"),
        Refused("renderer", "Example Renderer"),
        Refused("state", "fine"),
        Refused("reason", "the file was not there"),
        Refused("status.connect.dependencies", [{"service": "sync", "status": "ACTIVE"}] * 17),
        *(Kept("reason", reason) for reason in RENDER_REASONS),
        # A string that is not one short word is never stored as what it said.
        Kept("status.last_attempt.failure", "the token was refused by Connect", UNREADABLE_WORD),
        Kept("status.connect.version", "1.8.1 (example build)", UNREADABLE_WORD),
        Kept("status.connect.dependencies.0.status", "TOKEN NEEDED\nnow", UNREADABLE_WORD),
        Kept("status.last_attempt.at", "2026-01-01T00:00:00.123456789Z", "2026-01-01T00:00:00.123456+00:00"),
    ),
}
