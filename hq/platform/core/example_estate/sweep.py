"""The estate's controller's sweeps, each reported as the machine it runs on."""

from functools import partial

from hq.platform.application import inventory_testing

from .names import CONTROLLER

store = partial(inventory_testing.store, controller_id=CONTROLLER)
