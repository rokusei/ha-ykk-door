"""Bleak-based BLE transport for the SCK lock characteristic.

A single GATT characteristic (a4370001-…) handles both request writes and
response notifications. Each protocol command is a synchronous round-trip:
write → wait for one notification → parse.

The BLE host running this must already have an LE bond to the lock. Bonding
happens at the OS level (BlueZ on Linux, Android BluetoothManager on
Android). Re-pairing requires putting the physical lock back into
registration mode.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .frames import CHARACTERISTIC_UUID, SERVICE_UUID, parse_frame


class SCKTransport:
    """Single-characteristic request/response over GATT notify."""

    def __init__(self, device: BLEDevice | str, *, response_timeout: float = 5.0):
        self._target = device
        self._timeout = response_timeout
        self._client: BleakClient | None = None
        self._notify_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def __aenter__(self) -> "SCKTransport":
        self._client = BleakClient(self._target, timeout=10.0)
        await self._client.connect()
        await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.stop_notify(CHARACTERISTIC_UUID)
            except Exception:
                pass
            await self._client.disconnect()
        self._client = None

    def _on_notify(self, _char, data: bytearray) -> None:
        self._notify_queue.put_nowait(bytes(data))

    async def write_and_wait(self, frame: bytes) -> bytes:
        """Write `frame` to the characteristic and return the next notification
        body (CRC-verified, with the CRC bytes stripped)."""
        if self._client is None:
            raise RuntimeError("transport not entered")
        # Drain any stale notifications before we send a new request
        while not self._notify_queue.empty():
            self._notify_queue.get_nowait()
        await self._client.write_gatt_char(CHARACTERISTIC_UUID, frame, response=True)
        try:
            data = await asyncio.wait_for(self._notify_queue.get(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"no notification within {self._timeout}s for write {frame.hex()}"
            ) from e
        return parse_frame(data)


@asynccontextmanager
async def open_transport(device: BLEDevice | str) -> AsyncIterator[SCKTransport]:
    async with SCKTransport(device) as t:
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
