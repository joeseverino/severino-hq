"""Substituted values for a domain that can be shown to somebody else.

A domain calls a substitution on each value of its own worth hiding
(``amount(record.total, key=record.id)``), unconditionally: with no demo asked
for it returns the real value, so there is no second code path. Whether
something is connected, failing or waiting is a fact, never substituted.
"""

# showing_demo is for the rare case that has to branch, a chart that would plot
# a real series; a branch is a place the real value can escape through.
from hq.platform.application.demo import amount, day, label, showing_demo

__all__ = ["amount", "day", "label", "showing_demo"]
