"""Binary sensors for Delonghi Primadonna."""

from homeassistant.components.binary_sensor import (BinarySensorDeviceClass,
                                                    BinarySensorEntity)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .base_entity import DelonghiDeviceEntity
from .const import DOMAIN
from .device import DelongiPrimadonna


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
):
    """Register binary sensor entities for a config entry."""

    delongh_device: DelongiPrimadonna = hass.data[DOMAIN][entry.unique_id]
    async_add_entities(
        [
            DelongiPrimadonnaDescaleSensor(delongh_device, hass),
            DelongiPrimadonnaFilterSensor(delongh_device, hass),
            DelongiPrimadonnaEnabledSensor(delongh_device, hass),
            DelongiPrimadonnaDispensingSensor(delongh_device, hass),
        ]
    )
    return True


class DelongiPrimadonnaEnabledSensor(
    DelonghiDeviceEntity, BinarySensorEntity
):
    """Shows whether the machine is out of standby.

    Deliberately not a RestoreEntity: is_on reads the machine state, so
    there is nothing of its own to restore, and writing a remembered value
    back into the device made it claim a state no frame had reported.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = 'enabled'

    @property
    def icon(self) -> str:
        """Return the icon of the device."""
        if self.device.switches.is_on:
            return 'mdi:coffee-maker-check'
        if self.device.connected:
            return 'mdi:coffee-maker-check-outline'
        return 'mdi:coffee-maker-outline'

    @property
    def native_value(self):
        return self.device.switches.is_on

    @property
    def is_on(self) -> bool:
        return self.device.switches.is_on


class DelongiPrimadonnaDescaleSensor(
    DelonghiDeviceEntity, BinarySensorEntity, RestoreEntity
):
    """
    Shows if the device needs descaling
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = 'descaling'

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == 'on'

    @property
    def native_value(self):
        return self.device.service

    @property
    def is_on(self) -> bool:
        return bool((self.device.service >> 2) % 2)

    @property
    def icon(self):
        result = 'mdi:dishwasher'
        if self.is_on:
            result = 'mdi:dishwasher-alert'
        return result


class DelongiPrimadonnaFilterSensor(
    DelonghiDeviceEntity, BinarySensorEntity, RestoreEntity
):
    """
    Shows if the filter need to be changed
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = 'filter'

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == 'on'

    @property
    def native_value(self):
        return self.device.service

    @property
    def is_on(self) -> bool:
        return bool((self.device.service >> 3) % 2)

    @property
    def icon(self):
        result = 'mdi:filter'
        if self.is_on:
            result = 'mdi:filter-off'
        return result


class DelongiPrimadonnaDispensingSensor(
    DelonghiDeviceEntity, BinarySensorEntity
):
    """On while the machine is dispensing a beverage."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = 'dispensing'
    _attr_icon = 'mdi:coffee-to-go'

    @property
    def is_on(self) -> bool:
        """Return True while a beverage is being dispensed."""
        return self.device.is_dispensing

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the dispensing progress percentage."""
        return {'percentage': self.device.dispensing_percentage}
