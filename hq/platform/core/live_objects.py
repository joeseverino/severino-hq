"""Every live object of a class, found without changing what is live.

The walk asks ``type()``, never ``isinstance``, which a Django lazy object
answers by building what it wraps. The one place ``gc.get_objects`` is named
(the ``live-objects-walked-in-one-place`` rule).
"""

import gc


def live[T](cls: type[T]) -> list[T]:
    """Each object the collector tracks whose own type is ``cls`` or a subclass of it."""

    return [thing for thing in gc.get_objects() if issubclass(type(thing), cls)]
