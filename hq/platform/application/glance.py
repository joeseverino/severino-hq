"""Typed, cached dashboard observations and their explicit refresh queue."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from typing import Annotated, Any, Literal, TypedDict

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from pydantic import AfterValidator, BaseModel, Field, TypeAdapter, ValidationError

from hq.domains.control_plane.glance_shapes import GlanceObservation, Hour, Machine, Weather
from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
)
lazy from hq.domains.control_plane.bridge_actions import Admitted, admission
lazy from hq.platform.application.routes import reverse

from . import readings
from .answer_fields import CallMessage
from .asks import FAILED, QUEUED, REFRESHING, RUNNING, Ask, Asked, AskedAnswer, Standing
from .cadence import ask
from .command_fields import Describe
from .connections import ConnectionAbility, ConnectionInstance, ConnectionLink, ConnectionSpec, machines_once
from .contracts import Effect
from .drawings import StripTile
from .freshness import DASHBOARD_GLANCE, freshness
from .labels import dotted, human_bytes
from .moments import moment, when_hour
from .record_errors import PolicyError, RecordNotFound
from .security import Capability, Principal
from .tone import Tone
from .ui import MISSING, counted, measured, severity_tone
lazy from .capabilities import capability_words, command_spec
lazy from .projection import read_once

# The National Weather Service API. The connection shows it and the glance plan
# hands it to the controller, which states no address of its own.
NWS_API = "https://api.weather.gov"


def connection_specs():
    """Emit the keyless NWS boundary without spending a discovery query."""

    def instances():
        return (
            ConnectionInstance(
                id="national-weather-service",
                label="National Weather Service",
                kind="nws",
                status=Tone.GOOD,
                status_label="keyless",
                detail="Weather for the dashboard. Public, no key needed.",
                endpoint=NWS_API,
                credential_model="none",
                ability_names=("nws.hourly_forecast", "nws.active_alerts"),
                targets=(ConnectionLink("Dashboard weather", reverse("dashboard")),),
            ),
        )

    return (
        ConnectionSpec(
            name="hq.nws",
            label="National Weather Service",
            summary="Hourly forecast and weather alerts for the dashboard. Public, no key needed.",
            required_capability=Capability.READ,
            instance_provider=instances,
            abilities=(
                # No capability: no command performs these. The dashboard reads
                # the forecast as it renders, so the family's own routes are
                # where a reader goes.
                ConnectionAbility(
                    "nws.hourly_forecast",
                    "Hourly forecast",
                    "Current conditions and the hourly forecast for the point.",
                    effect=Effect.READ,
                    grant="none",
                ),
                ConnectionAbility(
                    "nws.active_alerts",
                    "Active weather alerts",
                    "Active National Weather Service alerts for the point.",
                    effect=Effect.READ,
                    grant="none",
                ),
            ),
            web_route="dashboard",
            management_route="dashboard_glance_settings",
            documentation_url="https://www.weather.gov/documentation/services-web-api",
        ),
    )


@dataclass(frozen=True, slots=True)
class DashboardPanelSpec:
    """What a glance panel is and how its readings are shown.

    ``short_labels`` names a reading in the compact head. ``alert_metric`` is
    the reading drawn as the panel's alert chip.
    """

    id: str
    label: str
    empty: str
    icon: str
    head_labels: bool = True
    alert_metric: str = ""
    short_labels: tuple[tuple[str, str], ...] = ()
    # The reading the head leads with, then the ones in the line under it, by
    # display label. The first lead the reading holds is the one shown.
    lead: tuple[str, ...] = ()
    caption: tuple[str, ...] = ()
    # Where the whole story is: (label, url), both built by HQ.
    link: tuple[str, str] | None = None
    # The reading whose words choose the panel's icon, by display label.
    icon_from: str = ""


def dashboard_configuration() -> DashboardConfiguration:
    return DashboardConfiguration.objects.filter(pk=1).first() or DashboardConfiguration()


# The words a forecast uses, most telling first, and the mark each earns. The
# Weather Service says "Clear" at night and "Sunny" by day.
_CONDITION_ICONS = (
    (("rain", "shower", "thunder", "drizzle", "storm"), "rain"),
    (("partly",), "weather"),
    (("cloud", "overcast", "fog", "snow"), "cloud"),
    (("sunny",), "sun"),
    (("clear",), "moon"),
)


def condition_icon(words: str, fallback: str) -> str:
    """The icon a forecast's words call for, or ``fallback``."""

    words = words.casefold()
    return next(
        (icon for keys, icon in _CONDITION_ICONS if any(key in words for key in keys)),
        fallback,
    )


