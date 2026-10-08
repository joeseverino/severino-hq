"""The connections page and asking the controller to read now."""


from typing import ClassVar, override

from django.views import View
from django.views.generic import TemplateView

from hq.platform.application.action_links import READ_NOW_CAPABILITY, read_now_payload
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.connection_context import connections_context
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal


class ConnectionListView(PageMixin, TemplateView):
    """What HQ can reach, as the controllers last found it.

    Read-only by construction. Every row here started as a 1Password item, and
    the only way to change one is to change that item, so this page reports
    and never edits, which is what keeps it from becoming a second inventory.
    """

    template_name = "control_plane/connection_list.html"
    page_title = "Connections"

    def _connections(self):
        if not hasattr(self, "_context"):
            self._context = connections_context(
                principal=web_principal(self.request.user), request=self.request
            )
        return self._context

    @override
    def get_page_actions(self):
        # Beside the title with every other page's controls, not on a line of
        # their own halfway down the summary.
        inspect = reverse("connection")
        actions = [PageAction("How I am connected", inspect, modal="connection")]
        read_all = self._connections().read_all
        if read_all:
            from hq.platform.application.asks import Ask

            actions.append(Ask(read_all.label, read_all.url, title=read_all.reason))
        return tuple(actions)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["connections"] = self._connections()
        return context


class ReadNowView(View):
    """Ask the controller to read one connection, one kind, or everything now.

    POST only; the subject comes from the form or the action's URL. The
    request answers once the ask is stored: the controller reads on its next
    pull, and ``ReadStatusView`` says how that stands.
    """

    http_method_names: ClassVar[list[str]] = ["post"]

    def post(self, request):
        from django.utils import timezone

        from hq.platform.application.asks import FAILED, Standing, answer, read_standing, read_status_url
        from hq.platform.application.cadence import forced_kinds

        back = reverse("control_plane:connections")
        payload = read_now_payload({**request.GET.dict(), **request.POST.dict()})
        asked = timezone.now()
        result = execute_capability(READ_NOW_CAPABILITY, payload, principal=web_principal(request.user))
        if not result.get("ok"):
            return answer(request, Standing(FAILED, result["error"]["message"]), fallback=back)
        if not result.get("read_now"):
            # A bare wake-up names nothing to wait for.
            return answer(request, Standing(note=str(result.get("message") or "")), fallback=back)
        ref = str(payload.get("connection_ref", ""))
        kinds = forced_kinds(ref, str(payload.get("kind", ""))) or ()
        return answer(
            request,
            read_standing(kinds, asked, connection_ref=ref),
            fallback=back,
            status_url=read_status_url(kinds, asked, connection_ref=ref),
            message=str(result.get("message") or ""),
        )


class ReadStatusView(View):
    """How a read the controller was asked for stands, for a page following it.

    Reports only on what HQ handed that page to watch, signed, and asks nothing
    of anyone: it reads when each kind was last stored.
    """

    http_method_names: ClassVar[list[str]] = ["get"]

    def get(self, request):
        from django.http import Http404, JsonResponse

        from hq.platform.application.asks import WATCH_LIMIT, read_standing, watched
        from hq.platform.application.timestamps import moment

        token = str(request.GET.get("watch", ""))
        found = watched(token) if 0 < len(token) <= WATCH_LIMIT else None
        asked = moment(found.get("asked", "")) if found else None
        if found is None or asked is None or not isinstance(found.get("read"), list):
            raise Http404("Nothing to watch.")
        standing = read_standing(
            tuple(str(kind) for kind in found["read"]),
            asked,
            connection_ref=str(found.get("ref", "")),
        )
        return JsonResponse(standing.as_json())
