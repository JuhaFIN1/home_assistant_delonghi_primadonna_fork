"""Delongi primadonna device description"""
import asyncio
import copy

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - fallback for older Home Assistant
    from homeassistant.backports.enum import StrEnum

import logging
import time
import uuid
from binascii import crc_hqx, hexlify
from dataclasses import dataclass
from datetime import datetime
from enum import IntFlag

from bleak import BleakClient
from bleak.exc import BleakDBusError, BleakError
from homeassistant.components import bluetooth
from homeassistant.const import CONF_MAC, CONF_MODEL, CONF_NAME
from homeassistant.core import HomeAssistant, callback

try:
    from bleak_retry_connector import (BleakClientWithServiceCache,
                                       establish_connection)
    HAS_RETRY_CONNECTOR = True
except ImportError:  # pragma: no cover - very old Home Assistant
    BleakClientWithServiceCache = BleakClient
    establish_connection = None
    HAS_RETRY_CONNECTOR = False

from .const import (AMERICANO_OFF, AMERICANO_ON, AVAILABLE_PROFILES,
                    BASE_COMMAND, BEVERAGE_NONE, BYTES_AUTOPOWEROFF_COMMAND,
                    BYTES_LOAD_PROFILES, BYTES_POWER, BYTES_STATISTICS_COMMAND,
                    BYTES_SWITCH_COMMAND, BYTES_TIME_COMMAND,
                    BYTES_WATER_HARDNESS_COMMAND,
                    BYTES_WATER_TEMPERATURE_COMMAND, COFFE_OFF, COFFE_ON,
                    COFFEE_GROUNDS_CONTAINER_CLEAN,
                    COFFEE_GROUNDS_CONTAINER_DETACHED,
                    COFFEE_GROUNDS_CONTAINER_FULL, CONTROLL_CHARACTERISTIC,
                    DEBUG, DEFAULT_DEVICE_NAME, DEFAULT_IMAGE_URL,
                    DEVICE_READY, DEVICE_STATUS, DEVICE_TURNOFF, DOMAIN,
                    DOPPIO_OFF, DOPPIO_ON, ESPRESSO2_OFF, ESPRESSO2_ON,
                    ESPRESSO_OFF, ESPRESSO_ON, HOTWATER_OFF, HOTWATER_ON,
                    LONG_OFF, LONG_ON, MACHINE_STATUS, NAME_CHARACTERISTIC,
                    NOZZLE_STATE, START_COFFEE, STEAM_OFF, STEAM_ON,
                    WATER_SHORTAGE, WATER_TANK_DETACHED)
from .machine_switch import MachineSwitch, parse_switches
from .model import get_machine_model

_LOGGER = logging.getLogger(__name__)

START_BYTE = 0xD0

# Backoff applied when the machine is advertising but refuses connections.
# Without this the integration retries at the entity poll rate forever.
MIN_BACKOFF = 30
MAX_BACKOFF = 900

# How long a user initiated command waits for the machine to advertise.
# A machine in standby advertises infrequently, so a single point-in-time
# presence check is not enough to decide it is unreachable.
DISCOVERY_TIMEOUT = 25

# Safety net: even though we subscribe to advertisements, probe for the
# machine occasionally so a missed callback can never leave the
# integration permanently asleep. Kept under Home Assistant's 10s entity
# update warning threshold.
PROBE_INTERVAL = 600
PROBE_TIMEOUT = 8


class DeviceNotPresent(BleakError):
    """Raised when the machine is not advertising.

    This is an expected, boring condition (machine switched off or out of
    range) and must never be logged above debug level.
    """


@dataclass
class MonitorData:
    """Monitor Data structure"""
    switches: int
    alarms: int
    status: int
    sub_status: int
    nozzle_state: int


def parse_monitor_data(data: bytes) -> MonitorData | None:
    """Parse Monitor Data packet (v1 0x70 or v2 0x75)"""
    if len(data) < 3:
        return None

    answer_id = data[2]

    # Defaults
    switches = 0
    alarms = 0
    status = 0
    sub_status = 0
    nozzle_state = -1

    if answer_id == 0x75:  # MonitorDataV2
        if len(data) < 14:
            return None
        # Switches: Bytes 5, 6 (Little Endian)
        switches = data[5] + (data[6] << 8)

        # Alarms: Bytes 7, 8, 12, 13 (Little Endian in blocks)
        # Based on MonitorDataV2.b():
        # iS = z.S(bArr[7]) + (z.S(bArr[8]) << 8) + (z.S(bArr[12]) << 16) + \
        # (z.S(bArr[13]) << 24)
        alarms = (data[7]
                  + (data[8] << 8)
                  + (data[12] << 16)
                  + (data[13] << 24))

        # Status/State: Byte 9
        status = data[9]

        # SubStatus: Byte 10
        sub_status = data[10]

        # Nozzle State: Byte 4 (from MonitorDataV2.a())
        nozzle_state = data[4]

    elif answer_id == 0x70:  # MonitorData (v1)
        if len(data) < 11:
            return None

        # Switches: Bytes 9, 10
        # Based on MonitorData.g(): bArr[9] + (bArr[10] << 8)
        switches = data[9] + (data[10] << 8)

        # Alarms: Bytes 4, 5
        # Based on MonitorData.b(): bArr[4] + (bArr[5] << 8)
        alarms = data[4] + (data[5] << 8)

        # Status/State: Byte 8
        # Based on MonitorData.f(): bArr[8]
        status = data[8]

        # SubStatus: Byte 9
        # Based on MonitorData.e(): bArr[9]
        # Note: Byte 9 is also used for switches low byte?
        # MonitorData.g (Switches) uses 9, 10.
        # MonitorData.e (SubState/Aux) uses 9.
        # We will extract it as sub_status anyway.
        sub_status = data[9]

        # Nozzle State: a() returns -1 for v1.
        nozzle_state = -1

    else:
        return None

    return MonitorData(switches, alarms, status, sub_status, nozzle_state)