def _forecast_url(point: str) -> str:
    latitude, longitude = point.split(",")
    return f"https://forecast.weather.gov/MapClick.php?lat={latitude}&lon={longitude}"


def panel_specs(
    configuration: DashboardConfiguration | None = None,
) -> tuple[DashboardPanelSpec, ...]:
    configuration = configuration or dashboard_configuration()
    specs = [
        DashboardPanelSpec(
            "infrastructure",
            configuration.infrastructure_label,
            "Choose “Show on dashboard” in a machine's settings.",
            icon="server",
            short_labels=(("Containers", "containers"), ("Docker storage", "Storage")),
            lead=("Containers", "CPU"),
            caption=("CPU", "Memory"),
            link=("Containers", reverse("control_plane:containers")),
        )
    ]
    if configuration.weather_point:
        specs.append(
            DashboardPanelSpec(
                "weather",
                configuration.weather_label,
                "The forecast appears here once it is read.",
                icon="weather",
                head_labels=False,
                alert_metric="Alerts",
                lead=("Temperature",),
                caption=("Conditions", "Rain", "Range"),
                link=("Forecast at weather.gov", _forecast_url(configuration.weather_point)),
                icon_from="Conditions",
            )
        )
    return tuple(specs)


def dashboard_machines() -> tuple[ManagedResource, ...]:
    """The machines chosen for the dashboard, read once per page: the panels and
    the first steps both ask."""

    return read_once("glance.dashboard_machines", _dashboard_machines)


def _dashboard_machines() -> tuple[ManagedResource, ...]:
    """Enabled declared machines selected for the dashboard, in operator order."""

    return tuple(
        placement.machine
        for placement in DashboardMachine.objects.select_related("machine").filter(
            machine__kind="machine", machine__enabled=True
        )
    )


def _machine_routes(
    resources: tuple[ManagedResource, ...],
) -> dict[int, tuple[str, ...]]:
    catalog = {item.name.lower(): item for item in machines_once()}
    controller_ids = set(ProviderConnection.objects.filter(reachable=True).values_list("controller_id", flat=True))
    routes = {}
    for resource in resources:
        name = str(resource.spec.get("name") or resource.key).lower()
        found = catalog.get(name)
        # Telemetry is read through a credential that opens the machine.
        opened_by = tuple(found.opened_by) if found else ()
        if opened_by or resource.key in controller_ids:
            routes[resource.pk] = opened_by
    return routes


def dashboard_machine_selected(key: str) -> bool:
    return DashboardMachine.objects.filter(machine__key=key, machine__kind="machine", machine__enabled=True).exists()


DASHBOARD_MACHINE = "dashboard.machine.select"


@dataclass(frozen=True, slots=True)
class DashboardMachineCommand:
    """Whether the machine's telemetry is shown on the dashboard."""

    selected: Annotated[bool, Describe("Whether the dashboard shows this machine's telemetry.")]


class DashboardMachineSet(TypedDict):
    """A machine's place on the dashboard, as the call left it."""

    ok: Literal[True]
    machine: Annotated[str, Describe("The machine's key while the dashboard shows it; empty when it does not.")]


