"""The widget's one write: create, or update by slug."""

from dataclasses import dataclass
from typing import Any

from hq.platform.application.domains import records_of
from hq.platform.application.security import Principal

from .models import Widget


@dataclass(frozen=True)
class WidgetCommand:
    slug: str
    name: str


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
    items = [{"slug": w.slug, "name": w.name} for w in Widget.objects.order_by("slug")[:limit]]
    return {"items": items, "count": len(items)}
