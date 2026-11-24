"""Text a person wrote outside HQ, typed so every page shows it as written."""


class Quoted(str):
    """Text a person wrote outside HQ, read where HQ reads it (a playbook's
    title and statements, an input's description, a scan's problems): the
    author's own words, shown as written however they read, and escaped on
    the page like any text. Joined only with more of them it is wrapped
    again; beside HQ's own words it is a field or item of its own, never
    one str with them.
    """

    __slots__ = ()
