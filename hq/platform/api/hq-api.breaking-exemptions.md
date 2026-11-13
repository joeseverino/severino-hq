# Client breaks the machine API admits

lint:openapi-breaking fails a change to hq-api.openapi.json that breaks a
client, unless a line here names it. oasdiff reads this file as its
`--err-ignore` list. An entry is a list item: the line the gate printed for
the change, then `Reason:` and why clients can take the break. A line oasdiff
could match that gives no reason fails the gate. Once the change is on main,
its entry matches nothing and is deleted.
