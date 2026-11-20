"""A template's comments, as the readers of its text skip them."""

import re

TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.DOTALL)
