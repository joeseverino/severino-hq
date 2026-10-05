"""Work an extension does outside the process: a network call, a provider.

HQ refuses any connection, subprocess or sleep made while a request is being
served, so a view or a capability handler cannot call out. An extension
declares the work instead, and HQ runs it:

    from hq_sdk.outbound import Failed, OutboundWork, ask

    def look_up(progress, *, subject, principal):
        progress("Asking the registry")
        found = registry.read(subject)          # the network call
        if found is None:
            raise Failed("The registry does not list it.")
        store(subject, found)
        progress(f"Read {len(found)} entries.", force=True)
        return {"seen": len(found)}

    def outbound():
        return (
            OutboundWork(
                "notes.lookup", "Look up", "Ask the registry about one note.",
                "notes.write", look_up, subject_label="Note",
            ),
        )

`PluginIntegration(outbound=outbound)` is the whole declaration. From it HQ
derives the job (one live at a time, recorded and audited), a capability named
`notes.lookup` for the API, MCP, the command centre and a command line, the
route the button posts to, and the status resource the button follows. The
page shows what is stored and puts the control beside it:

    context["lookup"] = ask("notes.lookup", note.slug)
    {% include "partials/_ask.html" with ask=lookup %}

The control answers at once, says how the work stands, and says why when it
failed, with no script, template, status endpoint or polling in the extension.
`ask` may also stand among a page's actions, wherever a `PageAction` does.

`run_now` does the same work to its end on the calling thread, for a
management command a timer runs. It is refused inside a request.

Nothing here lifts the rule. The work is allowed to reach out because HQ runs
it off the request, and only there.
"""

from hq.domains.jobs.runner import Failed
from hq.platform.application.outbound_work import OutboundWork, ask, run_now

__all__ = ["Failed", "OutboundWork", "ask", "run_now"]
