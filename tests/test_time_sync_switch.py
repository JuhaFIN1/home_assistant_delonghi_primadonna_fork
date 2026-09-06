"""Regressions for the time sync switch."""

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

from delonghi_primadonna.device import DelongiPrimadonna  # noqa: E402
from delonghi_primadonna.switch import \
    DelongiPrimadonnaTimeSyncSwitch  # noqa: E402

CONFIG = {
    "mac": "00:11:22:33:44:55",
    "model": "TEST",
    "name": "TEST",
}


class FakeHass:
    """Records tasks instead of running them."""

    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro):
        self.tasks.append(coro)
        coro.close()
        return None


def make_switch():
    device = DelongiPrimadonna(CONFIG, None)
    hass = FakeHass()
    switch = DelongiPrimadonnaTimeSyncSwitch(device, hass)
    switch.hass = hass
    return switch, device, hass


async def test_turn_on_enables_synchronisation():
    """Turning the switch on must actually enable syncing.

    It only ever set the clock once and left sync_time False, so the
    switch could turn synchronisation off but never on.
    """
    switch, device, _ = make_switch()

    assert device.sync_time is False
    await switch.async_turn_on()

    assert device.sync_time is True
    assert switch.is_on is True


async def test_turn_off_disables_synchronisation():
    switch, device, _ = make_switch()
    await switch.async_turn_on()

    await switch.async_turn_off()

    assert device.sync_time is False
    assert switch.is_on is False


async def test_state_follows_the_device():
    """is_on reads the device rather than a private copy."""
    switch, device, _ = make_switch()

    device.sync_time = True
    assert switch.is_on is True
    device.sync_time = False
    assert switch.is_on is False


async def test_turn_on_sets_the_clock_once():
    switch, _, hass = make_switch()

    await switch.async_turn_on()

    assert len(hass.tasks) == 1


async def test_daily_resync_waits_a_day():
    switch, device, hass = make_switch()
    await switch.async_turn_on()
    hass.tasks.clear()

    await switch.async_update()
    assert hass.tasks == [], "re-synced again immediately after turning on"

    device.last_time_sync -= 25 * 3600
    await switch.async_update()
    assert len(hass.tasks) == 1, "did not re-sync after a day"


async def test_no_resync_while_disabled():
    switch, device, hass = make_switch()
    device.sync_time = False
    device.last_time_sync -= 25 * 3600

    await switch.async_update()

    assert hass.tasks == []


async def test_switch_methods_are_coroutines():
    """HA runs sync turn_on in an executor thread, where calling
    hass.async_create_task is not thread safe."""
    switch, _, _ = make_switch()

    assert asyncio.iscoroutinefunction(switch.async_turn_on)
    assert asyncio.iscoroutinefunction(switch.async_turn_off)
    own = type(switch).__dict__
    assert "turn_on" not in own, "still defines a synchronous turn_on"
    assert "turn_off" not in own, "still defines a synchronous turn_off"


async def run_tests():
    await test_turn_on_enables_synchronisation()
    await test_turn_off_disables_synchronisation()
    await test_state_follows_the_device()
    await test_turn_on_sets_the_clock_once()
    await test_daily_resync_waits_a_day()
    await test_no_resync_while_disabled()
    await test_switch_methods_are_coroutines()
    print("[SUCCESS] Time sync switch verified.")


if __name__ == "__main__":
    asyncio.run(run_tests())
