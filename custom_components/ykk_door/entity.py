"""Shared base entity for YKK SCK platforms."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import SCKCoordinator


class SCKEntity(Entity):
    """Base entity: device info + signal subscription."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: SCKCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.lock_id)},
            name=f"YKK Lock {coordinator.lock_id}",
            manufacturer="YKK AP",
            model="Smart Control Key",
            connections={("bluetooth", coordinator.address)},
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self.async_write_ha_state
            )
        )
