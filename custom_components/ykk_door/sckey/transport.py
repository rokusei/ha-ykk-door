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
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .frames import NOTIFY_UUID, SERVICE_UUID, WRITE_UUID, parse_frame

_LOGGER = logging.getLogger(__name__)

try:
    from bleak_retry_connector import (
        BleakClientWithServiceCache,
        establish_connection,
    )

    _HAS_RETRY_CONNECTOR = True
except ImportError:  # pragma: no cover - HA always has it; standalone may not
    _HAS_RETRY_CONNECTOR = False


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
    """

    def __init__(
        self,
        device: BLEDevice | str,
        *,
        response_timeout: float = 5.0,
        connect_timeout: float = 30.0,
        max_connect_attempts: int = 3,
        device_lookup: Callable[[], BLEDevice | None] | None = None,
    ):
        self._target = device
        self._response_timeout = response_timeout
        self._connect_timeout = connect_timeout
        self._max_attempts = max_connect_attempts
        self._device_lookup = device_lookup
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
        # The lock requires an encrypted/bonded link to enable notifications
        # on the SCK characteristic (writing the CCCD raises
        # `error=5 Insufficient authentication` otherwise). Pair before
        # start_notify so the bond is established by then. pair() is a no-op
        # if a bond already exists; some backends raise NotImplementedError
        # or pair lazily on the next encrypted op — swallow that and let
        # start_notify surface any remaining issue. The lock accepts a
        # Just-Works bond; the 6-digit PIN is enforced at the SCK protocol
        # layer (verify_pin / register_pin), not at SMP.
        try:
            await self._client.pair()
            _LOGGER.debug("Pair complete")
        except (NotImplementedError, BleakError) as e:
            _LOGGER.debug("pair() not effective here, continuing: %s", e)
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.stop_notify(NOTIFY_UUID)
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
        await self._client.write_gatt_char(WRITE_UUID, frame, response=True)
        try:
            data = await asyncio.wait_for(
                self._notify_queue.get(), timeout=self._response_timeout
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"no notification within {self._response_timeout}s for write {frame.hex()}"
            ) from e
        return parse_frame(data)

    async def write_pipeline(
        self,
        frames: list[bytes],
        *,
        timeout: float | None = None,
        write_response: bool = False,
    ) -> list[bytes]:
        """Dispatch `frames` concurrently, then collect one notification per
        write in order.

        Used for the lock's post-pair registration window (~220ms over a BT
        proxy) which is too tight for any sequential write pattern. Two
        compounding optimisations vs 0.1.14:

        * **asyncio.gather** dispatches all writes in parallel at the API
          layer (the proxy still serialises at BLE, but the per-write WiFi
          round-trip stops being on the critical path).
        * **write_response=False** issues ATT write-without-response, so
          each write returns immediately at the API layer (no BLE-level
          ATT ACK wait). Multiple short writes fit in 1-2 BLE conn events
          as LL data PDUs.

        Risk: if the SCK write characteristic only declares
        write-with-response, write-without-response is silently dropped on
        the lock side — the writes "succeed" at the API layer but the
        lock never sees them. Symptom: zero notifications + no beep. Pass
        `write_response=True` to fall back to the safer mode.

        Responses are matched to frames by index — the lock processes
        commands sequentially per the RN app's saga code, so notify
        arrival order tracks write order.
        """
        if self._client is None:
            raise RuntimeError("transport not entered")
        if timeout is None:
            timeout = self._response_timeout
        # Drain stale notifications
        while not self._notify_queue.empty():
            self._notify_queue.get_nowait()

        loop = asyncio.get_event_loop()
        t_start = loop.time()
        write_results = await asyncio.gather(
            *(
                self._client.write_gatt_char(
                    WRITE_UUID, frame, response=write_response
                )
                for frame in frames
            ),
            return_exceptions=True,
        )
        for i, r in enumerate(write_results):
            if isinstance(r, BaseException):
                raise BleakError(
                    f"pipeline write {i + 1}/{len(frames)} "
                    f"({frames[i][:2].hex()}) failed after "
                    f"{(loop.time() - t_start) * 1000:.0f}ms: {r}"
                ) from r
        t_writes_done = loop.time()
        _LOGGER.debug(
            "Pipeline dispatched %d writes in %.0fms (write_response=%s)",
            len(frames),
            (t_writes_done - t_start) * 1000,
            write_response,
        )

        results: list[bytes] = []
        deadline = t_start + timeout
        for i in range(len(frames)):
            remaining = max(0.0, deadline - loop.time())
            try:
                data = await asyncio.wait_for(
                    self._notify_queue.get(), timeout=remaining
                )
            except asyncio.TimeoutError as e:
                raise TimeoutError(
                    f"pipeline timeout: got {len(results)}/{len(frames)} "
                    f"responses within {timeout}s"
                ) from e
            results.append(parse_frame(data))
        _LOGGER.debug(
            "Pipeline collected %d responses in %.0fms (total %.0fms)",
            len(results),
            (loop.time() - t_writes_done) * 1000,
            (loop.time() - t_start) * 1000,
        )
        return results


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
