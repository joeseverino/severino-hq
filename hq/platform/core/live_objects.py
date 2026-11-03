"""Every live object of a class, found without changing what is live.

The walk asks each object's ``type()``, never ``isinstance``: Django's lazy
objects (``staticfiles_storage``, a request's ``user``) answer ``isinstance``
through ``__class__``, which builds what they wrap, so a walk that asked it
would make a storage, connect its receivers or query the database as it
looked. A reader of what a process holds (the registries a boot left, a
stalled test's greenlets) reads it here, the one place ``gc.get_objects`` is
named (the ``live-objects-walked-in-one-place`` rule).
"""

import gc


def live[T](cls: type[T]) -> list[T]:
    """Each object the collector tracks whose own type is ``cls`` or a subclass of it."""

    return [thing for thing in gc.get_objects() if issubclass(type(thing), cls)]
