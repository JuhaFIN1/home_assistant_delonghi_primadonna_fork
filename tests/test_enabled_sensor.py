"""Regressions for the enabled binary sensor."""

import asyncio
import os
import sys

sys.path.append(
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
        )
    )
)

from delonghi_primadonna.binary_sensor import \
    DelongiPrimadonnaEnabledSensor  # noqa: E402
from delonghi_primadonna.device import DelongiPrimadonna  # noqa: E402

CONFIG = {
    "mac": "00:11:22:33:44:55",
    "model": "TEST",
    "name": "TEST",
}


def make_sensor():
    device = DelongiPrimadonna(CONFIG, None)
    return DelongiPrimadonnaEnabledSensor(device, None), device


async def test_does_not_write_state_into_the_device():
    """The sensor must not tell the device what state it is in.

    It restored the remembered value straight into
    device.switches.is_on, so after a restart the device claimed a state
    no BLE frame had reported - and every other entity reading that flag
    inherited the guess.
    """
    sensor, _ = make_sensor()

    own = type(sensor).__dict__
    assert "async_added_to_hass" not in own, (
        "still writes a restored value back into the device"
    )


async def test_is_not_a_restore_entity():
    """Nothing of its own to restore: is_on reads the machine state."""
    sensor, _ = make_sensor()

    names = [c.__name__ for c in type(sensor).__mro__]
    assert "RestoreEntity" not in names


async def test_state_follows_the_device():
    sensor, device = make_sensor()

    device.switches.is_on = True
    assert sensor.is_on is True
    device.switches.is_on = False
    assert sensor.is_on is False


async def run_tests():
    await test_does_not_write_state_into_the_device()
    await test_is_not_a_restore_entity()
    await test_state_follows_the_device()
    print("[SUCCESS] Enabled sensor verified.")


if __name__ == "__main__":
    asyncio.run(run_tests())
