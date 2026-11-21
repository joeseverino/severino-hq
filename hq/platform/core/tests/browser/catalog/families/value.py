"""The catalog's values: a value a template prints bare, as the engine holds it (held_values)."""

from hq.platform.core.template_parts import Example

# Each place the entry prints its name that the engine held otherwise than it
# promises: as a token in text, its mark inside the token's box, and as the
# name alone in an attribute, and a day in a `<time>` as plain text.
_HELD = """(name) => {
  const wrong = [];
  const said = document.querySelector('main [data-said]');
  if (said.getAttribute('title') !== name) wrong.push(`the title attribute reads "${said.getAttribute('title')}"`);
  if (!said.querySelector('.token .whole')) wrong.push('the name in a sentence is not held');
  if (!said.querySelector('.token .mark')) wrong.push("the sentence's mark is not inside the name's box");
  if (!document.querySelector('main [data-alone] .token[data-sole]')) wrong.push('the name alone is not held');
  if (document.querySelector('main time *')) wrong.push('the day in a time is not plain text');
  return wrong;
}"""
_NAME = "example-docker-host-with-a-long-name-01.example.com"

ENTRIES = {
    # One name printed bare in each place a template prints a value: in a
    # sentence with its mark after it, alone and in an attribute; and a day in
    # an element whose text stays plain.
    "value/held": Example(
        '{% section "Example host" %}<p data-said title="{{ name }}">Runs on {{ name }}.</p>'
        "<p data-alone>{{ name }}</p><p><time>{{ day }}</time></p>{% endsection %}",
        {"name": _NAME, "day": "2026-11-21"},
        premise=(_HELD, _NAME),
    ),
}
