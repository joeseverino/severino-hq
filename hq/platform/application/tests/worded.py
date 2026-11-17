"""Findings in the words the queue's cards carry them (``attention.finding_insight``), for a test that reads those words."""

from collections.abc import Iterable

from hq.platform.application.attention import finding_insight
from hq.platform.application.card_items import Insight
from hq.platform.application.finding_model import Finding
from hq.platform.application.routes import reverse


def worded(findings: Iterable[Finding]) -> tuple[Insight, ...]:
    """Each of ``findings`` as its card says it."""

    url = reverse("control_plane:findings")
    return tuple(finding_insight(finding, url) for finding in findings)