@transaction.atomic
def select_dashboard_machine(
    command: DashboardMachineCommand, *, principal: Principal, current_key: str
) -> DashboardMachineSet:
    del principal
    resource = ManagedResource.objects.filter(key=current_key, kind="machine").first()
    if resource is None:
        raise RecordNotFound("HQ has no machine by that key.")
    if not resource.enabled:
        raise PolicyError("Dashboard telemetry requires an enabled machine record.")
    shown = DashboardMachine.objects.filter(machine=resource).exists()
    if command.selected == shown:
        return {"ok": True, "machine": resource.key if shown else ""}
    if command.selected:
        last = DashboardMachine.objects.aggregate(value=Max("position"))["value"]
        placement = DashboardMachine(machine=resource, position=(last + 1) if last is not None else 0)
        placement.audit_said = f"{resource.key} shown on the dashboard"
        placement.save()
    else:
        placement = DashboardMachine.objects.select_related("machine").get(machine=resource)
        placement.audit_gone = f"{resource.key} taken off the dashboard"
        placement.delete()
    return {"ok": True, "machine": resource.key if command.selected else ""}


@cache
def _coordinates() -> TypeAdapter[tuple[float, float]]:
    return TypeAdapter(tuple[Annotated[float, Field(ge=-90, le=90)], Annotated[float, Field(ge=-180, le=180)]])


def _as_point(value: Any) -> Any:
    """A point sent as text, as latitude,longitude each to four places, or
    empty for none; anything else is refused as its type is validated."""

    if not isinstance(value, str) or not value.strip():
        return value.strip() if isinstance(value, str) else value
    latitude, longitude = _coordinates().validate_python([part.strip() for part in value.split(",")])
    return f"{latitude:.4f},{longitude:.4f}"


DASHBOARD_SETTINGS = "dashboard.settings.update"


@dataclass(frozen=True, slots=True)
class DashboardSettingsCommand:
    """Where the weather is read for, and what the glance strip calls its panels."""

    weather_point: Annotated[
        str, AfterValidator(_as_point), Describe("Where the weather is read for, as latitude,longitude.")
    ] = ""
    weather_label: Annotated[str, Describe("What the weather panel calls that place.")] = ""
    infrastructure_label: Annotated[str, Describe("What the infrastructure panel is called.")] = ""


class DashboardSettingsSaved(TypedDict):
    """The dashboard's settings, saved."""

    ok: Literal[True]
    message: CallMessage


@transaction.atomic
def save_dashboard_settings(command: DashboardSettingsCommand, *, principal: Principal) -> DashboardSettingsSaved:
    del principal
    configuration = DashboardConfiguration.objects.select_for_update().filter(pk=1).first() or DashboardConfiguration()
    configuration.weather_point = command.weather_point
    configuration.weather_label = command.weather_label.strip()[:40] or "Weather"
    configuration.infrastructure_label = command.infrastructure_label.strip()[:40] or "Homelab"
    configuration.audit_said = "Dashboard settings saved"
    configuration.save()
    return {"ok": True, "message": "Dashboard settings saved."}


def _panel(
    spec: DashboardPanelSpec,
    *,
    payload: dict[str, Any],
    observed_at: Any,
    refreshing: bool,
    refreshable: bool,
    subject: str = "",
) -> dict[str, Any]:
    """One panel as the glance template reads it, from its spec and its reading
    as words (``machine_words``, ``weather_words``). ``subject`` is what its
    head names, where panels alike stand side by side."""

    shown = tuple(_glance_reading(metric, spec) for metric in payload.get("metrics", []))
    return {
        "id": spec.id,
        "label": spec.label,
        "subject": subject,
        "empty": spec.empty,
        "icon": condition_icon(next((str(r["value"]) for r in shown if r["label"] == spec.icon_from), ""), spec.icon)
        if spec.icon_from
        else spec.icon,
        "head_labels": spec.head_labels,
        "link": {"label": spec.link[0], "url": spec.link[1]} if spec.link else None,
        "payload": payload,
        "observed_at": observed_at,
        "refreshing": refreshing,
        "refreshable": refreshable,
        "standing": _standing(payload, refreshing=refreshing),
        "readings": shown,
        "hours": payload.get("hours", ()),
        **_head(shown, spec),
    }


