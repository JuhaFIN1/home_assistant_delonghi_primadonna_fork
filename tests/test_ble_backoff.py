"""Regression tests for the BLE availability / log-spam behaviour.

Home Assistant is not installed in this test environment, so the modules
device.py needs are stubbed before import. The tests then assert the
property that matters: a machine that is switched off must not produce
log records at Home Assistant's default level.
"""
import asyncio
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

PRESENT = {"value": False}
ADVERTISES = {"will_appear": False, "calls": 0}


def _mod(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _BleakError(Exception):
    pass


class _BleakDBusError(_BleakError):
    pass


class _Bluetooth:
    """Minimal stand-in for homeassistant.components.bluetooth."""

    class BluetoothScanningMode:
        ACTIVE = "active"

    @staticmethod
    def async_address_present(hass, address, connectable=True):
        return PRESENT["value"]

    @staticmethod
    def async_ble_device_from_address(hass, address, connectable=True):
        return MagicMock() if PRESENT["value"] else None

    @staticmethod
    def async_register_callback(hass, callback, match, mode):
        return lambda: None

    @staticmethod
    def async_track_unavailable(hass, callback, address, connectable=True):
        return lambda: None

    @staticmethod
    async def async_process_advertisements(
        hass, callback, match, mode, timeout
    ):
        ADVERTISES["calls"] += 1
        if ADVERTISES["will_appear"]:
            PRESENT["value"] = True
            return MagicMock()
        raise asyncio.TimeoutError


def _install_stubs():
    _mod("bleak", BleakClient=MagicMock)
    _mod("bleak.exc", BleakError=_BleakError, BleakDBusError=_BleakDBusError)
    _mod(
        "bleak_retry_connector",
        BleakClientWithServiceCache=MagicMock,
        establish_connection=MagicMock(),
    )
    homeassistant = _mod("homeassistant")
    homeassistant.__path__ = []
    _mod("homeassistant.config_entries", ConfigEntry=MagicMock)
    _mod("homeassistant.helpers", device_registry=MagicMock())
    _mod("homeassistant.helpers.device_registry",
         CONNECTION_NETWORK_MAC="mac")
    components = _mod("homeassistant.components", bluetooth=_Bluetooth)
    components.__path__ = []
    sys.modules["homeassistant.components.bluetooth"] = _Bluetooth
    backports = _mod("homeassistant.backports")
    backports.__path__ = []
    _mod("homeassistant.backports.enum", StrEnum=str)
    const = _mod(
        "homeassistant.const",
        CONF_MAC="mac",
        CONF_MODEL="model",
        CONF_NAME="name",
    )
    const.__getattr__ = lambda name: MagicMock()
    core = _mod(
        "homeassistant.core", HomeAssistant=MagicMock, callback=lambda f: f
    )
    core.__getattr__ = lambda name: MagicMock()


_install_stubs()
sys.path.insert(0, "custom_components")

from delonghi_primadonna import device as dev  # noqa: E402


@pytest.fixture
def machine():
    PRESENT["value"] = False
    ADVERTISES["will_appear"] = False
    ADVERTISES["calls"] = 0
    hass = MagicMock()
    hass.async_create_task = lambda coro: coro.close()
    return dev.DelongiPrimadonna(
        {"mac": "00:00:00:00:00:01", "name": "Machine", "model": ""}, hass
    )


def test_absent_machine_is_silent(machine, caplog):
    """One hour of polling a switched-off machine logs nothing."""
    caplog.set_level(logging.INFO)
    asyncio.run(machine.async_start())
    for _ in range(3600 // 30):
        asyncio.run(machine.async_refresh())
    assert caplog.records == []


def test_absent_machine_never_connects(machine):
    """No connection is attempted while the machine is not advertising."""
    asyncio.run(machine.async_start())
    asyncio.run(machine.async_refresh())
    assert machine.connected is False
    assert dev.establish_connection.call_count == 0


def test_advertisement_resets_backoff(machine):
    """Coming back into range clears any accumulated backoff."""
    machine._backoff = dev.MAX_BACKOFF
    machine._retry_after = 1e12
    machine._failure_logged = True

    PRESENT["value"] = True
    machine._async_on_advertisement(MagicMock(), None)

    assert machine.available is True
    assert machine._backoff == dev.MIN_BACKOFF
    assert machine._retry_after == 0.0
    assert machine._failure_logged is False


def test_backoff_grows_and_is_capped(machine):
    """Repeated real failures widen the retry window up to the cap."""
    machine._reset_backoff()
    delays = []
    for _ in range(10):
        delays.append(machine._backoff)
        machine._note_failure(_BleakError("boom"))

    assert delays[0] == dev.MIN_BACKOFF
    assert delays[1] == dev.MIN_BACKOFF * 2
    assert machine._backoff == dev.MAX_BACKOFF


def test_device_not_present_is_not_a_failure(machine):
    """A missing machine must not arm the backoff or log a warning."""
    machine._reset_backoff()
    machine._note_failure(dev.DeviceNotPresent("gone"))

    assert machine._retry_after == 0.0
    assert machine._backoff == dev.MIN_BACKOFF
    assert machine._failure_logged is False


def test_user_command_waits_for_a_sleeping_machine(machine):
    """The power-on button must not give up on one presence check.

    A machine in standby advertises intermittently; asking once and
    returning would make the button silently do nothing.
    """
    ADVERTISES["will_appear"] = True
    written = []

    async def _connected():
        machine._client = MagicMock()

        async def _write(*args, **kwargs):
            written.append(args)
            machine._response_event.set()

        machine._client.write_gatt_char = _write

    machine._connect = lambda *a, **kw: _connected()

    asyncio.run(machine.power_on())

    assert ADVERTISES["calls"] == 1
    assert machine.available is True
    # The command actually went out rather than being dropped.
    assert len(written) == 1


def test_user_command_warns_when_machine_never_appears(machine, caplog):
    """A command a person triggered must never fail silently."""
    caplog.set_level(logging.INFO)
    ADVERTISES["will_appear"] = False

    asyncio.run(machine.power_on())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "Cannot send command" in warnings[0].getMessage()


def test_background_polling_never_waits(machine, caplog):
    """Statistics polling must not block on a missing machine."""
    caplog.set_level(logging.INFO)

    asyncio.run(machine.get_statistics(100, 10))

    assert ADVERTISES["calls"] == 0
    assert caplog.records == []


def test_unavailable_does_not_drop_a_live_connection(machine):
    """Peripherals stop advertising while connected; that is not a loss."""
    machine._present = True
    machine.connected = True
    machine._client = MagicMock()
    machine._client.is_connected = True

    machine._async_on_unavailable(MagicMock())

    assert machine.connected is True
    assert machine._present is True


def test_unavailable_marks_loss_when_link_is_down(machine):
    """With no live client the callback does mean the machine is gone."""
    machine._present = True
    machine.connected = True
    machine._client = None

    machine._async_on_unavailable(MagicMock())

    assert machine.connected is False
    assert machine._present is False


def test_probe_is_rate_limited(machine):
    """The safety-net probe runs at most once per interval."""
    asyncio.run(machine.async_start())
    for _ in range(3600 // 30):
        asyncio.run(machine.async_refresh())

    # One hour of polling, one probe per PROBE_INTERVAL - not per poll.
    assert ADVERTISES["calls"] <= 3600 // dev.PROBE_INTERVAL + 1


def test_probe_recovers_a_missed_advertisement(machine):
    """If a callback is ever missed, the probe still brings us back."""
    asyncio.run(machine.async_start())
    reconnected = []
    machine.get_device_name = lambda: _noop(reconnected)
    ADVERTISES["will_appear"] = True

    asyncio.run(machine.async_refresh())

    assert reconnected == ["called"]


async def _noop(sink):
    sink.append("called")


def test_first_real_failure_warns_once(machine, caplog):
    """A genuine connection problem is reported, but only once."""
    caplog.set_level(logging.INFO)
    machine._reset_backoff()
    for _ in range(5):
        machine._note_failure(_BleakError("gatt error"))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