class BeverageEntityFeature(IntFlag):
    """Supported features of the beverage entity"""

    MAKE_BEVERAGE = 1
    SET_TEMPERATURE = 2
    SET_INTENCE = 4


class AvailableBeverage(StrEnum):
    """Coffee machine available beverages"""

    NONE = BEVERAGE_NONE
    STEAM = 'steam'
    LONG = 'long'
    COFFEE = 'coffee'
    DOPIO = 'dopio'
    HOTWATER = 'hot_water'
    ESPRESSO = 'espresso'
    AMERICANO = 'americano'
    ESPRESSO2 = 'espresso2'


class NotificationType(StrEnum):
    """Coffee machine notification types"""

    STATUS = 'status'
    PROCESS = 'process'


class BeverageCommand:
    """Coffee machine beverage commands"""

    def __init__(self, on, off):
        self.on = on
        self.off = off


class BeverageNotify:
    """Coffee machine beverage notifications"""

    def __init__(self, kind, description):
        self.kind = str(kind)
        self.description = str(description)


class DeviceSwitches:
    """All binary switches for the device"""

    def __init__(self):
        self.sounds = False
        self.energy_save = False
        self.cup_light = False
        self.filter = False
        self.is_on = False


BEVERAGE_COMMANDS = {
    AvailableBeverage.NONE: BeverageCommand(DEBUG, DEBUG),
    AvailableBeverage.STEAM: BeverageCommand(STEAM_ON, STEAM_OFF),
    AvailableBeverage.LONG: BeverageCommand(LONG_ON, LONG_OFF),
    AvailableBeverage.COFFEE: BeverageCommand(COFFE_ON, COFFE_OFF),
    AvailableBeverage.DOPIO: BeverageCommand(DOPPIO_ON, DOPPIO_OFF),
    AvailableBeverage.HOTWATER: BeverageCommand(HOTWATER_ON, HOTWATER_OFF),
    AvailableBeverage.ESPRESSO: BeverageCommand(ESPRESSO_ON, ESPRESSO_OFF),
    AvailableBeverage.AMERICANO: BeverageCommand(AMERICANO_ON, AMERICANO_OFF),
    AvailableBeverage.ESPRESSO2: BeverageCommand(ESPRESSO2_ON, ESPRESSO2_OFF),
}

# Map recipe IDs from MachinesModels.json to existing hardcoded commands
RECIPE_ID_TO_BEVERAGE = {
    1: AvailableBeverage.ESPRESSO,     # Espresso Coffee
    2: AvailableBeverage.COFFEE,       # Regular Coffee
    3: AvailableBeverage.LONG,         # Long Coffee
    4: AvailableBeverage.ESPRESSO2,    # 2X Espresso Coffee
    5: AvailableBeverage.DOPIO,        # Doppio+
    6: AvailableBeverage.AMERICANO,    # Americano
    16: AvailableBeverage.HOTWATER,    # Hot Water
    17: AvailableBeverage.STEAM,       # Steam
}


def _build_stop_command(recipe_id: int) -> list[int]:
    """Build a stop command for any recipe ID."""
    return [0x0D, 0x08, 0x83, 0xF0, recipe_id & 0xFF, 0x02, 0x06, 0x00, 0x00]


def _build_start_command(recipe_id: int, coffee_qty: int = 0,
                         milk_qty: int = 0) -> list[int]:
    """Build a generic start command for a recipe.

    The command structure varies by recipe type, but this covers the common
    coffee-only and milk-drink patterns observed from the DeLonghi protocol.
    """
    rid = recipe_id & 0xFF

    if milk_qty <= 0:
        # Coffee-only format
        return [
            0x0D, 0x0D, 0x83, 0xF0, rid, 0x01,
            0x01, 0x00, coffee_qty & 0xFF,
            0x00, 0x00, 0x06, 0x00, 0x00,
        ]

    # Milk drink format (observed for cappuccino-like beverages)
    milk_lo = milk_qty & 0xFF
    milk_hi = (milk_qty >> 8) & 0xFF
    return [
        0x0D, 0x0F, 0x83, 0xF0, rid, 0x01,
        0x01, 0x00, coffee_qty & 0xFF,
        0x02, 0x02, milk_hi, milk_lo,
        0x06, 0x00, 0x00,
    ]


