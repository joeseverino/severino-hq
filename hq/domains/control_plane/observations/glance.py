"""Readings the controller takes when the dashboard opens: each machine's
figures and the forecast at the configured point.

Each record is the shape the controller reports (``glance_shapes``), stamped
with when it was read, one record per machine or point.
"""

from datetime import timedelta

from pydantic import ConfigDict

from ..glance_shapes import Machine, Weather
from .contract import ObservationRecord, ObservationSpec, Stamp

MACHINE_KIND = "machine.telemetry"
WEATHER_KIND = "nws.forecast"

# The keyless National Weather Service API the forecast comes from.
NWS = "nws"

# A dashboard reading costs a trip to a machine, so it is read again when the
# dashboard opens and it is older than this.
GLANCE_EVERY = timedelta(minutes=5)


class MachineReading(ObservationRecord, Machine):
    model_config = ConfigDict(extra="ignore", frozen=True)

    read_at: Stamp = None


class WeatherReading(ObservationRecord, Weather):
    model_config = ConfigDict(extra="ignore", frozen=True)

    read_at: Stamp = None


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    # A reading, not resource state: stored on the resource it made every
    # refresh a status change, and every status change an audit row. The
    # controller reads a machine through its host agent or its Docker
    # environment, so no one connection reads the kind.
    ObservationSpec(
        MACHINE_KIND,
        None,
        "Machine figures",
        MachineReading,
        title=lambda record: str(record.get("key", "")),
        relation="Load and memory of",
        every=GLANCE_EVERY,
        on_open=True,
    ),
    ObservationSpec(
        WEATHER_KIND,
        NWS,
        "Weather forecast",
        WeatherReading,
        title=lambda record: str(record.get("city") or record.get("point", "")),
        relation="Forecast for",
        every=GLANCE_EVERY,
        on_open=True,
    ),
)
