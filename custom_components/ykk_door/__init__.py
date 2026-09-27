"""YKK Smart Control Key (SCK) door-lock integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_ADDRESS, CONF_LOCK_ID, DOMAIN
from .coordinator import SCKCoordinator
from .sckey.commands import sanitize_lock_id

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LOCK, Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up YKK SCK from a config entry."""
    await _async_fix_lock_id(hass, entry)
    coordinator = SCKCoordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_fix_lock_id(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring the stored lock_id and registry IDs in line with a clean lock_id.

    Two cases end up here:
    - versions <= 0.2.0 stored a trailing control char in lock_id (issue #1);
    - lock_id was empty at registration and later backfilled, leaving
      entities on the ``_lock`` / ``(DOMAIN, "")`` IDs built from "".

    Entity unique_ids and the device identifier embed lock_id, so rewrite
    them in place rather than orphaning the old entities.
    """
    stored = entry.data.get(CONF_LOCK_ID, "")
    new = sanitize_lock_id(stored)
    if not new:
        return

    dev_reg = dr.async_get(hass)
    old = stored
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        for domain, ident in device.identifiers:
            if domain == DOMAIN and ident != new:
                old = ident
                dev_reg.async_update_device(
                    device.id, new_identifiers={(DOMAIN, new)}
                )
    if old == new:
        return
    _LOGGER.info("Migrating lock_id %r -> %r", old, new)

    @callback
    def _migrate(ent: er.RegistryEntry) -> dict | None:
        # Every unique_id is f"{lock_id}_{suffix}".
        if ent.unique_id.startswith(f"{old}_"):
            return {"new_unique_id": new + ent.unique_id[len(old):]}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)

    title = entry.title
    if title in (f"YKK Lock {old}", f"YKK Lock ({entry.data.get(CONF_ADDRESS)})"):
        title = f"YKK Lock {new}"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LOCK_ID: new}, title=title
    )


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when scanner-source options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: SCKCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok
