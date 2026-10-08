"""Typed, cached dashboard observations and their explicit refresh queue."""

import re
from dataclasses import dataclass, replace
from typing import Any

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
    WeatherObservation,
)
from hq.platform.core.audit import operation_context

from . import readings
from .cadence import ring_doorbell
from .connections import machines_once
from .freshness import DASHBOARD_GLANCE, freshness
from .security import Capability, Principal

# The National Weather Service API. The connection shows it and the glance plan
# hands it to the controller, which states no address of its own.
NWS_API = "https://api.weather.gov"


def connection_specs():
    """Emit the keyless NWS boundary without spending a discovery query."""

    from hq.platform.application.routes import reverse

    from .connections import (
        ConnectionAbility,
        ConnectionInstance,
        ConnectionLink,
        ConnectionSpec,
    )

    def instances():
        return (
            ConnectionInstance(
                id="national-weather-service",
                label="National Weather Service",
                kind="nws",
                status="good",
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
                    effect="read",
                    grant="none",
                ),
                ConnectionAbility(
                    "nws.active_alerts",
                    "Active weather alerts",
                    "Active National Weather Service alerts for the point.",
                    effect="read",
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

    ``labels`` renames a reported metric everywhere it is shown; ``short_labels``
    names it in the compact head. ``alert_metric`` is the metric counted as the
    panel's alert chip, dropped while it reads zero.
    """

    id: str
    label: str
    empty: str
    icon: str
    head_labels: bool = True
    alert_metric: str = ""
    labels: tuple[tuple[str, str], ...] = ()
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
    from hq.platform.application.routes import reverse

    configuration = configuration or dashboard_configuration()
    specs = [
        DashboardPanelSpec(
            "infrastructure",
            configuration.infrastructure_label,
            "Choose “Show on dashboard” in a machine's settings.",
            icon="server",
            labels=(("Container CPU", "CPU"), ("Container memory", "Memory")),
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
                "Press Read now to fetch the forecast.",
                icon="weather",
                head_labels=False,
                alert_metric="Alerts",
                labels=(("Now", "Conditions"),),
                lead=("Temperature",),
                caption=("Conditions", "Rain", "Range"),
                link=("Forecast at weather.gov", _forecast_url(configuration.weather_point)),
                icon_from="Conditions",
            )
        )
    return tuple(specs)


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


@transaction.atomic
def select_dashboard_machine(key: str, *, selected: bool, principal: Principal) -> dict[str, Any]:
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = ManagedResource.objects.filter(key=key, kind="machine", enabled=True).first()
    if resource is None:
        raise ValueError("Dashboard telemetry requires an enabled machine record.")
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="dashboard.machine.select",
    ):
        if selected:
            last = DashboardMachine.objects.aggregate(value=Max("position"))["value"]
            DashboardMachine.objects.get_or_create(
                machine=resource,
                defaults={"position": (last + 1) if last is not None else 0},
            )
        else:
            DashboardMachine.objects.filter(machine=resource).delete()
    return {"ok": True, "machine": resource.key if selected else ""}


def _clean_point(value: str) -> str:
    point = value.strip()
    if not point:
        return ""
    parts = point.split(",")
    if len(parts) != 2:
        raise ValueError("Weather location must be latitude, longitude.")
    try:
        latitude, longitude = (float(part.strip()) for part in parts)
    except ValueError as exc:
        raise ValueError("Weather coordinates must be numbers.") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Weather coordinates are outside the valid range.")
    return f"{latitude:.4f},{longitude:.4f}"


@transaction.atomic
def save_dashboard_settings(
    *,
    weather_point: str,
    weather_label: str,
    infrastructure_label: str,
    principal: Principal,
) -> dict[str, Any]:
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="dashboard.settings.update",
    ):
        configuration, _ = DashboardConfiguration.objects.select_for_update().get_or_create(pk=1)
        configuration.weather_point = _clean_point(weather_point)
        configuration.weather_label = weather_label.strip()[:40] or "Weather"
        configuration.infrastructure_label = infrastructure_label.strip()[:40] or "Homelab"
        configuration.save(
            update_fields=(
                "weather_point",
                "weather_label",
                "infrastructure_label",
                "updated_at",
            )
        )
    return {"ok": True}


def _panel(
    spec: DashboardPanelSpec,
    *,
    payload: dict[str, Any],
    observed_at: Any,
    refreshing: bool,
    refreshable: bool,
) -> dict[str, Any]:
    """One panel as the glance template reads it, from its spec and reading."""

    payload = dict(payload)
    if spec.alert_metric and payload.get("metrics"):
        payload["metrics"] = [
            metric
            for metric in payload["metrics"]
            if not (
                str(metric.get("label", "")).strip().casefold() == spec.alert_metric.casefold()
                and str(metric.get("value", "")).strip() == "0"
            )
        ]
    shown = tuple(_glance_reading(metric, spec) for metric in payload.get("metrics", []))
    return {
        "id": spec.id,
        "label": spec.label,
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
        "readings": shown,
        **_head(shown, spec),
    }


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
        "alert": _alert_text(alert) if alert else "",
    }


def _alert_text(alert: dict[str, Any]) -> str:
    """The alert by name, or a count when the reading holds only a number."""

    value = str(alert["value"])
    return f"{value} {'alert' if value == '1' else 'alerts'}" if value.isdigit() else value


def dashboard_panels(
    configuration: DashboardConfiguration | None = None,
) -> tuple[dict[str, Any], ...]:
    configuration = configuration or dashboard_configuration()
    specs = {spec.id: spec for spec in panel_specs(configuration)}
    machine_resources = _dashboard_machines()
    routes = _machine_routes(machine_resources) if machine_resources else {}
    point = configuration.weather_point
    weather = WeatherObservation.objects.filter(point=point).first() if point else None
    telemetry = readings.stored_many(readings.machine_telemetry(resource.key) for resource in machine_resources)
    pending = set(DashboardRefreshRequest.objects.filter(completed_at__isnull=True).values_list("panel_id", flat=True))
    panels = []
    for resource in machine_resources:
        panel_id = f"machine-{resource.pk}"
        reading = telemetry.get(readings.machine_telemetry(resource.key))
        spec = replace(
            specs["infrastructure"],
            id=panel_id,
            label=(
                specs["infrastructure"].label
                if len(machine_resources) == 1
                else str(resource.spec.get("name") or resource.key)
            ),
            empty=(
                "Press Read now to read this machine."
                if resource.pk in routes
                else "HQ has no way to read this machine yet."
            ),
        )
        panels.append(
            _panel(
                spec,
                payload=getattr(reading, "value", None) or {},
                observed_at=getattr(reading, "observed_at", None),
                # A request nothing can answer is not a refresh in progress.
                refreshing=panel_id in pending and resource.pk in routes,
                refreshable=resource.pk in routes,
            )
        )
    # Nothing empty takes space: with no machine chosen there is no machine
    # reading to draw. The one exception is a dashboard that would otherwise
    # have no reading at all, where this one is the way in to choosing some.
    if not panels and "weather" not in specs:
        panels.append(
            _panel(
                specs["infrastructure"],
                payload={},
                observed_at=None,
                refreshing=False,
                refreshable=False,
            )
        )
    if "weather" in specs:
        panels.append(
            _panel(
                specs["weather"],
                payload=weather.payload if weather else {},
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


def glance_context(
    configuration: DashboardConfiguration | None = None,
    panels: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """What the glance template reads, for the dashboard and its own endpoint."""

    configuration = configuration or dashboard_configuration()
    panels = dashboard_panels(configuration) if panels is None else panels
    return {
        "dashboard_panels": panels,
        "dashboard_can_refresh": any(panel["refreshable"] for panel in panels),
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

    reported = metric.get("label", "")
    label = dict(spec.labels).get(reported, reported)
    short_label = dict(spec.short_labels).get(reported, label)
    return {
        **metric,
        "label": label,
        "short_label": short_label,
        "alert": bool(spec.alert_metric) and reported == spec.alert_metric,
    }


@transaction.atomic
def request_dashboard_refresh(*, principal: Principal) -> dict[str, Any]:
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    now = timezone.now()
    configuration = dashboard_configuration()
    machines = _dashboard_machines()
    routes = _machine_routes(machines) if machines else {}
    ids = [f"machine-{machine.pk}" for machine in machines if machine.pk in routes]
    if ids and configuration.weather_point:
        ids.append("weather")
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="dashboard.refresh.request",
    ):
        for panel_id in ids:
            DashboardRefreshRequest.objects.update_or_create(
                panel_id=panel_id,
                defaults={"requested_at": now, "completed_at": None},
            )
    if ids:
        transaction.on_commit(ring_doorbell)
    return {"ok": True, "requested": ids, "requested_at": now.isoformat()}


def request_stale_panel_refresh(panels: list[dict[str, Any]], *, principal: Principal) -> tuple[str, ...]:
    """Ask for the panels whose reading is due again.

    The dashboard posts this when it opens on a due reading. Only panels that
    are due, refreshable and not already waiting on a refresh are asked for,
    so repeat views of one card ask once.

    Silent for a principal who cannot ask: the page still renders.
    """

    wanted = tuple(
        str(panel["id"])
        for panel in panels
        if panel.get("due") and panel.get("refreshable") and not panel.get("refreshing")
    )
    if not wanted or not principal.permits(Capability.MANAGE_INFRASTRUCTURE):
        return ()

    now = timezone.now()
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="dashboard.refresh.stale",
    ):
        for panel_id in wanted:
            DashboardRefreshRequest.objects.update_or_create(
                panel_id=panel_id,
                defaults={"requested_at": now, "completed_at": None},
            )
    transaction.on_commit(ring_doorbell)
    return wanted


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
    }


def _clean_metric(metric: Any) -> dict[str, str]:
    if not isinstance(metric, dict):
        raise ValueError("Dashboard metrics must be objects.")
    label = str(metric.get("label", "")).strip()[:40]
    value = str(metric.get("value", "")).strip()[:80]
    detail = str(metric.get("detail", "")).strip()[:120]
    if not label or not value:
        raise ValueError("Dashboard metrics require a label and value.")
    return {"label": label, "value": value, "detail": detail}


_HOUR_FIELDS = ("time", "temperature", "forecast", "precipitation")


def _clean_hour(hour: Any) -> dict[str, str]:
    """One forecast hour, holding only the four fields the dashboard shows."""

    if not isinstance(hour, dict) or set(hour) - set(_HOUR_FIELDS):
        raise ValueError("A forecast hour holds only time, temperature, forecast and precipitation.")
    return {field: str(hour.get(field, "")).strip()[:40] for field in _HOUR_FIELDS}


def _refresh_is_pending(panel_id: str) -> bool:
    pending = DashboardRefreshRequest.objects.filter(completed_at__isnull=True)
    lookup = {"panel_id__startswith": "machine-"} if panel_id == "infrastructure" else {"panel_id": panel_id}
    return pending.filter(**lookup).exists()


def _record_machine_readings(item: dict[str, Any], *, controller_id: str, observed_at: Any) -> None:
    expected = {target["key"] for target in dashboard_refresh_plan(controller_id)["targets"]["infrastructure"]}
    matched = False
    for machine_reading in item.get("machines") or []:
        key = str(machine_reading.get("key", "")).strip()
        if key not in expected:
            continue
        resource = ManagedResource.objects.filter(kind="machine", key=key, enabled=True).first()
        if resource is None:
            continue
        matched = True
        reading_key = readings.machine_telemetry(resource.key)
        previous = readings.stored(reading_key)
        telemetry, since = _settle(
            _clean_panel(machine_reading),
            previous=previous.value if previous else None,
            previous_at=previous.observed_at if previous else None,
            now=observed_at,
        )
        telemetry["controller_id"] = controller_id
        readings.record(reading_key, telemetry, observed_at=since)
        DashboardRefreshRequest.objects.filter(panel_id=f"machine-{resource.pk}").update(completed_at=observed_at)
    if not matched:
        raise ValueError("No machine HQ knows matched the controller's report.")


def _record_weather_reading(item: dict[str, Any], *, configuration: DashboardConfiguration, observed_at: Any) -> None:
    point = str(item.get("point", "")).strip()
    if not point or point != configuration.weather_point:
        raise ValueError("Weather was reported without a configured point.")
    stored = WeatherObservation.objects.filter(point=point).first()
    payload, since = _settle(
        _clean_panel(item),
        previous=stored.payload if stored else None,
        previous_at=stored.observed_at if stored else None,
        now=observed_at,
    )
    WeatherObservation.objects.update_or_create(point=point, defaults={"payload": payload, "observed_at": since})
    DashboardRefreshRequest.objects.filter(panel_id="weather").update(completed_at=observed_at)


@transaction.atomic
def record_dashboard_observations(
    payload: list[dict[str, Any]], *, principal: Principal, controller_id: str
) -> dict[str, Any]:
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
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
        if panel_id == "infrastructure":
            _record_machine_readings(item, controller_id=controller_id, observed_at=now)
        else:
            _record_weather_reading(item, configuration=configuration, observed_at=now)
        stored.append(panel_id)
    return {"ok": True, "recorded": stored, "observed_at": now.isoformat()}


def _clean_panel(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status", "neutral")).strip()
    if status not in {"good", "attention", "serious", "neutral"}:
        raise ValueError(f"Unknown dashboard status {status!r}.")
    cleaned = {
        "status": status,
        "summary": str(item.get("summary", "")).strip()[:200],
        "metrics": [_clean_metric(metric) for metric in item.get("metrics", [])][:8],
    }
    hours = [_clean_hour(hour) for hour in item.get("hours") or []][:12]
    if hours:
        cleaned["hours"] = hours
    # The type of error, and nothing else a controller might say about it.
    failed = re.sub(r"[^A-Za-z0-9_]", "", str(item.get("refresh_failed") or ""))[:60]
    if failed:
        cleaned["refresh_failed"] = failed
    return cleaned


def _settle(
    reading: dict[str, Any], *, previous: dict[str, Any] | None, previous_at: Any, now: Any
) -> tuple[dict[str, Any], Any]:
    """What a panel should hold after a refresh, and since when.

    A failed refresh never replaces a reading that worked. The last good reading
    keeps its own time, so the panel ages toward "Out of date" on its own, and
    the failure is carried as a note. Only a panel with nothing to show reports
    the failure itself.
    """

    if reading.get("refresh_failed") and previous and previous.get("metrics"):
        kept = {key: value for key, value in previous.items() if key != "refresh_failed"}
        return {**kept, "refresh_failed": reading["refresh_failed"]}, previous_at
    return reading, now
