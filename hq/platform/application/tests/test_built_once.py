"""What a process builds on first use is built once, however many threads ask at once.

Each race is forced: the first thread builds, and does not finish until the
second has either reached the lock that should hold it or begun building too.
"""

import threading
from collections.abc import Callable
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from hq.platform.application import composed_providers, plugins

# Only a bound on a thread that never arrives; nothing waits it out.
BOUND = 30.0


class _Race:
    """Two threads asking at once, the second arriving while the first builds."""

    def __init__(self) -> None:
        self.second: threading.Thread | None = None
        self.building = threading.Event()
        self.arrived = threading.Event()

    def _arrive(self) -> None:
        if threading.current_thread() is self.second:
            self.arrived.set()

    def at(self, lock: Any) -> Any:
        """``lock``, saying when the second thread reaches it."""

        race = self

        class Arriving:
            def __enter__(self) -> Any:
                race._arrive()
                return lock.__enter__()

            def __exit__(self, *exc: Any) -> Any:
                return lock.__exit__(*exc)

        return Arriving()

    def building_with[T](self, build: Callable[..., T]) -> Callable[..., T]:
        """``build``, which the first thread finishes only once the second has arrived."""

        def built(*args: Any, **kwargs: Any) -> T:
            if threading.current_thread() is self.second:
                self._arrive()
            else:
                self.building.set()
                if not self.arrived.wait(BOUND):
                    raise AssertionError("The second thread never arrived.")
            return build(*args, **kwargs)

        return built

    def run(self, ask: Callable[[], Any]) -> tuple[list[Any], list[BaseException]]:
        answers: list[Any] = [None, None]
        errors: list[BaseException] = []

        def asking(index: int) -> None:
            try:
                answers[index] = ask()
            except Exception as error:  # noqa: BLE001 - what the thread raised is the finding
                errors.append(error)

        first = threading.Thread(target=asking, args=(0,))
        first.start()
        if not self.building.wait(BOUND):
            raise AssertionError("The first thread never began building.")
        self.second = threading.Thread(target=asking, args=(1,))
        self.second.start()
        first.join(BOUND)
        self.second.join(BOUND)
        return answers, errors


class BuiltOnceTests(SimpleTestCase):
    def test_a_new_provider_is_declared_once(self):
        race = _Race()
        lock = race.at(threading.Lock())
        derivation = race.building_with(composed_providers.derivation)

        with (
            mock.patch.object(composed_providers, "_DECLARING", lock, create=True),
            mock.patch.object(composed_providers, "derivation", derivation),
        ):
            answers, errors = race.run(
                lambda: composed_providers.provided("extension.example.at-once.attention", tuple)
            )

        self.assertEqual(errors, [])
        self.assertIs(answers[0], answers[1])

    def test_the_composition_is_admitted_once(self):
        race = _Race()

        with (
            mock.patch.object(plugins, "_COMPOSED", []),
            mock.patch.object(plugins, "_COMPOSING", race.at(threading.Lock()), create=True),
            mock.patch.object(plugins, "_admitted", race.building_with(tuple)),
        ):
            answers, errors = race.run(plugins.composition)

        self.assertEqual(errors, [])
        self.assertIs(answers[0], answers[1])