DEVICE_NOTIFICATION = {
    str(bytearray(DEVICE_READY)): BeverageNotify(
        NotificationType.STATUS, 'DeviceOK'
    ),
    str(bytearray(DEVICE_TURNOFF)): BeverageNotify(
        NotificationType.STATUS, 'DeviceOFF'
    ),
    str(bytearray(WATER_TANK_DETACHED)): BeverageNotify(
        NotificationType.STATUS, 'NoWaterTank'
    ),
    str(bytearray(WATER_SHORTAGE)): BeverageNotify(
        NotificationType.STATUS, 'NoWater'
    ),
    str(bytearray(COFFEE_GROUNDS_CONTAINER_DETACHED)): BeverageNotify(
        NotificationType.STATUS, 'NoGroundsContainer'
    ),
    str(bytearray(COFFEE_GROUNDS_CONTAINER_FULL)): BeverageNotify(
        NotificationType.STATUS, 'GroundsContainerFull'
    ),
    str(bytearray(COFFEE_GROUNDS_CONTAINER_CLEAN)): BeverageNotify(
        NotificationType.STATUS, 'GroundsContainerFull'
    ),
    str(bytearray(START_COFFEE)): BeverageNotify(
        NotificationType.STATUS, 'START_COFFEE'
    ),
}


class DelongiPrimadonna:
    """Delongi Primadonna class"""

    def __init__(self, config: dict, hass: HomeAssistant) -> None:
        """Initialize device"""
        self._device_status = None
        self._client = None
        self._hass = hass
        self._device = None
        self._connecting = False
        self.mac = config.get(CONF_MAC)
        self.name = config.get(CONF_NAME)
        self.product_code = config.get(CONF_MODEL)
        self.hostname = ''
        self.friendly_name = ''
        self.cooking = BEVERAGE_NONE
        self.connected = False
        self.notify = False
        self.steam_nozzle = NOZZLE_STATE[-1]
        self.service = 0
        self.status = "Ready"
        self.switches = DeviceSwitches()
        self.active_switches: list[MachineSwitch] = []
        self.sync_time = False
        self._lock = asyncio.Lock()
        self._rx_buffer = bytearray()
        self._response_event = None
        self._last_response: bytes | None = None
        # --- availability / backoff bookkeeping -------------------------
        # ``_present`` mirrors what the Bluetooth stack sees (advertising),
        # ``connected`` means we actually hold a GATT connection.
        self._present = False
        self._unsub_bluetooth = None
        self._unsub_unavailable = None
        self._backoff = MIN_BACKOFF
        self._retry_after = 0.0
        self._failure_logged = False
        self._next_probe = 0.0
        self.statistics: dict[int, int | float] = {}
        self._last_stats_request = 0.0
        self._stats_lock = asyncio.Lock()
        self._statistics_task: asyncio.Task | None = None
        machine = get_machine_model(self.product_code)
        self.model = (
            machine.name if machine and machine.name else 'Prima Donna'
        )
        self.image_url = (
            machine.image_url if machine and machine.image_url
            else DEFAULT_IMAGE_URL
        )
        self._n_profiles = (
            machine.nProfiles
            if machine and machine.nProfiles
            else len(AVAILABLE_PROFILES)
        )
        self.active_profile_id: int | None = None
        for pid in range(1, self._n_profiles + 1):
            AVAILABLE_PROFILES.setdefault(pid, f"Profile {pid}")
        for pid in list(AVAILABLE_PROFILES):
            if pid > self._n_profiles:
                AVAILABLE_PROFILES.pop(pid)
        self.profiles = list(AVAILABLE_PROFILES.values())
        self._profiles_loaded = False

        # Build dynamic beverage list from machine recipes
        # name -> {id, coffee_qty, milk_qty}
        self._recipe_map: dict[str, dict] = {}
        self.available_beverages: list[str] = [BEVERAGE_NONE]
        if machine and machine.recipes:
            custom_idx = 0
            for recipe in machine.recipes:
                rname = recipe.name.value if recipe.name else None
                if rname and recipe.id is not None:
                    rid = int(recipe.id)
                    # Deduplicate: custom recipes get numbered names
                    if rname == "Custom":
                        custom_idx += 1
                        rname = f"Custom {custom_idx}"
                    elif rname in self._recipe_map:
                        rname = f"{rname} ({rid})"
                    self._recipe_map[rname] = {
                        'id': rid,
                        'coffee_qty': recipe.coffee_qty or 0,
                        'milk_qty': recipe.milk_qty or 0,
                    }
                    self.available_beverages.append(rname)
        if len(self.available_beverages) <= 1:
            # Fallback to legacy enum if no recipes
            self.available_beverages = [*AvailableBeverage]

    # ------------------------------------------------------------------
    # Lifecycle / availability
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Start watching for the machine's advertisements.

        Instead of polling a possibly absent device we let the Bluetooth
        integration tell us when it shows up. A machine that is switched
        off simply produces no callbacks, and therefore no log lines.
        """
        self._present = bluetooth.async_address_present(
            self._hass, self.mac, connectable=True
        )
        self._unsub_bluetooth = bluetooth.async_register_callback(
            self._hass,
            self._async_on_advertisement,
            {'address': self.mac.upper(), 'connectable': True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        # Let the Bluetooth stack tell us when the machine stops
        # advertising, so state flips promptly instead of on the next poll.
        self._unsub_unavailable = bluetooth.async_track_unavailable(
            self._hass,
            self._async_on_unavailable,
            self.mac.upper(),
            connectable=True,
        )
        _LOGGER.debug(
            'Watching %s, present at startup: %s', self.mac, self._present
        )
        if self._present:
            await self.async_refresh()

    async def async_stop(self) -> None:
        """Stop watching and drop the connection."""
        for unsub_name in ('_unsub_bluetooth', '_unsub_unavailable'):
            unsub = getattr(self, unsub_name)
            if unsub is not None:
                unsub()
                setattr(self, unsub_name, None)
        await self.cancel_statistics_update()
        await self.disconnect()

    @callback
    def _async_on_unavailable(self, _service_info) -> None:
        """Handle the machine no longer being advertised.

        A BLE peripheral normally stops advertising while it is in a
        connection, so this callback fires during perfectly healthy
        sessions. Only treat it as a loss when the link is actually down.
        """
        if self._client is not None and self._client.is_connected:
            _LOGGER.debug(
                '%s stopped advertising but the connection is up', self.mac
            )
            return
        if self._present:
            _LOGGER.info('%s went out of range', self.name)
        self._present = False
        self.connected = False

    @callback
    def _async_on_advertisement(self, service_info, change) -> None:
        """Handle an advertisement from the machine."""
        was_present = self._present
        self._present = True
        if not was_present:
            _LOGGER.info('%s is back in range', self.name)
            # A fresh advertisement means a fresh start: forget the backoff
            # accumulated while the machine was away.
            self._reset_backoff()
            self._hass.async_create_task(self.async_refresh())

    @property
    def available(self) -> bool:
        """Whether the machine is reachable at all."""
        return self._present

    def _address_present(self) -> bool:
        """Ask the Bluetooth stack whether the machine is advertising.

        A held-open GATT link makes the machine stop advertising, same as
        in ``_async_on_unavailable``, so an active connection counts as
        present without asking the advertisement history.
        """
        if self._client is not None and self._client.is_connected:
            self._present = True
            return True
        present = bluetooth.async_address_present(
            self._hass, self.mac, connectable=True
        )
        if not present and self._present:
            _LOGGER.info('%s went out of range', self.name)
        self._present = present
        return present

    async def _async_wait_for_device(
        self, timeout: int = DISCOVERY_TIMEOUT
    ) -> bool:
        """Wait for the machine to advertise, requesting an active scan.

        A machine in standby can advertise only every few seconds, and the
        Bluetooth stack drops it from its history in between. Asking once
        and giving up would make the power-on button unreliable, so wait
        for a real advertisement instead.
        """
        _LOGGER.debug(
            'Waiting up to %ss for an advertisement from %s',
            timeout,
            self.mac,
        )
        try:
            await bluetooth.async_process_advertisements(
                self._hass,
                lambda service_info: True,
                {'address': self.mac.upper(), 'connectable': True},
                bluetooth.BluetoothScanningMode.ACTIVE,
                timeout,
            )
        except asyncio.TimeoutError:
            return False
        self._present = True
        self._reset_backoff()
        return True

    def _reset_backoff(self) -> None:
        self._backoff = MIN_BACKOFF
        self._retry_after = 0.0
        self._failure_logged = False

    def _note_failure(self, error: Exception) -> None:
        """Record a failed connection and widen the retry window."""
        if isinstance(error, DeviceNotPresent):
            # Not a failure at all: the machine is simply off. The
            # advertisement callback wakes us up when it returns.
            _LOGGER.debug('%s is not advertising', self.mac)
            return
        self._retry_after = time.monotonic() + self._backoff
        # Log the first failure of a streak at warning level so real
        # problems stay visible, then stay quiet until we recover.
        if not self._failure_logged:
            _LOGGER.warning(
                'Cannot reach %s (%s: %s). Retrying with backoff, '
                'further attempts are logged at debug level.',
                self.name,
                type(error).__name__,
                error,
            )
            self._failure_logged = True
        else:
            _LOGGER.debug(
                'Connection to %s still failing (%s), next try in %ss',
                self.mac,
                error,
                self._backoff,
            )
        self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    async def async_refresh(self) -> None:
        """Refresh machine state, but only when that can possibly work.

        This replaces the old behaviour where every entity poll produced a
        connection attempt regardless of whether the machine existed.
        """
        if not self._address_present():
            self.connected = False
            await self._async_probe_if_due()
            return
        if time.monotonic() < self._retry_after:
            return
        await self.get_device_name()

    async def _async_probe_if_due(self) -> None:
        """Occasionally listen for the machine even when it looks absent.

        Advertisement callbacks are the primary wake-up path. This is the
        backstop: if one is ever missed, the integration would otherwise
        stay asleep until Home Assistant restarts. Logs at debug only, so
        it does not bring back the log spam.
        """
        now = time.monotonic()
        if now < self._next_probe:
            return
        self._next_probe = now + PROBE_INTERVAL
        if await self._async_wait_for_device(PROBE_TIMEOUT):
            _LOGGER.debug('Probe found %s, reconnecting', self.mac)
            await self.get_device_name()

    @callback
    def _async_disconnected(self, _client) -> None:
        """Handle the machine dropping the GATT link."""
        _LOGGER.debug('Disconnected from %s', self.mac)
        self._client = None
        self.connected = False

    async def disconnect(self):
        """Disconnect from the device."""
        _LOGGER.debug("Disconnect from %s", self.mac)
        async with self._lock:
            client = self._client
            if client is not None and client.is_connected:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5)
                except (
                    asyncio.TimeoutError,
                    Exception,
                ) as error:  # noqa: BLE001
                    _LOGGER.debug(
                        "Forced disconnect [%s]: %s",
                        type(error).__name__,
                        error
                    )
                finally:
                    self._client = None
                    self.connected = False
            else:
                self._client = None
                self.connected = False

    async def _connect(self, retries=3):
        """Connect to the device.

        Retries are delegated to ``bleak_retry_connector``, which knows how
        to deal with ESPHome proxies, transient GATT errors and the various
        backend quirks far better than a hand written loop. It also keeps
        Home Assistant from logging its own "connect() called without
        bleak-retry-connector" warning on every attempt.
        """
        if self._client is not None and self._client.is_connected:
            return

        self._connecting = True
        try:
            self._device = bluetooth.async_ble_device_from_address(
                self._hass, self.mac, connectable=True
            )
            if not self._device:
                # Expected whenever the machine is off or out of range.
                self._present = False
                raise DeviceNotPresent(
                    f'{self.mac} is not advertising'
                )

            _LOGGER.debug('Connecting to %s', self.mac)

            if HAS_RETRY_CONNECTOR:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._device,
                    self.name or self.mac,
                    self._async_disconnected,
                    max_attempts=retries,
                    ble_device_callback=lambda: (
                        bluetooth.async_ble_device_from_address(
                            self._hass, self.mac, connectable=True
                        )
                    ),
                )
            else:  # pragma: no cover - legacy fallback
                self._client = BleakClient(
                    self._device,
                    disconnected_callback=self._async_disconnected,
                )
                await asyncio.wait_for(self._client.connect(), timeout=20)

            # Service discovery happens during connect; ``client.services``
            # holds the result. Do not call get_services() - it raises a
            # FutureWarning on recent Bleak.
            await asyncio.wait_for(
                self._client.start_notify(
                    uuid.UUID(CONTROLL_CHARACTERISTIC),
                    self._process_raw_data,
                ),
                timeout=10,
            )
            self._reset_backoff()
        except Exception as error:
            client = self._client
            self._client = None
            self.connected = False
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            self._note_failure(error)
            raise
        finally:
            self._connecting = False

    def _make_switch_command(self):
        """Make hex command"""
        base_command = list(BASE_COMMAND)
        base_command[3] = '1' if self.switches.energy_save else '0'
        base_command[4] = '1' if self.switches.cup_light else '0'
        base_command[5] = '1' if self.switches.sounds else '0'
        hex_command = BYTES_SWITCH_COMMAND.copy()
        hex_command[9] = int(''.join(base_command), 2)
        return hex_command

    async def _event_trigger(self, value):
        """
        Trigger event
        :param value: event value
        """
        event_data = {'data': str(hexlify(value, ' '))}

        notification_message = (
            str(hexlify(value, ' '))
            .replace(' ', ', 0x')
            .replace("b'", '[0x')
            .replace("'", ']')
        )

        if str(bytearray(value)) in DEVICE_NOTIFICATION:
            notification_message = DEVICE_NOTIFICATION.get(
                str(bytearray(value))
            ).description
            event_data.setdefault(
                'type', DEVICE_NOTIFICATION.get(str(bytearray(value))).kind
            )
            event_data.setdefault(
                'description',
                DEVICE_NOTIFICATION.get(str(bytearray(value))).description,
            )
        self._hass.bus.async_fire(f'{DOMAIN}_event', event_data)

        if self.notify:
            answer_id = f"{value[2]:02x}"
            await self._hass.services.async_call(
                'persistent_notification',
                'create',
                {
                    'message': notification_message,
                    'title': f'{self.name} {answer_id}',
                    'notification_id': f'{self.mac}_err_{uuid.uuid4()}',
                },
            )
        _LOGGER.debug('Event triggered: %s', event_data)

    async def _process_raw_data(self, sender, value):
        """Assemble incoming BLE packets and pass complete messages."""
        self._rx_buffer.extend(value)

        while True:
            if len(self._rx_buffer) < 2:
                return
            try:
                start_index = self._rx_buffer.index(START_BYTE)
            except ValueError:
                self._rx_buffer.clear()
                return

            if start_index > 0:
                del self._rx_buffer[:start_index]

            if len(self._rx_buffer) < 2:
                return

            msg_len = self._rx_buffer[1] + 1

            if len(self._rx_buffer) < msg_len:
                return

            packet = bytes(self._rx_buffer[:msg_len])
            del self._rx_buffer[:msg_len]
            await self._handle_data(sender, packet)

    async def _handle_data(self, sender, value):
        """Handle notifications from the device."""
        if (
            self._response_event is not None
            and not self._response_event.is_set()
        ):
            self._response_event.set()
        answer_id = value[2] if len(value) > 2 else None

        if answer_id in [0x75, 0x70]:
            monitor_data = parse_monitor_data(value)
            if monitor_data:
                self._handle_monitor_data(monitor_data, answer_id, value)
        elif answer_id == 0xA4:
            parsed = []
            try:
                parsed = self._parse_profile_response(
                    list(value)
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to parse profile response: %s", err)
            for pid, name in parsed.items():
                AVAILABLE_PROFILES[pid] = name
            _LOGGER.debug(
                "Available profiles: %s",
                AVAILABLE_PROFILES
            )
            self.profiles = list(AVAILABLE_PROFILES.values())
        elif answer_id == 0xA9:
            profile_id = value[4] if len(value) > 4 else None
            status = value[5] if len(value) > 5 else None
            _LOGGER.debug(
                "Profile change response id=%s status=%s raw=%s",
                profile_id,
                status,
                hexlify(value, " "),
            )
            if profile_id is not None and status == 0:
                self.active_profile_id = profile_id
        elif answer_id == 0xA2:
            await self._parse_statistics(value)

        hex_value = hexlify(value, ' ')

        if self._device_status != hex_value:
            _LOGGER.debug(
                'Received data: %s from %s',
                hex_value,
                sender
            )
            await self._event_trigger(value)

        self._device_status = hex_value

    def _handle_monitor_data(
        self, monitor_data: MonitorData, answer_id: int, raw_packet: bytes
    ) -> None:
        """Apply parsed monitor data to device state."""
        # Power state
        self.switches.is_on = monitor_data.status > 0

        # Nozzle state (only present in v2 / 0x75 packets)
        if monitor_data.nozzle_state != -1:
            self.steam_nozzle = NOZZLE_STATE.get(
                monitor_data.nozzle_state, NOZZLE_STATE[-1]
            )

        # Alarm bitmask — feeds the Descale binary sensor (bit 2)
        self.service = monitor_data.alarms

        # Display status: show first active alarm, or machine state
        if monitor_data.alarms > 0:
            for i in range(32):
                if (monitor_data.alarms >> i) & 1:
                    self.status = DEVICE_STATUS.get(i, f"Alarm {i}")
                    break
        elif monitor_data.status in (0, 1, 5):
            self.status = "Ready"
        else:
            self.status = MACHINE_STATUS.get(
                monitor_data.status,
                f"State {monitor_data.status}"
            )

        # Active switches (v2 only; v1 uses different byte offsets)
        if answer_id == 0x75:
            self.active_switches = parse_switches(raw_packet)

    def _parse_profile_response(
        self,
        data: list[int],
    ) -> dict[int, str]:
        """Parse profile names sent by the machine."""

        b = bytes(data)
        if len(b) < 4 or b[0] != 0xD0:
            raise ValueError("Wrong start byte")

        profiles: dict[int, str] = {}
        NAME_SIZE = 20
        NAME_OFFSET = 1
        NAME_HEADER = 4
        profile_index = 1
        idx = NAME_HEADER
        while idx + NAME_SIZE < len(b):
            profiles.setdefault(
                profile_index,
                b[idx:idx + NAME_SIZE]
                .decode("utf-16-be")
                .rstrip("\x00")
                .strip(),
            )
            profile_index += 1
            idx += NAME_SIZE + NAME_OFFSET
        return profiles

    async def power_on(self) -> None:
        """Turn the device on."""
        await self.send_command(BYTES_POWER)

    async def cup_light_on(self) -> None:
        """Turn the cup light on."""
        self.switches.cup_light = True
        await self.send_command(self._make_switch_command())

    async def cup_light_off(self) -> None:
        """Turn the cup light off."""
        self.switches.cup_light = False
        await self.send_command(self._make_switch_command())

    async def energy_save_on(self):
        """Enable energy save mode"""
        self.switches.energy_save = True
        await self.send_command(self._make_switch_command())

    async def energy_save_off(self):
        """Enable energy save mode"""
        self.switches.energy_save = False
        await self.send_command(self._make_switch_command())

    async def sound_alarm_on(self):
        """Enable sound alarm"""
        self.switches.sounds = True
        await self.send_command(self._make_switch_command())

    async def sound_alarm_off(self):
        """Disable sound alarm"""
        self.switches.sounds = False
        await self.send_command(self._make_switch_command())

    async def beverage_start(self, beverage: str) -> None:
        """Start beverage by name (recipe or legacy enum)."""
        if beverage == BEVERAGE_NONE:
            return
        # Try recipe map (dynamic from machine model)
        recipe = self._recipe_map.get(beverage)
        if recipe:
            rid = recipe['id']
            # Use hardcoded command if available for this recipe ID
            legacy = RECIPE_ID_TO_BEVERAGE.get(rid)
            if legacy and legacy in BEVERAGE_COMMANDS:
                _LOGGER.info(
                    "Starting %s (recipe %d) via legacy",
                    beverage, rid,
                )
                await self.send_command(BEVERAGE_COMMANDS[legacy].on)
            else:
                _LOGGER.info(
                    "Starting %s (recipe %d) via dynamic",
                    beverage, rid,
                )
                cmd = _build_start_command(
                    rid, recipe['coffee_qty'], recipe['milk_qty']
                )
                await self.send_command(cmd)
            self.cooking = beverage
            return
        _LOGGER.warning("Unknown beverage: %s", beverage)

    async def beverage_cancel(self) -> None:
        """Cancel beverage"""
        if self.cooking == BEVERAGE_NONE:
            return
        recipe = self._recipe_map.get(self.cooking)
        if recipe:
            await self.send_command(_build_stop_command(recipe['id']))
        else:
            _LOGGER.warning("Cannot cancel unknown beverage: %s", self.cooking)
        self.cooking = BEVERAGE_NONE

    async def debug(self):
        """Send command which causes status reply"""
        await self.send_command(DEBUG)

    async def get_device_name(self):
        """
        Get device name
        :return: device name
        """
        async with self._lock:
            try:
                await self._connect()
                try:
                    self.hostname = bytes(
                        await self._client.read_gatt_char(
                            uuid.UUID(NAME_CHARACTERISTIC)
                        )
                    ).decode('utf-8')
                except BleakError as error:
                    _LOGGER.debug(
                        'Could not read NAME_CHARACTERISTIC: %s', error
                    )
                    self.hostname = self.name or DEFAULT_DEVICE_NAME
                await self._client.write_gatt_char(
                    uuid.UUID(CONTROLL_CHARACTERISTIC), bytearray(DEBUG)
                )
                if not self.connected:
                    _LOGGER.info('Connected to %s', self.name)
                self.connected = True
            # _connect() already logged the first failure of a streak and
            # armed the backoff, so these handlers stay at debug level.
            except DeviceNotPresent:
                self.connected = False
            except BleakDBusError as error:
                self.connected = False
                _LOGGER.debug('BleakDBusError: %s', error)
            except BleakError as error:
                self.connected = False
                _LOGGER.debug('BleakError: %s', error)
            except asyncio.exceptions.TimeoutError as error:
                self.connected = False
                _LOGGER.debug('TimeoutError: %s at device connection', error)
            except asyncio.exceptions.CancelledError as error:
                self.connected = False
                _LOGGER.debug('CancelledError: %s', error)

        if self.connected and not self._profiles_loaded:
            command = BYTES_LOAD_PROFILES.copy()
            command[5] = self._n_profiles
            await self.send_command(command)
            # Default to first profile until the user switches
            if self.active_profile_id is None:
                self.active_profile_id = 1
            self._profiles_loaded = True

    async def set_time(self, dt: datetime) -> None:
        """Set device clock from provided datetime."""
        packet = BYTES_TIME_COMMAND.copy()
        packet[4] = dt.hour & 0xFF
        packet[5] = dt.minute & 0xFF
        await self.send_command(packet)

    async def select_profile(self, profile_id) -> None:
        """select a profile."""
        _LOGGER.debug("Send select profile command id=%s", profile_id)
        message = [0x0D, 0x06, 0xA9, 0xF0, profile_id, 0xD7, 0xC0]
        await self.send_command(message)

    async def set_auto_power_off(self, power_off_interval) -> None:
        """Set auto power off time."""
        message = copy.deepcopy(BYTES_AUTOPOWEROFF_COMMAND)
        message[9] = power_off_interval
        await self.send_command(message)

    async def set_water_hardness(self, hardness_level) -> None:
        """Set water hardness"""
        message = copy.deepcopy(BYTES_WATER_HARDNESS_COMMAND)
        message[9] = hardness_level
        await self.send_command(message)

    async def set_water_temperature(self, temperature_level) -> None:
        """Set water temperature"""
        message = copy.deepcopy(BYTES_WATER_TEMPERATURE_COMMAND)
        message[9] = temperature_level
        await self.send_command(message)

    async def common_command(self, command: str) -> None:
        """Send custom BLE command"""
        message = [int(x, 16) for x in command.split(' ')]
        await self.send_command(message)

    async def send_command(self, message, retries=3, wait_for_device=True):
        """Send a command, waking the machine up if necessary.

        ``wait_for_device`` is True for anything a person triggered: those
        commands are rare and must not fail just because the machine
        happened to be between advertisements. Background polling passes
        False so it never blocks.
        """
        if not self._address_present():
            if not wait_for_device:
                _LOGGER.debug(
                    'Skipping background command, %s is not in range',
                    self.name,
                )
                self.connected = False
                return
            if not await self._async_wait_for_device():
                # A command a person asked for must never fail silently.
                _LOGGER.warning(
                    'Cannot send command to %s: no Bluetooth advertisement '
                    'within %ss. The machine is switched off at the mains, '
                    'out of range of the proxy, or already connected to '
                    'the De\'Longhi app (it accepts only one connection).',
                    self.name,
                    DISCOVERY_TIMEOUT,
                )
                self.connected = False
                return
        async with self._lock:
            message_to_send = copy.deepcopy(message)
            for attempt in range(retries):
                try:
                    await self._connect()
                    crc = crc_hqx(bytearray(message_to_send[:-2]), 0x1D0F)
                    crc_bytes = crc.to_bytes(2, byteorder='big')
                    message_to_send[-2] = crc_bytes[0]
                    message_to_send[-1] = crc_bytes[1]
                    _LOGGER.debug(
                        'Send command: %s',
                        hexlify(bytearray(message_to_send), " ")
                    )
                    self._response_event = asyncio.Event()
                    await self._client.write_gatt_char(
                        CONTROLL_CHARACTERISTIC, bytearray(message_to_send)
                    )
                    try:
                        await asyncio.wait_for(
                            self._response_event.wait(),
                            timeout=10,
                        )
                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            'Timeout waiting for response to command: %s',
                            hexlify(bytearray(message_to_send), " ")
                        )
                    finally:
                        self._response_event = None
                    return
                except DeviceNotPresent:
                    self.connected = False
                    if wait_for_device:
                        _LOGGER.warning(
                            '%s stopped advertising while the command was '
                            'being sent', self.name
                        )
                    return
                except BleakError as error:
                    self.connected = False
                    self._client = None
                    _LOGGER.debug(
                        'BleakError: %s (attempt %d)',
                        error,
                        attempt + 1
                    )
                    await asyncio.sleep(2)
            _LOGGER.warning(
                'Failed to send command to %s after %d attempts',
                self.name,
                retries,
            )

    async def _parse_statistics(self, data: bytes) -> None:
        """Parse statistics response"""
        if len(data) < 12:
            return

        hex_data = hexlify(data, " ").decode('utf-8')
        _LOGGER.debug("Statistics Parser. Raw: %s", hex_data)

        # The first parameter ID is implicit from bytes 4-5
        pid = (data[4] << 8) | data[5]
        val = int.from_bytes(data[6:10], byteorder='big')
        self.statistics[pid] = val
        _LOGGER.debug(
            "Statistics Parser.Parsed (Implicit): ID %s = %s", pid, val
        )

        # Subsequent parameters are in the format [ID 2B] + [Value 4B]
        current_offset = 10

        # Check if there is at least one more [ID 2B] + [Val 4B] block before
        # CRC (last 2 bytes)
        while current_offset + 6 <= len(data) - 2:
            pid = (data[current_offset] << 8) | data[current_offset + 1]
            val = int.from_bytes(
                data[current_offset + 2:current_offset + 6],
                byteorder='big'
            )
            self.statistics[pid] = val
            _LOGGER.debug(
                "Statistics Parser.Parsed (Explicit): ID %s = %s", pid, val
            )
            current_offset += 6

        # Calculate combined values for total coffee
        if 3000 in self.statistics or 3077 in self.statistics:
            total = self.statistics.get(3000, 0) + self.statistics.get(3077, 0)
            self.statistics[-3077] = total

        # Calculate combined values for total coffee with milk
        if 3001 in self.statistics or 3003 in self.statistics:
            total = self.statistics.get(3001, 0) + self.statistics.get(3003, 0)
            self.statistics[-3003] = total

        # Convert water quantity to liters (divide by 2000).
        # Use float division to preserve precision.
        if 106 in self.statistics:
            water_ml = self.statistics.get(106, 0)
            self.statistics[10106] = round(water_ml / 2000.0, 2)

    def schedule_statistics_update(self) -> None:
        """Schedule a statistics refresh as a single tracked background task.

        Deduplicates: a request while one is already in flight is a no-op,
        so entities polling concurrently don't stack one task each.
        """
        task = self._statistics_task
        if task is not None and not task.done():
            return
        self._statistics_task = self._hass.async_create_background_task(
            self._run_statistics_update(), "delonghi statistics update",
        )

    async def _run_statistics_update(self) -> None:
        """Run update_statistics(), logging unexpected failures."""
        try:
            await self.update_statistics()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Statistics update failed")

    async def cancel_statistics_update(self) -> None:
        """Cancel and wait for a pending statistics update, if any."""
        task = self._statistics_task
        self._statistics_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def update_statistics(self) -> None:
        """Update statistics with throttling."""
        # Prevent concurrent updates from multiple sensors
        if self._stats_lock.locked():
            return

        async with self._stats_lock:
            current_time = time.monotonic()
            # Update at most once every 60 seconds
            if current_time - self._last_stats_request < 60:
                return

            self._last_stats_request = current_time
            # Maintenance counters (100-109)
            await self.get_statistics(100, 10)
            await asyncio.sleep(0.3)

            # Extended maintenance (110-119)
            await self.get_statistics(110, 10)
            await asyncio.sleep(0.3)

            # Coffee beverage totals (3000-3009)
            await self.get_statistics(3000, 10)
            await asyncio.sleep(0.3)

            # Request additional coffee totals range
            # Covers: 3077-3080 (3077 is combined with 3000 for total coffee)
            await self.get_statistics(3077, 4)
            await asyncio.sleep(0.3)

            # Request cold milk, choco and tea statistics
            # Covers: 3017-3026 (3017=cold milk, 3021=choco, 3025=tea)
            await self.get_statistics(3017, 10)
            await asyncio.sleep(0.3)

    async def get_statistics(self, start_index: int, count: int) -> None:
        """Get statistics from the machine"""
        message = copy.deepcopy(BYTES_STATISTICS_COMMAND)
        message[4] = (start_index >> 8) & 0xFF
        message[5] = start_index & 0xFF
        message[6] = count

        # Background polling: never block waiting for an advertisement.
        await self.send_command(message, wait_for_device=False)
