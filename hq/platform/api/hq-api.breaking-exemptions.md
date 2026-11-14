# Client breaks the machine API admits

lint:openapi-breaking fails a change to hq-api.openapi.json that breaks a
client, unless a line this change adds here names it. oasdiff reads the added
lines as its `--err-ignore` list, so a line already on the base admits
nothing, and a change may delete it. An entry is a list item: the line the
gate printed for the change, then `Reason:` and why clients can take the
break. A line oasdiff could match that gives no reason fails the gate.
