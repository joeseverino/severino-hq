"""The names HQ defines for something outside it to call, which vulture's reads cannot see.

audit:dead hands this file to vulture as a use of each name in it. What a
decorator registers or a naming convention reaches is [tool.vulture] in
pyproject.toml instead. Each entry says who calls it.
"""

from vulture.whitelist_utils import Whitelist

_ = Whitelist()

# html.parser.HTMLParser calls its subclass's handlers by name.
_.handle_data
_.handle_startendtag
# Python calls a module's __getattr__ for a name the module does not define.
_.__getattr__
