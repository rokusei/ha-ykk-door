"""Config flow for YKK Smart Control Key.

Four-step setup:

1. **bluetooth discovery / user pick** — HA's bluetooth integration
   auto-discovers SCK adverts by service UUID and invokes
   ``async_step_bluetooth``; the manual flow lists nearby SCK devices.
2. **adapters** — choose which scanner handles the long-range
   (state-listening) role and which handles the short-range (GATT
   read/write) role. Each defaults to ``auto``. You don't have to commit
   to a specific scanner here — the options flow lets you change later
   (e.g. once an ESPHome ``bluetooth_proxy`` near the door comes online).
3. **register** — user picks a PIN (will be set on the lock), then is told
   to press the physical button on the lock to enter registration mode.
   Submitting runs the GATT registration handshake — this is what captures
   the per-lock ``AdvDataKey`` the integration needs to decode adverts.
4. (config entry created)

The underlying sckey library uses bleak, which has no Android backend. We
guard against running on Android and surface a clear error; HA Core itself
doesn't run on Android, but the guard documents the constraint and protects
unusual install paths.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_ADDRESS,
    CONF_ADV_DATA_KEY,
    CONF_LOCK_ID,
    CONF_LONG_RANGE_SOURCE,
    CONF_NAME,
    CONF_PIN,
    CONF_SHORT_RANGE_SOURCE,
    CONF_SMARTPHONE_ID,
    DEFAULT_NAME,
    DOMAIN,
    SOURCE_AUTO,
)
from .sckey.advertising import COMPANY_ID
from .sckey.client import SCKClient
from .sckey.frames import SERVICE_UUID
from .sckey.transport import SCKTransport

_LOGGER = logging.getLogger(__name__)


def _is_android() -> bool:
    """sckey -> bleak has no Android backend. Refuse setup if we're on it."""
    return hasattr(sys, "getandroidapilevel")


_MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}$")


def _gap_name(info: BluetoothServiceInfoBleak | None) -> str | None:
    """Return the lock's advertised GAP name, or None if only an address is known.

    HA's BluetoothServiceInfoBleak.name falls back to the address when no
    local name has been received in adverts (e.g. when the only scanner
    hearing the lock is a passive proxy that never captures the scan
    response). Treat that as "no name" so callers can fall back to
    DEFAULT_NAME instead of writing the MAC to the lock during
    REGISTER_NAME.
    """
    if info is None:
        return None
    # Prefer the explicit advertised local name if present.
    adv = getattr(info, "advertisement", None)
    local = getattr(adv, "local_name", None) if adv is not None else None
    if local and not _MAC_PATTERN.match(local):
        return local
    name = info.name
    if name and not _MAC_PATTERN.match(name):
        return name
    return None


def _is_sck(service_info: BluetoothServiceInfoBleak) -> bool:
    if SERVICE_UUID in service_info.service_uuids:
        return True
    if COMPANY_ID in service_info.manufacturer_data:
        return True
    return (service_info.name or "").upper().startswith("SCK")


def _parse_adv_data_key(raw: str) -> bytes | None:
    """Accept either 32 hex chars (separators allowed) or 16 ASCII bytes.

    Returns the 16-byte key, or ``None`` on parse failure. Hex form wins
    when the input has no whitespace/separators and is exactly 32 hex
    chars; otherwise we fall back to raw bytes if the input encodes to
    exactly 16 bytes.
    """
    stripped = "".join(c for c in raw if c not in " :-")
    try:
        candidate = bytes.fromhex(stripped)
        if len(candidate) == 16:
            return candidate
    except ValueError:
        pass
    encoded = raw.encode("utf-8")
    if len(encoded) == 16:
        return encoded
    return None


def _scanner_choices(
    hass: HomeAssistant, address: str, *, connectable: bool
) -> dict[str, str]:
    """Build a {source: label} mapping of scanners currently seeing `address`.

    ``connectable=True`` for the short-range role (only connectable scanners
    can do GATT); ``connectable=False`` for the long-range role (any scanner
    that hears the advert is fine).
    """
    choices: dict[str, str] = {SOURCE_AUTO: "Auto (best available)"}
    for sd in bluetooth.async_scanner_devices_by_address(
        hass, address, connectable=connectable
    ):
        rssi = sd.advertisement.rssi
        choices[sd.scanner.source] = (
            f"{sd.scanner.name} [{sd.scanner.source}]  {rssi} dBm"
        )
    return choices


class SCKConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a YKK SCK config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str | None = None
        self._long_source: str = SOURCE_AUTO
        self._short_source: str = SOURCE_AUTO

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SCKOptionsFlow()

    # --- discovery -------------------------------------------------------
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a flow initialized by bluetooth discovery."""
        if _is_android():
            return self.async_abort(reason="unsupported_platform")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered[discovery_info.address] = discovery_info
        self._address = discovery_info.address
        self.context["title_placeholders"] = {
            "name": _gap_name(discovery_info) or "SCK lock"
        }
        return await self.async_step_adapters()

    # --- manual user start ----------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an SCK lock from current bluetooth adverts."""
        if _is_android():
            return self.async_abort(reason="unsupported_platform")

        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_adapters()

        current_ids = {
            entry.unique_id for entry in self._async_current_entries(include_ignore=True)
        }
        for info in async_discovered_service_info(self.hass, connectable=False):
            if info.address in current_ids:
                continue
            if _is_sck(info):
                self._discovered[info.address] = info

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        choices = {
            addr: f"{info.name or 'SCK'} ({addr})"
            for addr, info in self._discovered.items()
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(choices)}),
        )

    # --- adapter selection ----------------------------------------------
    async def async_step_adapters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which scanner handles long-range vs short-range roles."""
        assert self._address is not None
        if user_input is not None:
            self._long_source = user_input[CONF_LONG_RANGE_SOURCE]
            self._short_source = user_input[CONF_SHORT_RANGE_SOURCE]
            return await self.async_step_method()

        long_choices = _scanner_choices(self.hass, self._address, connectable=False)
        short_choices = _scanner_choices(self.hass, self._address, connectable=True)
        return self.async_show_form(
            step_id="adapters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LONG_RANGE_SOURCE, default=SOURCE_AUTO
                    ): vol.In(long_choices),
                    vol.Required(
                        CONF_SHORT_RANGE_SOURCE, default=SOURCE_AUTO
                    ): vol.In(short_choices),
                }
            ),
            description_placeholders={"address": self._address},
        )

    # --- method picker (live registration vs manual key entry) ----------
    async def async_step_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between a live GATT registration and pasting credentials.

        Live registration only works when a connectable scanner is within
        ~3 m of the lock and the lock is in registration mode. For users
        who already have a lock's per-device ``AdvDataKey`` (e.g. extracted
        during reverse engineering), the manual path skips the GATT trip
        entirely and gets you straight to a working read-only entity —
        useful while waiting for a near-door ESP32 ``bluetooth_proxy`` to
        be installed.
        """
        if user_input is not None:
            if user_input["method"] == "manual":
                return await self.async_step_manual()
            return await self.async_step_register()
        return self.async_show_form(
            step_id="method",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default="register"): vol.In(
                        {
                            "register": "Live registration (lock must be in registration mode)",
                            "manual": "Manual entry (I already have the AdvDataKey)",
                        }
                    ),
                }
            ),
        )

    # --- manual entry (expert mode) -------------------------------------
    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept the user-supplied AdvDataKey + lock_id + PIN directly.

        Lock and unlock commands still require a connectable short-range
        scanner at runtime; if none is configured yet, the read-only state
        entity will still work — actions will simply error with
        "not reachable" until a near-door scanner comes online.
        """
        errors: dict[str, str] = {}
        assert self._address is not None

        if user_input is not None:
            adv_key = _parse_adv_data_key(user_input[CONF_ADV_DATA_KEY])
            if adv_key is None:
                errors[CONF_ADV_DATA_KEY] = "wrong_length"
                adv_key = b""

            lock_id = user_input[CONF_LOCK_ID].strip()
            if len(lock_id) != 9 or not lock_id.isascii():
                errors[CONF_LOCK_ID] = "invalid_lock_id"

            pin = user_input[CONF_PIN]
            if not pin.isdigit() or len(pin) != 6:
                errors[CONF_PIN] = "invalid_pin"

            if not errors:
                return self.async_create_entry(
                    title=f"YKK Lock {lock_id}",
                    data={
                        CONF_ADDRESS: self._address,
                        CONF_PIN: pin,
                        CONF_NAME: user_input.get(CONF_NAME, DEFAULT_NAME),
                        CONF_LOCK_ID: lock_id,
                        CONF_ADV_DATA_KEY: adv_key.hex(),
                        CONF_SMARTPHONE_ID: "",
                    },
                    options={
                        CONF_LONG_RANGE_SOURCE: self._long_source,
                        CONF_SHORT_RANGE_SOURCE: self._short_source,
                    },
                )

        info = self._discovered.get(self._address)
        gap_name = _gap_name(info) or DEFAULT_NAME
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADV_DATA_KEY): str,
                    vol.Required(CONF_LOCK_ID): str,
                    vol.Required(CONF_PIN, default="111111"): str,
                    vol.Optional(CONF_NAME, default=gap_name): str,
                }
            ),
            errors=errors,
            description_placeholders={"address": self._address},
        )

    # --- registration ----------------------------------------------------
    async def async_step_register(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run the GATT registration handshake to capture the AdvDataKey.

        The lock must be in registration mode (press the physical button on
        the unit) for this to succeed.
        """
        errors: dict[str, str] = {}
        assert self._address is not None

        if user_input is not None:
            pin = user_input[CONF_PIN]
            name = user_input.get(CONF_NAME, DEFAULT_NAME)
            try:
                result = await self._run_registration(pin, name)
            except TimeoutError:
                errors["base"] = "timeout"
            except RuntimeError as err:
                _LOGGER.exception("SCK registration failed: %s", err)
                errors["base"] = "registration_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during SCK registration")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"YKK Lock {result['lock_id']}",
                    data={
                        CONF_ADDRESS: self._address,
                        CONF_PIN: pin,
                        CONF_NAME: name,
                        CONF_LOCK_ID: result["lock_id"],
                        CONF_ADV_DATA_KEY: result["adv_data_key"],
                        CONF_SMARTPHONE_ID: result["smartphone_id"],
                    },
                    options={
                        CONF_LONG_RANGE_SOURCE: self._long_source,
                        CONF_SHORT_RANGE_SOURCE: self._short_source,
                    },
                )

        info = self._discovered.get(self._address)
        gap_name = _gap_name(info) or DEFAULT_NAME
        return self.async_show_form(
            step_id="register",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PIN, default="111111"): str,
                    vol.Optional(CONF_NAME, default=gap_name): str,
                }
            ),
            description_placeholders={
                "address": self._address,
                "name": gap_name,
            },
            errors=errors,
        )

    async def _run_registration(self, pin: str, name: str) -> dict[str, str]:
        """Two-phase GATT registration.

        Phase 1: in the lock's brief post-pair window (~200ms over a BT
        proxy), pipeline the state-changing writes — enter, RegisterPin,
        RegisterName — fire-and-forget. The lock processes whatever
        reaches it before its internal watchdog tears the link down; we
        don't try to collect notification ACKs back in this window because
        we can't reliably squeeze them through.

        Phase 2: after the lock disconnects, reconnect (the bond should
        be reused — observed `LTK USED INSTEAD OF STK` in the proxy logs)
        and read back what we need to operate the lock: verify_pin
        (authenticates + confirms phase 1 committed), lock_id,
        smartphone_id, adv_data_key. The lock is no longer in registration
        mode and the watchdog no longer applies, so these run sequentially
        with no time pressure.

        Returns plain-str fields suitable for storing in the config entry.
        """
        if not pin.isdigit() or len(pin) != 6:
            raise RuntimeError("PIN must be exactly 6 digits")

        ble_device = self._pick_ble_device()
        if ble_device is None:
            raise RuntimeError(
                "Lock is not currently reachable for a GATT connection on the "
                "selected short-range adapter. Move HA's bluetooth adapter (or "
                "a bluetooth_proxy) closer to the lock and try again."
            )

        # === Phase 1: writes inside the registration window
        # skip_notify=True drops the ~86ms start_notify CCCD write that
        # otherwise eats most of the lock's ~243ms post-pair watchdog
        # window before the pipeline-fire even starts. Phase 1 is
        # fire-and-forget so a subscription buys nothing.
        async with SCKTransport(
            ble_device, response_timeout=10.0, skip_notify=True
        ) as transport:
            client = SCKClient(transport)
            await client.register_phase1_writes(pin, name=name)

        # Brief pause: ESPHome proxy needs to free the BLE connection slot
        # after the lock's own disconnect propagates through. ~7s was
        # observed in the 0.1.11 traces; bleak_retry_connector retries
        # internally so this is mostly a courtesy.
        await asyncio.sleep(2.0)

        # Re-resolve in case the BLE address rotated (RPAs do)
        ble_device = self._pick_ble_device() or ble_device

        # === Phase 2: authenticated reads on a fresh connection
        async with SCKTransport(ble_device, response_timeout=10.0) as transport:
            client = SCKClient(transport)
            result = await client.register_phase2_reads(pin)

        return {
            "lock_id": result.lock_id,
            "adv_data_key": result.adv_data_key.hex(),
            "smartphone_id": result.smartphone_id.hex(),
        }

    def _pick_ble_device(self):
        """Return a BLEDevice for ``self._address`` on the selected short-
        range scanner, or None if not currently reachable."""
        if self._short_source == SOURCE_AUTO:
            return bluetooth.async_ble_device_from_address(
                self.hass, self._address, connectable=True
            )
        for sd in bluetooth.async_scanner_devices_by_address(
            self.hass, self._address, connectable=True
        ):
            if sd.scanner.source == self._short_source:
                return sd.ble_device
        return None