def _metric(label: str, value: str, detail: str = "") -> dict[str, str]:
    return {"label": label, "value": value, "detail": detail}


def _used(part: int, whole: int) -> str:
    return measured(part * 100 / whole if whole else 0, "%", 0)


def _failed(failure: str) -> dict[str, Any]:
    """A panel whose last reading failed and had nothing before it to keep."""

    return {
        "status": Tone.SERIOUS,
        "summary": f"Refresh failed ({failure}).",
        "metrics": [],
        "refresh_failed": failure,
    }


def machine_words(machine: Machine) -> dict[str, Any]:
    """A machine's reading as its panel and its page say it: a tone, a summary
    and its metrics, each a label, a value and a detail."""

    failed = {"refresh_failed": machine.refresh_failed} if machine.refresh_failed else {}
    if host := machine.host:
        return {
            "status": Tone.GOOD,
            "summary": f"Host load {measured(host.load_1m, '', 2)}",
            "metrics": [
                _metric("CPU", measured(host.cpu_percent, "%", 0), counted(host.cores, "core")),
                _metric(
                    "Memory",
                    _used(host.memory_used, host.memory_total),
                    f"{human_bytes(host.memory_used)} of {human_bytes(host.memory_total)} used",
                ),
                _metric(
                    "Storage",
                    _used(host.storage_used, host.storage_total),
                    f"{human_bytes(host.storage_used)} of {human_bytes(host.storage_total)} used on /",
                ),
            ],
            **failed,
        }
    if docker := machine.docker:
        return {
            "status": Tone.GOOD,
            "summary": dotted(counted(docker.cores, "core"), f"{human_bytes(docker.memory_total)} memory"),
            "metrics": [
                _metric("Containers", str(docker.containers), "running"),
                _metric("CPU", measured(docker.cpu_percent, "%", 0), f"of {counted(docker.cores, 'core')}"),
                _metric(
                    "Memory",
                    _used(docker.memory_used, docker.memory_total),
                    f"{human_bytes(docker.memory_used)} of {human_bytes(docker.memory_total)}",
                ),
                _metric("Docker storage", human_bytes(docker.storage), "layers, volumes and build cache"),
            ],
            **failed,
        }
    if failed:
        return _failed(machine.refresh_failed)
    return {
        "status": Tone.ATTENTION,
        "summary": "No host telemetry connection is available.",
        "metrics": [],
    }


# A chance of rain, in percent, that the weather panel names as coming.
RAIN_CHANCE = 30


def _degrees(hour: Hour) -> str:
    return measured(hour.temperature, "°", -1)


def _alert_tone(weather: Weather) -> Tone:
    """The most severe tone among the alerts in force."""

    return Tone.worst((severity_tone(alert.severity.lower()) for alert in weather.alerts), default=Tone.NEUTRAL)


def weather_words(weather: Weather) -> dict[str, Any]:
    """The forecast as the weather panel says it: a tone, the place, its
    metrics, and the hours ahead as tiles."""

    hours = weather.hours
    if not hours and weather.refresh_failed:
        return _failed(weather.refresh_failed)
    metrics = []
    if hours:
        now = hours[0]
        metrics.append(_metric("Conditions", now.forecast or MISSING, now.name))
        if now.temperature is not None:
            metrics.append(_metric("Temperature", _degrees(now) + (now.temperature_unit or "F")))
    if temperatures := [hour.temperature for hour in hours if hour.temperature is not None]:
        metrics.append(
            _metric(
                "Range",
                f"{measured(min(temperatures), '', -1)}–{measured(max(temperatures), '°', -1)}",
                f"next {counted(len(hours), 'hour')}",
            )
        )
    if rain := next(((index, hour) for index, hour in enumerate(hours) if hour.chance >= RAIN_CHANCE), None):
        index, hour = rain
        metrics.append(
            _metric(
                "Rain",
                dotted("Now" if index == 0 else when_hour(hour.start), measured(hour.chance, "%")),
                hour.forecast,
            )
        )
    if hours and (wind := f"{hours[0].wind_direction} {hours[0].wind_speed}".strip()):
        metrics.append(_metric("Wind", wind, "NWS hourly forecast"))
    if alerts := weather.alerts:
        more = f" +{len(alerts) - 1}" if len(alerts) > 1 else ""
        metrics.append(_metric("Alerts", (alerts[0].event or "Weather alert") + more, alerts[0].headline))
    words = {
        "status": _alert_tone(weather) if alerts else Tone.GOOD,
        "summary": ", ".join(part for part in (weather.city, weather.state) if part) or "National Weather Service",
        "metrics": metrics,
        # Each hour's time and temperature, and a chance of rain said only when
        # there is one.
        "hours": tuple(
            StripTile(
                when_hour(hour.start),
                _degrees(hour),
                note=measured(hour.chance, "%") if hour.chance else "",
                tip=hour.forecast,
            )
            for hour in hours
        ),
    }
    if weather.refresh_failed:
        words["refresh_failed"] = weather.refresh_failed
    return words


