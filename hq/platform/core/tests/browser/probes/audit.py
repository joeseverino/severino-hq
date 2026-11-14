"""browser_audit.js, the audit dev:look shares with the gate, as a probe."""

from pathlib import Path

from hq.platform.core.tests.browser.probes import Check

AUDIT = Path(__file__).parents[3] / "browser_audit.js"

# The rule from browser_audit.js that finds what a reader loses and no other
# check does: a scrollbar no one asked for. Content clipped out of sight is
# the sideways check's; the other rules judge density and evenness, which
# dev:look shows by eye rather than the gate.
AUDIT_RULES = {"unwanted-scrollbar"}

_AUDIT = (
    "async () => { const audit = "
    + AUDIT.read_text(encoding="utf-8").strip()
    + ";\nreturn await audit({ evaluate: (probe) => probe() }); }"
)


def audit_findings(report):
    """The audit's violations of the rules the gate holds."""

    return [v for v in report["violations"] if v["rule"] in AUDIT_RULES]


# The survey checks these probes answer (`probes/__init__.py`).
CHECKS = {"audit": Check(_AUDIT, findings=audit_findings, reader_sees="a scrollbar on a box nobody asked to scroll")}
