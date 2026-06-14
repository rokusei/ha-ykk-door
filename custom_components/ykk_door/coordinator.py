"""Coordinator: passive adv decoder + on-demand GATT lock/unlock.

Two BLE roles with independently selectable adapters:

* **Long-range (read-only)** — passive advertisement listener. Decodes the
  encrypted state blob with the per-lock ``AdvDataKey`` and updates the
  state entity. Works through walls / over a stretched RF link, since the
  advert is broadcast at full power and any scanner that hears it is
  enough.
* **Short-range (read/write)** — full GATT round-trip for lock / unlock.
  Requires a connectable scanner physically near the door (≤ ~3 m).

The user can pin each role to a specific scanner source (a local adapter's
MAC or an ESPHome ``bluetooth_proxy`` identifier), or leave it on ``auto``:

* ``auto`` for the long-range role accepts adverts from **any** scanner
  that hears them.
* ``auto`` for the short-range role lets HA pick the connectable scanner
  with the best current RSSI to the lock (typically the proxy nearest the
  door if one is online).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_ADDRESS,
    CONF_ADV_DATA_KEY,
    CONF_LOCK_ID,
    CONF_LONG_RANGE_SOURCE,
    CONF_PIN,
    CONF_SHORT_RANGE_SOURCE,
    CONF_SMARTPHONE_ID,
    DOMAIN,
    SOURCE_AUTO,
)
from .sckey.advertising import COMPANY_ID, DecodedAdv, LockedState, decode_advertisement
from .sckey.client import LockState, SCKClient
from .sckey.transport import SCKTransport

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = f"{DOMAIN}_update"


class SCKCoordinator:
    """Wires HA's bluetooth integration to the sckey library."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        # Either field may be empty when registration was partial — the
        # lock doesn't always return notifies for every request inside
        # its post-pair window. They get backfilled lazily on the first
        # authenticated GATT session.
        self.lock_id: str = entry.data[CONF_LOCK_ID]
        self._pin: str = entry.data[CONF_PIN]
        adv_hex: str = entry.data[CONF_ADV_DATA_KEY]
        self._adv_key: bytes = bytes.fromhex(adv_hex) if adv_hex else b""

        self.latest: DecodedAdv | None = None
        self.last_rssi: int | None = None
        self.last_source: str | None = None
        self._unsub: CALLBACK_TYPE | None = None
        self._action_lock = asyncio.Lock()

    @property
    def signal(self) -> str:
        return f"{SIGNAL_UPDATE}_{self.entry.entry_id}"

    @property
    def long_range_source(self) -> str:
        return self.entry.options.get(CONF_LONG_RANGE_SOURCE, SOURCE_AUTO)

    @property
    def short_range_source(self) -> str:
        return self.entry.options.get(CONF_SHORT_RANGE_SOURCE, SOURCE_AUTO)

    async def async_start(self) -> None:
        """Register passive bluetooth listener.

        We match on the SCK manufacturer company ID (0x099D) rather than on a
        BLE address. SCK locks advertise with a **Random Private Address**
        that rotates roughly every 15 minutes; an address matcher only fires
        for the cached primer advert and then never again because each new
        RPA looks like a new device to HA's bluetooth manager.

        RPA→identity resolution requires an OS-level BLE bond, which only
        happens when the user goes through the live-registration flow.
        Manual-entry users have no bond, so address matching is unusable for
        them. Matching on manufacturer ID sidesteps the problem entirely;
        adverts from other people's SCK locks (vanishingly unlikely but
        possible in apartments) are filtered out by the decoder, since they
        won't decrypt cleanly with our per-lock AdvDataKey.

        ``connectable=False`` keeps non-connectable scanners (a stretched-
        range proxy etc.) in the eligible set.
        """
        self._unsub = bluetooth.async_register_callback(
            self.hass,
            self._on_adv,
            {"manufacturer_id": COMPANY_ID, "connectable": False},
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_adv(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        # Long-range scanner filter: when pinned to a specific source, ignore
        # the same adv arriving via other scanners (de-dupes overlapping
        # coverage and gives the user a predictable "which adapter is doing
        # the reading?" answer).
        long_source = self.long_range_source
        if long_source != SOURCE_AUTO and service_info.source != long_source:
            return
        try:
            decoded = decode_advertisement(
                service_info.manufacturer_data, self._adv_key
            )
        except ValueError as err:
            # Wrong key / checksum mismatch — almost certainly someone else's
            # SCK lock advertising on the same manufacturer ID. Silently skip.
            _LOGGER.debug(
                "Adv from %s did not decrypt with our key: %s",
                service_info.address,
                err,
            )
            return
        if decoded is None:
            return
        # If multiple SCK locks are configured (or a neighbour's lock happens
        # to share an unrelated AdvDataKey collision), only accept adverts
        # whose decoded lock_id matches the one stored at registration.
        # When lock_id hasn't been backfilled yet, accept any advert that
        # decoded cleanly — the key alone is per-lock so a clean decode is
        # already an identity check.
        if self.lock_id and decoded.lock_id != self.lock_id:
            return
        # Track the current RPA so the GATT path uses the live address, not
        # whatever address was around at config-flow time.
        self.address = service_info.address
        self.latest = decoded
        self.last_rssi = service_info.rssi
        self.last_source = service_info.source
        async_dispatcher_send(self.hass, self.signal)

    # --- actions --------------------------------------------------------
    async def async_set_locked(self, locked: bool) -> None:
        """Connect, verify PIN, set lock state. Updates self.latest optimistically.

        Uses the user's chosen short-range scanner when set, else lets HA
        pick the connectable scanner with the best RSSI.

        On the first call after a partial registration, this also
        backfills any missing credentials (adv_data_key, lock_id,
        smartphone_id) that the lock didn't return notifies for during
        the registration window — reusing the same authenticated session.
        """
        async with self._action_lock:
            ble_device = self._pick_connectable_ble_device()
            if ble_device is None:
                raise RuntimeError(
                    f"Lock {self.address} is not currently reachable for a GATT "
                    "connection on the selected short-range adapter."
                )
            target_state = LockState.LOCKED if locked else LockState.UNLOCKED
            async with SCKTransport(ble_device) as transport:
                client = SCKClient(transport)
                ok = await client.verify_pin(self._pin)
                if not ok:
                    raise RuntimeError("PIN rejected by lock")
                await self._backfill_if_needed(client)
                if not self.lock_id:
                    raise RuntimeError(
                        "lock_id is unknown and could not be read from the lock — "
                        "set_state requires it. Try the registration flow again."
                    )
                await client.set_state(target_state, self.lock_id)
            if self.latest is not None:
                self.latest = _replace_locked(self.latest, target_state)
            async_dispatcher_send(self.hass, self.signal)

    async def async_ensure_credentials(self) -> None:
        """Open an authenticated session and backfill any missing fields.

        Useful for triggering a backfill outside of a lock/unlock action,
        e.g. from a button entity or a setup-time best-effort task. No-op
        if nothing is missing.
        """
        if self._adv_key and self.lock_id and self.entry.data.get(CONF_SMARTPHONE_ID):
            return
        async with self._action_lock:
            if self._adv_key and self.lock_id and self.entry.data.get(
                CONF_SMARTPHONE_ID
            ):
                return
            ble_device = self._pick_connectable_ble_device()
            if ble_device is None:
                raise RuntimeError(
                    f"Lock {self.address} is not currently reachable for a GATT "
                    "connection on the selected short-range adapter."
                )
            async with SCKTransport(ble_device) as transport:
                client = SCKClient(transport)
                ok = await client.verify_pin(self._pin)
                if not ok:
                    raise RuntimeError("PIN rejected by lock")
                await self._backfill_if_needed(client)

    async def _backfill_if_needed(self, client: SCKClient) -> None:
        """Fill in adv_data_key / lock_id / smartphone_id if absent.

        Caller must hold ``_action_lock`` and have already verified the PIN
        on ``client``. Persists any successfully-read values to the config
        entry so the backfill is one-shot.
        """
        new_data: dict | None = None
        if not self._adv_key:
            try:
                key = await client.request_adv_data_key()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("adv_data_key backfill failed: %s", err)
            else:
                self._adv_key = bytes(key)
                new_data = new_data or dict(self.entry.data)
                new_data[CONF_ADV_DATA_KEY] = self._adv_key.hex()
                _LOGGER.info("Backfilled adv_data_key from lock")
        if not self.lock_id:
            try:
                lid = await client.request_lock_id()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("lock_id backfill failed: %s", err)
            else:
                self.lock_id = lid
                new_data = new_data or dict(self.entry.data)
                new_data[CONF_LOCK_ID] = lid
                _LOGGER.info("Backfilled lock_id=%s from lock", lid)
        if not self.entry.data.get(CONF_SMARTPHONE_ID):
            try:
                sid = await client.request_smartphone_id()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("smartphone_id backfill failed: %s", err)
            else:
                new_data = new_data or dict(self.entry.data)
                new_data[CONF_SMARTPHONE_ID] = bytes(sid).hex()
                _LOGGER.info("Backfilled smartphone_id from lock")
        if new_data is not None:
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    def _pick_connectable_ble_device(self):
        short_source = self.short_range_source
        if short_source == SOURCE_AUTO:
            return bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
        for scanner_device in bluetooth.async_scanner_devices_by_address(
            self.hass, self.address, connectable=True
        ):
            if scanner_device.scanner.source == short_source:
                return scanner_device.ble_device
        return None


def _replace_locked(adv: DecodedAdv, new_state: LockState) -> DecodedAdv:
    """Return a copy of `adv` with `locked` swapped to `new_state`."""
    from dataclasses import replace

    return replace(adv, locked=LockedState(int(new_state)))