def _stored[Shape: BaseModel](shape: type[Shape], value: Any) -> Shape | None:
    """A stored reading's value as the shape the controller reports, or None.

    A reading kept in an older shape is none: it keeps its time, so the panel
    falls due as it would have and the dashboard asks for it again.
    """

    try:
        return shape.model_validate(value) if value is not None else None
    except ValidationError:
        return None


def machine_metrics(telemetry: Any) -> list[dict[str, str]]:
    """A machine's stored telemetry as the metrics its page lists."""

    machine = _stored(Machine, telemetry)
    return machine_words(machine)["metrics"] if machine else []


def _head(shown: tuple[dict[str, Any], ...], spec: DashboardPanelSpec) -> dict[str, Any]:
    """The reading a panel leads with, the line under it, and its alert."""

    readings_shown = tuple(reading for reading in shown if not reading["alert"])
    by_label = {reading["label"]: reading for reading in readings_shown}
    lead = next(
        (by_label[label] for label in spec.lead if label in by_label),
        readings_shown[0] if readings_shown else None,
    )
    alert = next((reading for reading in shown if reading["alert"]), None)
    return {
        "lead": lead,
        "caption": tuple(
            by_label[label] for label in spec.caption if label in by_label and by_label[label] is not lead
        ),
        "alert": alert["value"] if alert else "",
    }


def dashboard_panels(
    configuration: DashboardConfiguration | None = None,
) -> tuple[dict[str, Any], ...]:
    configuration = configuration or dashboard_configuration()
    specs = {spec.id: spec for spec in panel_specs(configuration)}
    machine_resources = dashboard_machines()
    routes = _machine_routes(machine_resources) if machine_resources else {}
    point = configuration.weather_point
    weather_key = readings.weather(point) if point else ""
    stored = readings.stored_many(
        [*(readings.machine_telemetry(resource.key) for resource in machine_resources), weather_key]
    )
    weather = stored.get(weather_key)
    pending = set(DashboardRefreshRequest.objects.filter(completed_at__isnull=True).values_list("panel_id", flat=True))
    panels = []
    for resource in machine_resources:
        panel_id = f"machine-{resource.pk}"
        reading = stored.get(readings.machine_telemetry(resource.key))
        # Several machines' readings read alike, so each head names its own.
        name = str(resource.spec.get("name") or resource.key) if len(machine_resources) > 1 else ""
        spec = replace(
            specs["infrastructure"],
            id=panel_id,
            label=name or specs["infrastructure"].label,
            empty=(
                "Its readings appear here once it is read."
                if resource.pk in routes
                else "HQ has no way to read this machine yet."
            ),
        )
        machine = _stored(Machine, reading.value if reading else None)
        panels.append(
            _panel(
                spec,
                payload=machine_words(machine) if machine else {},
                observed_at=reading.observed_at if reading else None,
                # A request nothing can answer is not a refresh in progress.
                refreshing=panel_id in pending and resource.pk in routes,
                refreshable=resource.pk in routes,
                subject=name,
            )
        )
    # Nothing empty takes space: with no machine chosen there is no machine
    # reading to draw, and Get started's step is what asks for one.
    if "weather" in specs:
        forecast = _stored(Weather, weather.value if weather else None)
        panels.append(
            _panel(
                specs["weather"],
                payload=weather_words(forecast) if forecast else {},
                observed_at=weather.observed_at if weather else None,
                refreshing="weather" in pending and bool(routes),
                # Weather rides with a machine's refresh, so it can be asked
                # for only when a controller reaches one of them.
                refreshable=bool(routes),
            )
        )
    now = timezone.now()
    shown = tuple(_with_freshness(panel, freshness(DASHBOARD_GLANCE, panel["observed_at"], now)) for panel in panels)
    # Current readings lead; an outdated one steps aside.
    return tuple(sorted(shown, key=lambda panel: panel["outdated"]))


