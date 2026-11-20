"""Each shared part's examples, which its ``Part`` names as its ``example``
and ``states`` where they would crowd the module it is declared in. The
component catalog draws each as an entry of its own (`docs/Development.md`).

An example's context is built only as the catalog draws it, so loading a
part's examples builds nothing of HQ's records."""

from django.utils import timezone

lazy from hq.platform.application.drawings import Dot, Dots

# The moment every example is drawn at, and its times are told from, so a
# time a part counts from now draws the same between two renders.
DRAWN_AT = timezone.now()

# A narrow section's body: a few facts, as the connection panel's identity draws them.
NARROW = (
    '{% facts %}{% fact "Example name" value="example-01" %}{% fact "Example state" value="Online" %}{% endfacts %}'
)


def runs():
    """The last six runs of a piece of work, oldest first, each leading to its run."""

    return Dots(
        tuple(
            Dot(filled=done, tip=f"Example run {index}: {'done' if done else 'failed'}", url=f"/example/runs/{index}/")
            for index, done in enumerate((True, True, False, True, False, True), 1)
        ),
        ordered=True,
    )
