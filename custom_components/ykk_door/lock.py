"""Lock platform for YKK SCK."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SCKCoordinator
from .entity import SCKEntity
from .sckey.advertising import LockedState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SCKCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SCKLock(coordinator)])


class SCKLock(SCKEntity, LockEntity):
    _attr_name = None  # use device name

    def __init__(self, coordinator: SCKCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.lock_id}_lock"

    @property
    def available(self) -> bool:
        return self.coordinator.latest is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        return {
            "long_range_source": self.coordinator.long_range_source,
            "short_range_source": self.coordinator.short_range_source,
            "last_seen_via": self.coordinator.last_source,
        }

    @property
    def is_locked(self) -> bool | None:
        adv = self.coordinator.latest
        if adv is None:
            return None
        if adv.locked == LockedState.LOCKED:
            return True
        if adv.locked == LockedState.UNLOCKED:
            return False
        return None

    async def async_lock(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_locked(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_locked(False)
