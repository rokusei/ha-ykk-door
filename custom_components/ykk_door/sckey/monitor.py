"""Read-only ('monitor') mode: passively listen for SCK adverts and emit
decoded state. No GATT connection, works at any range that can receive
adverts.

Use this when:
  - Only state observation is needed (HA passive state entity)
  - The BT host is out of GATT range (through walls, far from the door) but
    still receives the lock's advert packets
  - You want a continuous state feed without the per-action connect overhead

For lock/unlock you need read/write mode — see `sckey.client.SCKClient`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .advertising import COMPANY_ID, DecodedAdv, decode_advertisement


class SCKMonitor:
    """Passive advertisement listener.

    Resolves an SCK lock by either:
      - explicit `address` (preferred, since the lock uses an RPA that the
        host stack resolves via the BLE bond identity), or
      - presence of the SCK manufacturer company ID in the advert.

    Emits a `DecodedAdv` to `on_update` on every successfully decoded advert.
    Decode failures (wrong key, bad checksum) are silently dropped to keep
    the stream usable in noisy RF environments — set `on_decode_error` to
    receive them.
    """

    def __init__(
        self,
        adv_data_key: bytes,
        *,
        address: str | None = None,
        on_update: Callable[[DecodedAdv, BLEDevice, AdvertisementData], None] | None = None,
        on_decode_error: Callable[[str, BLEDevice, AdvertisementData], None] | None = None,
    ):
        if len(adv_data_key) != 16:
            raise ValueError(f"adv_data_key must be 16 bytes, got {len(adv_data_key)}")
        self._key = adv_data_key
        self._address = address.lower() if address else None
        self._on_update = on_update
        self._on_decode_error = on_decode_error
        self._scanner: BleakScanner | None = None
        self._latest: DecodedAdv | None = None
        self._latest_event = asyncio.Event()

    @property
    def latest(self) -> DecodedAdv | None:
        return self._latest

    def _cb(self, device: BLEDevice, adv: AdvertisementData) -> None:
        if self._address and device.address.lower() != self._address:
            return
        if COMPANY_ID not in (adv.manufacturer_data or {}):
            return
        try:
            decoded = decode_advertisement(adv.manufacturer_data, self._key)
        except ValueError as e:
            if self._on_decode_error is not None:
                self._on_decode_error(str(e), device, adv)
            return
        if decoded is None:
            return
        self._latest = decoded
        self._latest_event.set()
        if self._on_update is not None:
            self._on_update(decoded, device, adv)

    async def start(self) -> None:
        if self._scanner is not None:
            return
        self._scanner = BleakScanner(detection_callback=self._cb)
        await self._scanner.start()

    async def stop(self) -> None:
        if self._scanner is not None:
            await self._scanner.stop()
            self._scanner = None

    async def __aenter__(self) -> "SCKMonitor":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def wait_for_next(self, timeout: float = 15.0) -> DecodedAdv:
        """Block until a decoded adv is received, return it. Raises
        TimeoutError if none arrives within `timeout` seconds."""
        self._latest_event.clear()
        try:
            await asyncio.wait_for(self._latest_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"no decodable SCK advertisement within {timeout}s"
            ) from e
        assert self._latest is not None
        return self._latest

    async def stream(self) -> AsyncIterator[DecodedAdv]:
        """Yield decoded adverts as they arrive."""
        while True:
            self._latest_event.clear()
            await self._latest_event.wait()
            assert self._latest is not None
            yield self._latest
