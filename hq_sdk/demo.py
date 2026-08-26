"""Substituted values for a domain that can be shown to somebody else.

The host owns the switch and knows nothing about who reads it. A domain decides
what of its own is a number worth hiding and calls the substitution on it; when
nobody asked for a demo every one of these returns the real value, so the call
is unconditional and there is no second code path to keep correct.

    from hq_sdk.demo import amount, label, showing_demo

    total = amount(record.total, key=record.id)
    title = label(record.title, key=record.id)

``showing_demo`` is for the rare case that has to branch -- a chart that would
otherwise plot a real series, say. Prefer the substitutions: a branch is a place
the real value can escape through.

What must not be substituted is whether something is connected, failing, or
waiting on the operator. Those are facts about the estate rather than data in
it, and a demo that shows a healthy board over a broken one is the failure this
exists to avoid.
"""

from application.demo import amount, day, label, showing_demo

__all__ = ["amount", "day", "label", "showing_demo"]