def _standing(payload: dict[str, Any], *, refreshing: bool) -> Standing:
    """How the panel's last ask for a reading stands, as every ask says it:
    under way while one is pending, failed while its last reading failed."""

    if refreshing:
        return Standing(RUNNING, REFRESHING)
    if failed := payload.get("refresh_failed"):
        return Standing(FAILED, f"The last reading failed ({failed}).")
    return Standing()


# The strip, which a finished ask fetches again (_dashboard_glance.html).
GLANCE_REGION = "[data-glance]"


def refresh_ask(standing: Standing) -> Ask:
    """A panel's Refresh: an ask for every panel, standing as this one's reading does."""

    return Ask(
        capability_words(DASHBOARD_REFRESH),
        reverse("dashboard_glance"),
        standing=standing,
        refresh=GLANCE_REGION,
        outcome=True,
    )


def glance_context(
    configuration: DashboardConfiguration | None = None,
    panels: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """What the glance template reads, for the dashboard and its own endpoint."""

    configuration = configuration or dashboard_configuration()
    panels = dashboard_panels(configuration) if panels is None else panels
    if any(panel["refreshable"] for panel in panels):
        panels = tuple({**panel, "ask": refresh_ask(panel["standing"])} for panel in panels)
    return {
        "dashboard_panels": panels,
        # The strip is asked again for as long as a reading is on its way, and
        # asks for the due ones itself when the page opens.
        "dashboard_refreshing": any(panel.get("refreshing") for panel in panels),
        "dashboard_due": any(panel.get("due") for panel in panels),
        "dashboard_glance_settings": configuration,
    }


def _with_freshness(panel: dict[str, Any], found) -> dict[str, Any]:
    """A panel with whether to ask for it again and whether it is out of date.

    Due asks for a refresh when the dashboard opens; only stale says "Out of
    date" and shows the reading as of its age.
    """

    return {**panel, "due": found.due, "outdated": found.stale, "freshness": found}


def _glance_reading(metric: dict[str, str], spec: DashboardPanelSpec) -> dict[str, Any]:
    """Display labels without altering the stored observation."""

    label = metric.get("label", "")
    short_label = dict(spec.short_labels).get(label, label)
    return {
        **metric,
        "label": label,
        "short_label": short_label,
        # What pointing at the reading says: its name, and its detail when it has one.
        "tip": dotted(label, metric.get("detail")),
        # The words drawn after its value, for a panel that names its readings.
        "unit": short_label if spec.head_labels else "",
        "alert": bool(spec.alert_metric) and label == spec.alert_metric,
    }


DASHBOARD_REFRESH = "dashboard.panels.refresh"
DASHBOARD_REFRESH_STALE = "dashboard.panels.refresh_due"


@dataclass(frozen=True, slots=True)
class DashboardRefreshCommand:
    """A refresh names nothing: which panels it asks for is HQ's to work out."""


def _panel_names(ids: list[str]) -> list[str]:
    """Each panel by what a person calls it: its machine's name, the weather's place."""

    names = {f"machine-{machine.pk}": str(machine.spec.get("name") or machine.key) for machine in dashboard_machines()}
    names["weather"] = dashboard_configuration().weather_label or "Weather"
    return [names.get(panel_id, panel_id) for panel_id in ids]


class PanelsRefreshRequested(AskedAnswer):
    """The dashboard panels asked to be read again."""

    requested: Annotated[list[str], Describe("The panels asked to be read again, by id.")]


def _ask_for(ids: list[str], *, why: str, message: str = "") -> PanelsRefreshRequested:
    """Queue a reading of each panel in ``ids``, recorded once."""

    if ids:
        # Each panel by the name its head shows.
        ask(
            panels={
                panel_id: f"Asked for {why}: {name}" for panel_id, name in zip(ids, _panel_names(ids), strict=True)
            },
            doorbell="glance",
        )
    standing = Standing(QUEUED, REFRESHING) if ids else Standing()
    return {**Asked(standing, message=message).answer(), "requested": ids}


@transaction.atomic
def request_dashboard_refresh(_command: DashboardRefreshCommand, *, principal: Principal) -> PanelsRefreshRequested:
    """Ask for a fresh reading of every panel a controller can read."""

    del principal
    configuration = dashboard_configuration()
    machines = _dashboard_machines()
    routes = _machine_routes(machines) if machines else {}
    ids = [f"machine-{machine.pk}" for machine in machines if machine.pk in routes]
    if ids and configuration.weather_point:
        ids.append("weather")
    return _ask_for(ids, why="a fresh reading of the dashboard", message=command_spec(DASHBOARD_REFRESH).requested())


@transaction.atomic
def request_stale_panel_refresh(_command: DashboardRefreshCommand, *, principal: Principal) -> PanelsRefreshRequested:
    """Ask for the panels whose reading is due again.

    The dashboard posts this when it opens on a due reading. Only panels that
    are due, refreshable and not already waiting on a refresh are asked for,
    so repeat views of one card ask once.
    """

    del principal
    wanted = [
        str(panel["id"])
        for panel in dashboard_panels()
        if panel.get("due") and panel.get("refreshable") and not panel.get("refreshing")
    ]
    # Posted as the page opens, which says nothing of it.
    return _ask_for(wanted, why="the dashboard's due readings")


def dashboard_refresh_plan(controller_id: str) -> dict[str, Any]:
    ids = list(
        DashboardRefreshRequest.objects.filter(completed_at__isnull=True)
        .order_by("requested_at")
        .values_list("panel_id", flat=True)
    )
    configuration = dashboard_configuration()
    targets = _dashboard_machines()
    routes = _machine_routes(targets) if targets else {}
    connection_refs = {ref for values in routes.values() for ref in values}
    owned = set(
        ProviderConnection.objects.filter(
            controller_id=controller_id,
            connection_ref__in=connection_refs,
            reachable=True,
        ).values_list("connection_ref", flat=True)
    )
    machines = []
    controller_owns_machine = False
    for target in targets:
        owned_connections = [ref for ref in routes.get(target.pk, ()) if ref in owned]
        if owned_connections or target.key == controller_id:
            controller_owns_machine = True
            if f"machine-{target.pk}" in ids:
                machines.append(
                    {
                        "key": target.key,
                        "connections": owned_connections,
                        "request_id": f"machine-{target.pk}",
                    }
                )
    panels = []
    if machines:
        panels.append("infrastructure")
    if "weather" in ids and controller_owns_machine:
        panels.append("weather")
    return {
        "ok": True,
        "panels": panels,
        "targets": {
            "infrastructure": machines,
            "weather": {"point": configuration.weather_point, "endpoint": NWS_API},
        },
        # Every request made by now is one a reading begun after this answers.
        "as_of": timezone.now().isoformat(),
    }


def _refresh_is_pending(panel_id: str) -> bool:
    pending = DashboardRefreshRequest.objects.filter(completed_at__isnull=True)
    lookup = {"panel_id__startswith": "machine-"} if panel_id == "infrastructure" else {"panel_id": panel_id}
    return pending.filter(**lookup).exists()


def _answer(panel_id: str, *, as_of: Any, observed_at: Any) -> None:
    """Mark answered the request for ``panel_id`` if it was made by ``as_of``.

    A request made while the reading was under way stays pending: the reading
    began before it, so the next one answers it.
    """

    DashboardRefreshRequest.objects.filter(
        panel_id=panel_id, completed_at__isnull=True, requested_at__lte=as_of
    ).update(completed_at=observed_at)


def _record_machine_readings(
    observation: GlanceObservation, *, controller_id: str, as_of: Any, observed_at: Any
) -> None:
    expected = {target["key"] for target in dashboard_refresh_plan(controller_id)["targets"]["infrastructure"]}
    matched = False
    for machine in observation.machines:
        if machine.key not in expected:
            continue
        resource = ManagedResource.objects.filter(kind="machine", key=machine.key, enabled=True).first()
        if resource is None:
            continue
        matched = True
        reading_key = readings.machine_telemetry(resource.key)
        _keep(
            reading_key, machine, worked=lambda kept: kept.host is not None or kept.docker is not None, now=observed_at
        )
        _answer(f"machine-{resource.pk}", as_of=as_of, observed_at=observed_at)
    if not matched:
        raise ValueError("No machine HQ knows matched the controller's report.")


def _record_weather_reading(
    observation: GlanceObservation, *, configuration: DashboardConfiguration, as_of: Any, observed_at: Any
) -> None:
    weather = observation.weather
    if weather is None or weather.point != configuration.weather_point:
        raise ValueError("Weather was reported without a configured point.")
    _keep(readings.weather(weather.point), weather, worked=lambda kept: bool(kept.hours), now=observed_at)
    _answer("weather", as_of=as_of, observed_at=observed_at)


@transaction.atomic
def record_dashboard_observations(payload: list[dict[str, Any]], *, admitted: Admitted) -> dict[str, Any]:
    controller_id = admission(admitted).controller_id
    configuration = dashboard_configuration()
    allowed = {spec.id for spec in panel_specs(configuration)}
    now = timezone.now()
    stored = []
    for item in payload:
        panel_id = str(item.get("panel_id", "")).strip()
        if panel_id not in allowed:
            raise ValueError(f"Unknown dashboard panel {panel_id!r}.")
        if not _refresh_is_pending(panel_id):
            raise ValueError(f"No refresh is pending for {panel_id!r}.")
        planned = moment(item.get("as_of"), naive="refuse")
        if planned is None:
            raise ValueError(f"The reading of {panel_id!r} does not say when its plan was made.")
        try:
            observation = GlanceObservation.model_validate(item)
        except ValidationError as exc:
            raise ValueError(f"The reading of {panel_id!r} is not the shape the contract states.") from exc
        as_of = min(planned, now)
        if panel_id == "infrastructure":
            _record_machine_readings(observation, controller_id=controller_id, as_of=as_of, observed_at=now)
        else:
            _record_weather_reading(observation, configuration=configuration, as_of=as_of, observed_at=now)
        stored.append(panel_id)
    return {"ok": True, "recorded": stored, "observed_at": now.isoformat()}


def _keep[Shape: (Machine, Weather)](key: str, reading: Shape, *, worked: Callable[[Shape], bool], now: Any) -> None:
    """Store a panel's reading, as the controller reported it.

    A failed refresh never replaces a reading that worked. The last good reading
    keeps its own time, so the panel ages toward "Out of date" on its own, and
    the failure is carried as a note. Only a panel with nothing to show stores
    the failure itself.
    """

    previous = readings.stored(key)
    kept = _stored(type(reading), previous.value if previous else None)
    if reading.refresh_failed and previous is not None and kept is not None and worked(kept):
        kept = kept.model_copy(update={"refresh_failed": reading.refresh_failed})
        readings.record(key, kept.model_dump(mode="json", exclude_defaults=True), observed_at=previous.observed_at)
        return
    readings.record(key, reading.model_dump(mode="json", exclude_defaults=True), observed_at=now)
