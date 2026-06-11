"""Binary sensors for YKK SCK (battery, inspection-time flag)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SCKCoordinator
from .entity import SCKEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SCKCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            SCKLowBattery(coordinator),
            SCKInspectionDue(coordinator),
        ]
    )


class SCKLowBattery(SCKEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_translation_key = "low_battery"

    def __init__(self, coordinator: SCKCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.lock_id}_low_battery"

    @property
    def available(self) -> bool:
        return self.coordinator.latest is not None

    @property
    def is_on(self) -> bool | None:
        adv = self.coordinator.latest
        return None if adv is None else adv.low_battery_warning


class SCKInspectionDue(SCKEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "inspection_due"

    def __init__(self, coordinator: SCKCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.lock_id}_inspection_due"

    @property
    def available(self) -> bool:
        return self.coordinator.latest is not None

    @property
    def is_on(self) -> bool | None:
        adv = self.coordinator.latest
        return None if adv is None else adv.inspection_time
