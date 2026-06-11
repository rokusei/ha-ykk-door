"""Diagnostic sensors (firmware, RSSI)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
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
            SCKLockUnitFirmware(coordinator),
            SCKHandleUnitFirmware(coordinator),
            SCKRssi(coordinator),
        ]
    )


class _DiagnosticSensor(SCKEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class SCKLockUnitFirmware(_DiagnosticSensor):
    _attr_translation_key = "lock_unit_fw"

    def __init__(self, coordinator: SCKCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.lock_id}_lock_unit_fw"

    @property
    def native_value(self) -> str | None:
        adv = self.coordinator.latest
        return None if adv is None else adv.lock_unit_fw


class SCKHandleUnitFirmware(_DiagnosticSensor):
    _attr_translation_key = "handle_unit_fw"

    def __init__(self, coordinator: SCKCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.lock_id}_handle_unit_fw"

    @property
    def native_value(self) -> str | None:
        adv = self.coordinator.latest
        return None if adv is None else adv.handle_unit_fw


class SCKRssi(_DiagnosticSensor):
    _attr_translation_key = "rssi"
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT

    def __init__(self, coordinator: SCKCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.lock_id}_rssi"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.last_rssi
