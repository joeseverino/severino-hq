"""The widget's one write: create, or update by slug."""

from dataclasses import dataclass
from typing import Annotated, Any

from hq.platform.application.command_fields import Describe
from hq.platform.application.domains import records_of
from hq.platform.application.projection import page_of
from hq.platform.application.security import Principal

from .models import Widget


@dataclass(frozen=True)
class WidgetCommand:
    slug: Annotated[str, Describe("The widget's address.")]
    name: Annotated[str, Describe("What the widget is called.")]


def save_widget(
    command: WidgetCommand,
    *,
    principal: Principal,
    current_slug: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(records_of("widgets").write)
    widget = Widget() if current_slug is None else Widget.objects.get(slug=current_slug)
    widget.slug, widget.name = command.slug, command.name
    widget.save()
    return {"ok": True, "widget": {"slug": widget.slug, "name": widget.name}}


def list_widgets(*, limit: int = 50) -> dict[str, Any]:
    return page_of(Widget.objects.all(), limit, order=("slug",), each=lambda w: {"slug": w.slug, "name": w.name})