class SCKOptionsFlow(OptionsFlow):
    """Lets the user repick scanner sources after install.

    ``self.config_entry`` is supplied by the OptionsFlow base class in
    current HA versions and is a read-only property — do not assign to it
    or override ``__init__`` to accept it.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        address = self.config_entry.data[CONF_ADDRESS]
        long_choices = _scanner_choices(self.hass, address, connectable=False)
        short_choices = _scanner_choices(self.hass, address, connectable=True)

        current_long = self.config_entry.options.get(
            CONF_LONG_RANGE_SOURCE, SOURCE_AUTO
        )
        current_short = self.config_entry.options.get(
            CONF_SHORT_RANGE_SOURCE, SOURCE_AUTO
        )
        # Preserve currently-set sources in the choices even if that scanner
        # isn't actively seeing the lock right now (e.g. a proxy that's
        # offline) — otherwise the form would silently drop the selection.
        long_choices.setdefault(current_long, f"{current_long} (not currently seen)")
        short_choices.setdefault(current_short, f"{current_short} (not currently seen)")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LONG_RANGE_SOURCE, default=current_long
                    ): vol.In(long_choices),
                    vol.Required(
                        CONF_SHORT_RANGE_SOURCE, default=current_short
                    ): vol.In(short_choices),
                }
            ),
        )
