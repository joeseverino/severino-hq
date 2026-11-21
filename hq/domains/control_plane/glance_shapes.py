"""What the controller reports when it reads a dashboard panel: numbers,
instants and states as their sources state them, and no words.

HQ validates each report with these models, stores it as it came, and words
it as a page asks (``application.glance``). The bridge contract carries the
same models (``provider_spec.SHARED``), so the controller's types are generated
from them.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints
from pydantic.json_schema import SkipJsonSchema

# A number the controller holds as a float64.
Double = Annotated[float, Field(json_schema_extra={"format": "double"})]
# A byte count.
Bytes = Annotated[int, Field(ge=0)]
# The failure class of a read that failed (runtime.Classify).
Failure = Annotated[str, StringConstraints(max_length=60)]

# A part or value present only on some readings. The contract leaves the null
# out, so the controller holds it behind a pointer and sends nothing for none.
_ABSENT = Field(default=None, json_schema_extra={"x-go-type-skip-optional-pointer": False})


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Host(_Shape):
    """A machine read over SSH, as the controller's host script prints it."""

    cpu_percent: Double
    cores: int
    load_1m: Double
    memory_used: Bytes
    memory_total: Bytes
    storage_used: Bytes
    storage_total: Bytes


class Docker(_Shape):
    """A machine read through its Docker environment: what its running
    containers take of it. ``cpu_percent`` is a share of the whole machine.
    How many there are is the sweep's to count (``ContainerCount``)."""

    cpu_percent: Double
    cores: int
    memory_used: Bytes
    memory_total: Bytes
    storage: Bytes


class Machine(_Shape):
    """One machine on the infrastructure panel. A machine with neither part
    and no failure is one the controller had no way to read."""

    key: str
    host: Host | SkipJsonSchema[None] = _ABSENT
    docker: Docker | SkipJsonSchema[None] = _ABSENT
    refresh_failed: Failure = ""


class Hour(_Shape):
    """One hour of the Weather Service's hourly forecast."""

    start: AwareDatetime
    name: str = ""
    temperature: Double | SkipJsonSchema[None] = _ABSENT
    temperature_unit: str = ""
    forecast: str = ""
    chance: int = 0
    wind_speed: str = ""
    wind_direction: str = ""


class Alert(_Shape):
    """One active Weather Service alert for the point."""

    event: str = ""
    headline: str = ""
    severity: str = ""


class Weather(_Shape):
    """The forecast at the configured point: the hours ahead, the first of
    them now, and the alerts in force."""

    point: str
    city: str = ""
    state: str = ""
    hours: Annotated[list[Hour], Field(max_length=12)] = []
    alerts: Annotated[list[Alert], Field(max_length=20)] = []
    refresh_failed: Failure = ""


class GlanceObservation(_Shape):
    """One panel the controller read for a glance plan."""

    panel_id: Literal["infrastructure", "weather"]
    as_of: str
    machines: list[Machine] = []
    weather: Weather | SkipJsonSchema[None] = _ABSENT
