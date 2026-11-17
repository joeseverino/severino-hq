"""The shared parts no tag draws, which a template includes by path, each
declared once as a ``Part``. A tag's part is declared where its tag is
registered; the template contract reads both (``template_contract.parts``)."""

from hq.platform.core.template_parts import Part

MODAL = Part(
    name="",
    partial="partials/_modal.html",
    about="A dialog: its title, what it is for, the body a template names, and its close.",
    classes=("modal", "modal-*", "modal-close", "modal-foot", "modal-head", "modal-lede", "modal-panel"),
)

PARTS = (
    MODAL,
    Part(
        name="",
        partial="partials/_demo_about.html",
        about="The About dialog's body on a saved copy of HQ: what it is, the pages to try, what built it.",
        draws=(MODAL,),
    ),
    Part(
        name="",
        partial="partials/_share_list.html",
        about="Analytics' ranked shares, each a bar against the largest.",
        classes=("bar-label", "bar-list", "bar-row", "bar-track", "bar-value"),
    ),
)
