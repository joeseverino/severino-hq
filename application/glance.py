"""Typed, cached dashboard observations and their explicit refresh queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.audit import operation_context
from control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
    WeatherObservation,
)

from .cadence import ring_doorbell
from .machines import machine_catalog
from .security import Capability, Principal


def connection_specs():
    """Emit the keyless NWS boundary without spending a discovery query."""

    from django.urls import reverse

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
                detail="Public forecasts and alerts; configured points refresh through HQ.",
                endpoint="https://api.weather.gov",
                ability_names=("nws.hourly_forecast", "nws.active_alerts"),
                targets=(ConnectionLink("Dashboard weather", reverse("dashboard")),),
            ),
        )

    return (
        ConnectionSpec(
            name="hq.nws",
            label="National Weather Service",
            summary="Keyless hourly forecasts and active alerts for the configured point.",
            required_capability=Capability.READ,
            instance_provider=instances,
            abilities=(
                ConnectionAbility(
                    "nws.hourly_forecast",
                    "Hourly forecast",
                    "Refresh the current conditions and hourly forecast.",
                    effect="infrastructure_change",
                    capability="infrastructure.controller.refresh",
                ),
                ConnectionAbility(
                    "nws.active_alerts",
                    "Active weather alerts",
                    "Refresh active National Weather Service alerts for this point.",
                    effect="infrastructure_change",
                    capability="infrastructure.controller.refresh",
                ),
            ),
            web_route="dashboard",
            management_route="dashboard_glance_settings",
            documentation_url="https://www.weather.gov/documentation/services-web-api",
        ),
    )


@dataclass(frozen=True)
class DashboardPanelSpec:
    id: str
    label: str
    empty: str


def dashboard_configuration() -> DashboardConfiguration:
    return (
        DashboardConfiguration.objects.filter(pk=1).first() or DashboardConfiguration()
    )


def panel_specs(
    configuration: DashboardConfiguration | None = None,
) -> tuple[DashboardPanelSpec, ...]:
    configuration = configuration or dashboard_configuration()
    specs = [DashboardPanelSpec("infrastructure", "Machines", "Refresh to read them.")]
    if configuration.weather_point:
        specs.append(
            DashboardPanelSpec(
                "weather",
                configuration.weather_label,
                "Refresh to read the National Weather Service.",
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
    catalog = {item.name.lower(): item for item in machine_catalog()}
    controller_ids = set(
        ProviderConnection.objects.filter(reachable=True).values_list(
            "controller_id", flat=True
        )
    )
    routes = {}
    for resource in resources:
        name = str(resource.spec.get("name") or resource.key).lower()
        found = catalog.get(name)
        reached_by = tuple(found.reached_by) if found else ()
        if reached_by or resource.key in controller_ids:
            routes[resource.pk] = reached_by
    return routes


def dashboard_machine_selected(key: str) -> bool:
    return DashboardMachine.objects.filter(
        machine__key=key, machine__kind="machine", machine__enabled=True
    ).exists()


@transaction.atomic
def select_dashboard_machine(
    key: str, *, selected: bool, principal: Principal
) -> dict[str, Any]:
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    resource = ManagedResource.objects.filter(
        key=key, kind="machine", enabled=True
    ).first()
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
        configuration, _ = (
            DashboardConfiguration.objects.select_for_update().get_or_create(pk=1)
        )
        configuration.weather_point = _clean_point(weather_point)
        configuration.weather_label = weather_label.strip()[:40] or "Weather"
        configuration.infrastructure_label = (
            infrastructure_label.strip()[:40] or "Homelab"
        )
        configuration.save(
            update_fields=(
                "weather_point",
                "weather_label",
                "infrastructure_label",
                "updated_at",
            )
        )
    return {"ok": True}


def dashboard_panels(
    configuration: DashboardConfiguration | None = None,
) -> tuple[dict[str, Any], ...]:
    configuration = configuration or dashboard_configuration()
    machine_resources = _dashboard_machines()
    routes = _machine_routes(machine_resources) if machine_resources else {}
    point = configuration.weather_point
    weather = WeatherObservation.objects.filter(point=point).first() if point else None
    pending = set(
        DashboardRefreshRequest.objects.filter(completed_at__isnull=True).values_list(
            "panel_id", flat=True
        )
    )
    panels = [
        {
            "id": f"machine-{resource.pk}",
            "label": (
                configuration.infrastructure_label
                if len(machine_resources) == 1
                else str(resource.spec.get("name") or resource.key)
            ),
            "empty": (
                "Refresh to read this machine."
                if resource.pk in routes
                else "No controller connection reaches this machine yet."
            ),
            "payload": (resource.status or {}).get("telemetry") or {},
            "observed_at": resource.last_observed_at,
            "refreshing": f"machine-{resource.pk}" in pending,
            "refreshable": resource.pk in routes,
        }
        for resource in machine_resources
    ]
    if not panels:
        panels.append(
            {
                "id": "infrastructure",
                "label": configuration.infrastructure_label,
                "empty": "Choose “Show on dashboard” in a machine's settings.",
                "payload": {},
                "observed_at": None,
                "refreshing": False,
                "refreshable": False,
            }
        )
    if configuration.weather_point:
        weather_payload = dict(weather.payload) if weather else {}
        if weather_payload.get("metrics"):
            weather_payload["metrics"] = [
                metric
                for metric in weather_payload["metrics"]
                if not (
                    str(metric.get("label", "")).strip().casefold() == "alerts"
                    and str(metric.get("value", "")).strip() == "0"
                )
            ]
        panels.append(
            {
                "id": "weather",
                "label": configuration.weather_label,
                "empty": "Refresh to read the National Weather Service.",
                "payload": weather_payload,
                "observed_at": weather.observed_at if weather else None,
                "refreshing": "weather" in pending,
                "refreshable": bool(machine_resources),
            }
        )
    return tuple(panels)


@transaction.atomic
def request_dashboard_refresh(*, principal: Principal) -> dict[str, Any]:
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    now = timezone.now()
    configuration = dashboard_configuration()
    machines = _dashboard_machines()
    routes = _machine_routes(machines) if machines else {}
    ids = [f"machine-{machine.pk}" for machine in machines if machine.pk in routes]
    if ids:
        if configuration.weather_point:
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
            "weather": {"point": configuration.weather_point},
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


def _refresh_is_pending(panel_id: str) -> bool:
    pending = DashboardRefreshRequest.objects.filter(completed_at__isnull=True)
    lookup = {"panel_id__startswith": "machine-"} if panel_id == "infrastructure" else {"panel_id": panel_id}
    return pending.filter(**lookup).exists()


def _record_machine_readings(
    item: dict[str, Any], *, controller_id: str, observed_at: Any
) -> None:
    expected = {
        target["key"]
        for target in dashboard_refresh_plan(controller_id)["targets"][
            "infrastructure"
        ]
    }
    matched = False
    for machine_reading in item.get("machines") or []:
        key = str(machine_reading.get("key", "")).strip()
        if key not in expected:
            continue
        resource = ManagedResource.objects.filter(
            kind="machine", key=key, enabled=True
        ).first()
        if resource is None:
            continue
        matched = True
        telemetry = _clean_panel(machine_reading)
        telemetry["controller_id"] = controller_id
        resource.status = {**(resource.status or {}), "telemetry": telemetry}
        resource.last_observed_at = observed_at
        resource.save(update_fields=("status", "last_observed_at", "updated_at"))
        DashboardRefreshRequest.objects.filter(
            panel_id=f"machine-{resource.pk}"
        ).update(completed_at=observed_at)
    if not matched:
        raise ValueError("No declared machine matched the controller report.")


def _record_weather_reading(
    item: dict[str, Any], *, configuration: DashboardConfiguration, observed_at: Any
) -> None:
    point = str(item.get("point", "")).strip()
    if not point or point != configuration.weather_point:
        raise ValueError("Weather was reported without a configured point.")
    WeatherObservation.objects.update_or_create(
        point=point,
        defaults={"payload": _clean_panel(item), "observed_at": observed_at},
    )
    DashboardRefreshRequest.objects.filter(panel_id="weather").update(
        completed_at=observed_at
    )


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
            _record_weather_reading(
                item, configuration=configuration, observed_at=now
            )
        stored.append(panel_id)
    return {"ok": True, "recorded": stored, "observed_at": now.isoformat()}


def _clean_panel(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status", "neutral")).strip()
    if status not in {"good", "attention", "serious", "neutral"}:
        raise ValueError(f"Unknown dashboard status {status!r}.")
    return {
        "status": status,
        "summary": str(item.get("summary", "")).strip()[:200],
        "metrics": [_clean_metric(metric) for metric in item.get("metrics", [])][:6],
    }
