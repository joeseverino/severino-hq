"""The dashboard's commands and the action queue it leads with."""

from pydantic import Field, create_model

from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND

from . import action_items, derived_reads, glance, glance_refresh, pins
from .appearance import SET_THEME, ThemeCommand, set_theme
from .contracts import Effect
from .demo import SET_DEMO, DemoCommand, set_demo, showing_demo
from .domains import queue_sources
from .estate_records import ActionItem, ActionItemRow
from .integration_specs import ON_ITS_PAGE, CapabilitySpec, ListRead, ResourceSpec, typed_keys
from .plugins import ATTENTION_ORDER
from .resources import TEXT_FILTER, BoundedQuery, one_of
from .security import Capability


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        # A person's own arrangement of HQ, kept by that person or an agent
        # they signed in (security.NEEDS_A_PERSON), and the dashboard's shared
        # settings: none of it reaches a spec or the controller.
        CapabilitySpec(
            pins.PIN,
            "Pin something so it sorts first for you, or unpin it.",
            Effect.REMOTE_WRITE,
            Capability.PIN_FAVORITES,
            pins.PinCommand,
            pins.pin,
            execution_notes=("Pin it last among your pins of its type, or unpin it.",),
            label="Pin or unpin an item",
            verb="Pin",
            # A domain's own head, where a pin is a favorite.
            variants=(("on", f"☆ Add to {pins.FAVORITES}"), ("off", f"★ {pins.FAVORITE}")),
            reached_from=ON_ITS_PAGE,
        ),
        CapabilitySpec(
            pins.MOVE,
            "Move one of your pins past its neighbor.",
            Effect.REMOTE_WRITE,
            Capability.PIN_FAVORITES,
            pins.MoveCommand,
            pins.move_pin,
            execution_notes=("Swap it with the pin above or below, leaving the rest in place.",),
            label="Move a pin",
            reached_from=ON_ITS_PAGE,
        ),
        CapabilitySpec(
            pins.CHOOSE,
            "Choose exactly which things of one type you pin.",
            Effect.REMOTE_WRITE,
            Capability.PIN_FAVORITES,
            pins.ChooseCommand,
            pins.choose,
            execution_notes=("Unpin what is not chosen and pin what is new, keeping the order of the rest.",),
            label="Choose pins",
            reached_from="dashboard",
        ),
        CapabilitySpec(
            action_items.SET_ASIDE,
            "Set action items aside for you until they get worse or change.",
            Effect.REMOTE_WRITE,
            Capability.SET_ASIDE_ACTION_ITEMS,
            action_items.SetAsideCommand,
            action_items.set_items_aside,
            execution_notes=(
                "Set each named row aside as it was drawn.",
                "Set each named item aside as the queue holds it now.",
            ),
            label="Set aside action items",
            verb="Dismiss",
            variants=((action_items.ALL, "Dismiss all"),),
            reached_from="action_items",
        ),
        CapabilitySpec(
            action_items.RESTORE,
            "Bring set-aside action items back into your queue.",
            Effect.REMOTE_WRITE,
            Capability.SET_ASIDE_ACTION_ITEMS,
            action_items.RestoreCommand,
            action_items.restore,
            label="Bring back action items",
            verb="Restore",
            reached_from="action_items",
        ),
        CapabilitySpec(
            glance.DASHBOARD_SETTINGS,
            "Set the dashboard's weather location and its panel names.",
            Effect.REMOTE_WRITE,
            Capability.SET_PREFERENCES,
            glance.DashboardSettingsCommand,
            glance.save_dashboard_settings,
            execution_notes=(
                "Refuse a weather location that is not a latitude and longitude in range.",
                "Name an unnamed panel by its default.",
            ),
            label="Save dashboard settings",
        ),
        # The dashboard everyone's pages share, arranged by whoever holds
        # set_preferences: an operator's, since a viewer holds only reads.
        CapabilitySpec(
            glance.DASHBOARD_MACHINE,
            "Show a machine's telemetry on the dashboard, or take it off.",
            Effect.REMOTE_WRITE,
            Capability.SET_PREFERENCES,
            glance.DashboardMachineCommand,
            glance.select_dashboard_machine,
            "key",
            "infrastructure",
            target_label="Machine",
            target_help="The machine whose telemetry to show.",
            target_query=(("kind", MACHINE_KIND),),
            execution_notes=("Refuse a record that is not an enabled machine.",),
            label="Show a machine on the dashboard",
            verb="Show on dashboard",
            wants="show {target} on the dashboard",
            variants=(("on", "Show on dashboard"), ("off", "Remove from dashboard")),
            state=glance.dashboard_machine_selected,
        ),
        CapabilitySpec(
            SET_THEME,
            "Choose whether your pages are light, dark, or follow the system.",
            Effect.REMOTE_WRITE,
            Capability.SET_PREFERENCES,
            ThemeCommand,
            set_theme,
            execution_notes=("Set the theme rather than cycle it, so asking twice changes nothing.",),
            label="Choose a theme",
        ),
        CapabilitySpec(
            SET_DEMO,
            "Show stand-in values in place of real ones in this browser, or the real ones again.",
            Effect.REMOTE_WRITE,
            Capability.SHOW_DEMO,
            DemoCommand,
            set_demo,
            execution_notes=("Set the mode rather than cycle it, so asking twice changes nothing.",),
            label="Show or hide demo data",
            variants=(("on", "Show demo data"), ("off", "Show real data")),
            state=showing_demo,
        ),
        CapabilitySpec(
            glance_refresh.DASHBOARD_REFRESH,
            "Ask for a fresh reading of every dashboard panel a controller can read.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            glance_refresh.DashboardRefreshCommand,
            glance_refresh.request_dashboard_refresh,
            execution_notes=("Ask only for panels a controller has a way to read.",),
            label="Refresh the dashboard",
            verb="Refresh",
        ),
        CapabilitySpec(
            glance_refresh.DASHBOARD_REFRESH_STALE,
            "Ask again for the dashboard panels whose reading is due.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            glance_refresh.DashboardRefreshCommand,
            glance_refresh.request_stale_panel_refresh,
            execution_notes=("Ask once for each panel that is due, readable and not already asked for.",),
            label="Refresh due readings",
        ),
    )


def action_item_query() -> type[BoundedQuery]:
    """The queue's narrowing: a status the queue holds, and a domain that raises
    items, as this composition declares them."""

    sources = queue_sources()
    return create_model(
        "ActionItemQuery",
        __base__=BoundedQuery,
        query=(str, Field(default="", max_length=200, description=TEXT_FILTER)),
        status=(
            one_of(ATTENTION_ORDER) | None,
            Field(default=None, description="Only the items this urgent; absent, every item."),
        ),
        source=(
            one_of(sources) | None,
            Field(default=None, description="Only the items from this section; absent, every section's."),
        ),
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "action_items",
            "Needs you",
            "The composed action queue: what needs doing, from every domain.",
            Capability.READ,
            listing=ListRead(
                derived_reads.list_action_items, action_item_query(), ActionItem, fields=typed_keys(ActionItemRow)
            ),
            web_route="action_items",
            pass_principal=True,
        ),
    )
