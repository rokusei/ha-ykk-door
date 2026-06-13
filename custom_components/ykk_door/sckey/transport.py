"""Bleak-based BLE transport for the SCK lock characteristic.

A single GATT characteristic (a4370001-…) handles both request writes and
response notifications. Each protocol command is a synchronous round-trip:
write → wait for one notification → parse.

The BLE host running this must already have (or be able to establish) an LE
bond to the lock. On Home Assistant this happens automatically via the
bluetooth integration; in standalone use it happens via the OS bond store.

When `bleak_retry_connector` is available (it is on Home Assistant) the
transport uses `establish_connection` which:
  * caches services after first discovery (much faster on reconnect),
  * retries on the "device disconnected during service discovery" race
    that's normal during the lock's brief 30s registration window,
  * works across BT proxies.

For headless/standalone runs, the transport falls back to a plain
`BleakClient` with a longer timeout and `services=[SCK_SERVICE]` hint so
discovery is scoped to one service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .frames import CHARACTERISTIC_UUID, SERVICE_UUID, parse_frame

_LOGGER = logging.getLogger(__name__)

try:
    from bleak_retry_connector import (
        BleakClientWithServiceCache,
        establish_connection,
    )

    _HAS_RETRY_CONNECTOR = True
except ImportError:  # pragma: no cover - HA always has it; standalone may not
    _HAS_RETRY_CONNECTOR = False

try:
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType
    from dbus_fast.service import ServiceInterface, method

    _HAS_DBUS_FAST = True
except ImportError:  # pragma: no cover - present on HA/Linux, absent elsewhere
    _HAS_DBUS_FAST = False


_AGENT_PATH = "/com/ykkdoor/agent"


if _HAS_DBUS_FAST:

    class _PinAgent(ServiceInterface):  # type: ignore[misc]
        """BlueZ pairing agent that answers PIN/passkey requests with a fixed value.

        The SCK lock requires a passkey-authenticated bond before it'll let the
        SCK characteristic's CCCD be written. Bleak's ``pair()`` itself takes
        no passkey argument on BlueZ — BlueZ asks the registered ``Agent1`` for
        one. We register this agent for the duration of pair() and tear it
        down after, so it doesn't shadow any other system agent.
        """

        def __init__(self, pin: str) -> None:
            super().__init__("org.bluez.Agent1")
            self._pin = pin

        @method()
        def Release(self):  # noqa: N802
            return

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F821, N802
            _LOGGER.debug("BlueZ RequestPinCode(%s)", device)
            return self._pin

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821, N802
            return

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F821, N802
            _LOGGER.debug("BlueZ RequestPasskey(%s)", device)
            return int(self._pin)

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821, N802
            return

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821, N802
            return

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821, N802
            return

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821, N802
            return

        @method()
        def Cancel(self):  # noqa: N802
            return


@asynccontextmanager
async def _bluez_pin_agent(pin: str | None) -> AsyncIterator[None]:
    """Register a one-shot BlueZ Agent1 that supplies `pin` for pairing.

    No-op when `pin` is None, when dbus_fast is unavailable, or when the
    system DBus / org.bluez aren't reachable (e.g. non-Linux backends or
    headless test envs).
    """
    if pin is None or not _HAS_DBUS_FAST:
        yield
        return
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as e:  # pragma: no cover - depends on host
        _LOGGER.debug("System DBus unavailable, skipping pairing agent: %s", e)
        yield
        return
    try:
        agent = _PinAgent(pin)
        bus.export(_AGENT_PATH, agent)
        try:
            introspect = await bus.introspect("org.bluez", "/org/bluez")
            proxy = bus.get_proxy_object("org.bluez", "/org/bluez", introspect)
            mgr = proxy.get_interface("org.bluez.AgentManager1")
        except Exception as e:
            _LOGGER.debug("org.bluez AgentManager1 unavailable: %s", e)
            yield
            return
        try:
            await mgr.call_register_agent(_AGENT_PATH, "KeyboardOnly")
        except Exception as e:
            _LOGGER.debug("RegisterAgent failed: %s", e)
            yield
            return
        # RequestDefaultAgent so BlueZ routes passkey prompts to us even when
        # another agent (e.g. HA's bluetooth integration) is also registered.
        # Best-effort: pairing may still succeed via our agent even if this
        # call is refused.
        with contextlib.suppress(Exception):
            await mgr.call_request_default_agent(_AGENT_PATH)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await mgr.call_unregister_agent(_AGENT_PATH)
    finally:
        with contextlib.suppress(Exception):
            bus.unexport(_AGENT_PATH)
        with contextlib.suppress(Exception):
            bus.disconnect()


class SCKTransport:
    """Single-characteristic request/response over GATT notify.

    Parameters
    ----------
    device : BLEDevice or str
        Target. `BLEDevice` (preferred — HA's bluetooth integration supplies
        these) or a MAC address string for standalone use.
    response_timeout : float
        Seconds to wait for one notification per write.
    connect_timeout : float
        Seconds for the GATT connect + service discovery phase. Default 30s
        matches the lock's registration-mode window.
    max_connect_attempts : int
        How many times to retry the connect (registration races can require
        2-3 attempts). Only used when bleak_retry_connector is available.
    device_lookup : callable returning BLEDevice or None
        Optional callback to re-resolve the device on reconnect (used by
        bleak_retry_connector for proxy environments where the RPA may
        rotate between attempts).
    pairing_pin : str or None
        6-digit ASCII PIN to feed BlueZ's pairing agent on RequestPasskey /
        RequestPinCode. Required for first-time bonding (factory default
        111111). After the bond exists pair() short-circuits and the PIN is
        never asked for — passing it on every connect is harmless.
    """

    def __init__(
        self,
        device: BLEDevice | str,
        *,
        response_timeout: float = 5.0,
        connect_timeout: float = 30.0,
        max_connect_attempts: int = 3,
        device_lookup: Callable[[], BLEDevice | None] | None = None,
        pairing_pin: str | None = None,
    ):
        self._target = device
        self._response_timeout = response_timeout
        self._connect_timeout = connect_timeout
        self._max_attempts = max_connect_attempts
        self._device_lookup = device_lookup
        self._pairing_pin = pairing_pin
        self._client: BleakClient | None = None
        self._notify_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def __aenter__(self) -> "SCKTransport":
        if _HAS_RETRY_CONNECTOR and isinstance(self._target, BLEDevice):
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._target,
                name=self._target.name or "SCK",
                disconnected_callback=self._on_disconnect,
                ble_device_callback=self._device_lookup,
                use_services_cache=True,
                max_attempts=self._max_attempts,
            )
            _LOGGER.debug(
                "Connected to %s via bleak_retry_connector",
                self._target.address,
            )
        else:
            self._client = BleakClient(
                self._target,
                timeout=self._connect_timeout,
                services=[SERVICE_UUID],
                disconnected_callback=self._on_disconnect,
            )
            await self._client.connect()
            _LOGGER.debug("Connected via plain BleakClient")
        # The lock requires an authenticated/bonded link to enable
        # notifications on the SCK characteristic (writing the CCCD raises
        # `error=5 Insufficient authentication` otherwise). Pair before
        # start_notify so the bond is established by then. pair() is a
        # no-op if a bond already exists; some backends raise
        # NotImplementedError or pair lazily on the next encrypted op —
        # swallow that and let start_notify surface any remaining issue.
        # SCK locks demand a passkey (factory default 111111). On BlueZ,
        # bleak's pair() takes no PIN arg — BlueZ asks the registered
        # Agent1 for one. _bluez_pin_agent registers a temporary agent
        # that supplies our PIN for the duration of pair().
        async with _bluez_pin_agent(self._pairing_pin):
            try:
                await self._client.pair()
                _LOGGER.debug("Pair complete")
            except (NotImplementedError, BleakError) as e:
                _LOGGER.debug("pair() not effective here, continuing: %s", e)
        await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.stop_notify(CHARACTERISTIC_UUID)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    def _on_disconnect(self, _client: BleakClient) -> None:
        # bleak_retry_connector and the outer write_and_wait coroutine
        # both observe the connection state directly; this is just here so
        # bleak doesn't warn about unset callbacks.
        _LOGGER.debug("SCK disconnected")

    def _on_notify(self, _char, data: bytearray) -> None:
        self._notify_queue.put_nowait(bytes(data))

    async def write_and_wait(self, frame: bytes) -> bytes:
        """Write `frame` to the characteristic and return the next notification
        body (CRC-verified, with the CRC bytes stripped)."""
        if self._client is None:
            raise RuntimeError("transport not entered")
        # Drain any stale notifications before sending a new request
        while not self._notify_queue.empty():
            self._notify_queue.get_nowait()
        await self._client.write_gatt_char(CHARACTERISTIC_UUID, frame, response=True)
        try:
            data = await asyncio.wait_for(
                self._notify_queue.get(), timeout=self._response_timeout
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"no notification within {self._response_timeout}s for write {frame.hex()}"
            ) from e
        return parse_frame(data)


@asynccontextmanager
async def open_transport(
    device: BLEDevice | str,
    **kwargs,
) -> AsyncIterator[SCKTransport]:
    async with SCKTransport(device, **kwargs) as t:
        yield t


async def scan(timeout: float = 6.0) -> list[BLEDevice]:
    """Return BLE devices advertising the SCK service UUID."""
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches: list[BLEDevice] = []
    for device, adv in devices.values():
        uuids = {u.lower() for u in (adv.service_uuids or [])}
        name = (device.name or "").upper()
        if SERVICE_UUID in uuids or name.startswith("SCK"):
            matches.append(device)
    return matches
