"""Readings HQ takes of the requests it serves.

One record per source device: which device or address, how its requests
arrived, when last and how many in the current window. Nothing a request
carries beyond that is a field here, so the schema drops it: no path, query,
header value, body or user agent.
"""

from __future__ import annotations

from .contract import ObservationRecord, ObservationSpec

ARRIVAL_KIND = "hq.request_path"


class ArrivalRecord(ObservationRecord):
    # The address HQ judged the caller by, and the tailnet device holding it.
    address: str
    device: str = ""
    # tailnet, network, loopback, opaque or elsewhere: ``connection.Channel.id``.
    channel: str = ""
    # direct, relayed or negotiating, as the tailnet reading said at the time.
    carried: str = ""
    # The trusted proxy's address when one forwarded the request, else "".
    forwarded_by: str = ""
    first_seen: str = ""
    last_seen: str = ""
    # Requests counted since ``window_start``.
    count: int = 0
    window_start: str = ""


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        ARRIVAL_KIND,
        "hq",
        "Request arrival",
        ArrivalRecord,
        title=lambda record: str(record.get("device") or record.get("address") or ""),
        relation="Reached HQ",
        read_by="request",
    ),
)
