"""The dashboard glance's refresh queue: who asks for a reading, the plan the
glance pass reads, and the report it answers with."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError

from hq.domains.control_plane.glance_shapes import GlanceObservation, Machine, Weather
from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
)
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.glance import MACHINE_KIND, WEATHER_KIND
lazy from hq.domains.control_plane.bridge_actions import Admitted, admission
lazy from hq.platform.application.routes import reverse

from .asks import QUEUED, REFRESHING, Ask, Asked, AskedAnswer, Standing
from .cadence import ask
from .command_fields import Describe
from .glance import (
    NWS_API,
    chosen_machines,
    dashboard_configuration,
    dashboard_machines,
    dashboard_panels,
    machine_routes,
    panel_specs,
    stored_reading,
)
from .moments import moment
from .security import Principal
lazy from .capabilities import capability_words, command_spec
lazy from .facts import kept_by, record_read_at
lazy from .inventory import keep
lazy from .projection import iso

DASHBOARD_REFRESH = "dashboard.panels.refresh"
DASHBOARD_REFRESH_STALE = "dashboard.panels.refresh_due"


# The strip, which a finished ask fetches again (_dashboard_glance.html).
GLANCE_REGION = "[data-glance]"


def refresh_ask(standing: Standing, panel: str) -> Ask:
    """A panel's Refresh: an ask for every panel, standing as this one's reading does."""

    return Ask(
        capability_words(DASHBOARD_REFRESH),
        reverse("dashboard_glance"),
        standing=standing,
        follows="dashboard",
        refresh=GLANCE_REGION,
        outcome=True,
        of=panel,
    )


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


def _ask_for(ids: list[str], *, why: str, message: str = "", pressed: bool = False) -> PanelsRefreshRequested:
    """Queue a reading of each panel in ``ids``, recorded once; ``pressed``
    when a person asked and waits on it."""

    if ids:
        # Each panel by the name its head shows.
        ask(
            panels={
                panel_id: f"Asked for {why}: {name}" for panel_id, name in zip(ids, _panel_names(ids), strict=True)
            },
            doorbell="glance",
            activity=pressed,
        )
    standing = Standing(QUEUED, REFRESHING) if ids else Standing()
    return {**Asked(standing, message=message).answer(), "requested": ids}


@transaction.atomic
def request_dashboard_refresh(_command: DashboardRefreshCommand, *, principal: Principal) -> PanelsRefreshRequested:
    """Ask for a fresh reading of every panel a controller can read."""

    del principal
    configuration = dashboard_configuration()
    machines = chosen_machines()
    routes = machine_routes(machines) if machines else {}
    ids = [f"machine-{machine.pk}" for machine in machines if machine.pk in routes]
    if ids and configuration.weather_point:
        ids.append("weather")
    return _ask_for(
        ids, why="a fresh reading of the dashboard", message=command_spec(DASHBOARD_REFRESH).requested(), pressed=True
    )


@transaction.atomic
def request_stale_panel_refresh(_command: DashboardRefreshCommand, *, principal: Principal) -> PanelsRefreshRequested:
    """Ask for the panels whose reading is due again.

    The dashboard posts this when it opens on a due reading. Only panels that
    are due, refreshable and not already waiting on a refresh are asked for,
    so repeat views of one card ask once; one whose ask went unanswered is
    asked again, so a refresh that never reached a reader is not the last.
    """

    del principal
    wanted = [
        str(panel["id"])
        for panel in dashboard_panels()
        if panel["due"] and panel["refreshable"] and (not panel["pending"] or panel["unanswered"])
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
    targets = chosen_machines()
    routes = machine_routes(targets) if targets else {}
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
        "as_of": iso(timezone.now()),
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
        _keep(
            MACHINE_KIND,
            "key",
            machine,
            worked=lambda kept: kept.host is not None or kept.docker is not None,
            now=observed_at,
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
    _keep(WEATHER_KIND, "point", weather, worked=lambda kept: bool(kept.hours), now=observed_at)
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
    return {"ok": True, "recorded": stored, "observed_at": iso(now)}


def _keep[Shape: (Machine, Weather)](
    kind: str, key: str, reading: Shape, *, worked: Callable[[Shape], bool], now: Any
) -> None:
    """Store a panel's reading, as the controller reported it.

    A failed refresh never replaces a reading that worked. The last good reading
    keeps its own time, so the panel ages toward "Out of date" on its own, and
    the failure is carried as a note. Only a panel with nothing to show stores
    the failure itself.
    """

    failed = reading.refresh_failed
    previous = kept_by(kind, key).get(getattr(reading, key))
    last = stored_reading(OBSERVATIONS[kind].record, previous)
    if failed and previous is not None and last is not None and worked(last):
        carried = {**previous, "refresh_failed": failed}
        keep(kind, key, carried, read_at=record_read_at(previous) or now, failed=failed)
        return
    keep(kind, key, reading.model_dump(mode="json", exclude_defaults=True), read_at=now, failed=failed)
