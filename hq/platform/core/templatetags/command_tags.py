"""The command: text to paste somewhere else, with its copy button and what
to know about it."""

from django import template

from hq.platform.core.template_parts import Option, Part, slotted

register = template.Library()


slotted(
    Part(
        name="command",
        example='{% command text="example-cli status" %}',
        classes=("command", "command-code", "operator-command"),
        partial="partials/_command.html",
        about=(
            "Text to paste somewhere else, with the button that copies it: given `text=` a command, one tag "
            "with no end; else a block of code, its `title` part over it, the `detail` part's words under it, "
            "then its `notes` part's `<li>`s, which stand alone when there is no code."
        ),
        parts=("content", "title", "detail", "notes"),
        options={
            "text": Option(about="A command, which makes it one tag with no end."),
            "id": Option(about="The anchor a link to it names."),
        },
        bare_with="text",
    ),
    library=register,
)
