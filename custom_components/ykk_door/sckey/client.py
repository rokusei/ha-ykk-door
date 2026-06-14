"""High-level SCK client that wires command builders to the transport."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
import datetime as _dt

_LOGGER = logging.getLogger(__name__)

from .commands import (
    enter_registration_mode_admin as _build_enter_reg_mode,
    exit_registration_mode_admin as _build_exit_reg_mode,
    register_name as _build_register_name,
    register_pin as _build_register_pin,
    request_adv_data_key as _build_request_adv_data_key,
    request_lock_id as _build_request_lock_id,
    request_name as _build_request_name,
    request_smartphone_id as _build_request_smartphone_id,
    set_app_version as _build_set_app_version,
    set_lock_state as _build_set_lock_state,
    set_timestamp as _build_set_timestamp,
    verify_pin as _build_verify_pin,
)
from .transport import SCKTransport


class LockState(IntEnum):
    LOCKED = 0x01
    UNLOCKED = 0x02


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of a registration handshake.

    Any of the read fields may be ``None`` when the lock did not return a
    notification for that opcode inside its ~200ms post-pair window. The
    coordinator backfills missing fields on the first authenticated GATT
    session (see ``SCKCoordinator.async_ensure_credentials``).
    """

    lock_id: str | None
    adv_data_key: bytes | None
    smartphone_id: bytes | None


class SCKClient:
    def __init__(self, transport: SCKTransport):
        self._t = transport

    # --- low-level helpers ----------------------------------------------
    async def _send(self, frame: bytes, *, timeout: float | None = None) -> bytes:
        return await self._t.write_and_wait(frame, timeout=timeout)

    @staticmethod
    def _check_ack(body: bytes, expect_first: int, expect_second: int) -> bytes:
        if len(body) < 2 or body[0] != expect_first or body[1] != expect_second:
            raise ValueError(
                f"unexpected response prefix: got {body[:2].hex()} "
                f"expected {expect_first:02X}{expect_second:02X}"
            )
        return body[2:]

    # --- protocol operations --------------------------------------------
    async def set_timestamp(self, when: _dt.datetime | None = None) -> None:
        body = await self._send(_build_set_timestamp(when))
        self._check_ack(body, 0x01, 0x02)

    async def set_app_version(self, version: str = "2.1.1") -> bool:
        body = await self._send(_build_set_app_version(version))
        payload = self._check_ack(body, 0x01, 0x03)
        return bool(payload) and payload[0] != 0

    async def request_adv_data_key(self) -> bytes:
        body = await self._send(_build_request_adv_data_key())
        return self._check_ack(body, 0x00, 0x10)[:16]

    async def request_name(self) -> bytes:
        body = await self._send(_build_request_name())
        return self._check_ack(body, 0x03, 0x23)

    async def register_name(self, name: str) -> None:
        body = await self._send(_build_register_name(name))
        self._check_ack(body, 0x03, 0x22)

    async def request_lock_id(self) -> str:
        body = await self._send(_build_request_lock_id())
        payload = self._check_ack(body, 0x03, 0x42)
        if payload and payload[0] == 0x82:
            payload = payload[1:]
        ascii_part = payload.split(b"\x00", 1)[0]
        return ascii_part.decode("ascii")

    async def request_smartphone_id(self, slot: int = 0) -> bytes:
        body = await self._send(_build_request_smartphone_id(slot))
        return self._check_ack(body, 0x03, 0x41)

    async def verify_pin(self, pin: str) -> bool:
        body = await self._send(_build_verify_pin(pin))
        payload = self._check_ack(body, 0x03, 0x13)
        return bool(payload) and payload[0] == 0x01

    async def register_pin(self, pin: str) -> bool:
        body = await self._send(_build_register_pin(pin))
        payload = self._check_ack(body, 0x03, 0x12)
        return bool(payload)

    async def set_state(self, state: LockState, lock_id: str) -> LockState:
        await self._send(_build_set_lock_state(int(state), lock_id))
        return state

    async def lock(self, lock_id: str) -> LockState:
        return await self.set_state(LockState.LOCKED, lock_id)

    async def unlock(self, lock_id: str) -> LockState:
        return await self.set_state(LockState.UNLOCKED, lock_id)

    # --- composite flows -------------------------------------------------
    async def enter_registration_mode_admin(self) -> int:
        body = await self._send(_build_enter_reg_mode())
        payload = self._check_ack(body, 0x03, 0x43)
        return payload[0] if payload else 0

    async def exit_registration_mode_admin(self) -> None:
        body = await self._send(_build_exit_reg_mode())
        self._check_ack(body, 0x03, 0x44)

    async def register(
        self, pin: str, name: str = "SCK"
    ) -> RegistrationResult:
        """Admin-smartphone (managementPhone) enrollment, sequential per
        iOS RN app saga (registerLockStep1/2/3, decompiled.js ~287100).

        Two big differences from v0.1.17-0.1.23:

        1. **No EnterRegistrationModeAdminSmartphone (0x8343)**. The
           iOS app's registerLockStep1 doesn't send it for admin
           registration — the physical button press IS the
           reg-mode-enter. Sending 0x8343 burned ~119ms of conn-event
           budget for nothing, leaving the actually-important PIN/Name
           writes to miss the lock's ~200ms post-pair window.

        2. **Sequential writeAndWait** (not pipelined gather +
           write-without-response). Each frame waits for its specific
           ack before the next is sent — so the lock has a chance to
           commit each command, and we know whether PIN actually
           registered.

        Wire sequence after pair+notify, all on one link:
          RequestAdvDataKey      → 00 10 <16-byte key>
          RequestLockId          → 03 42 ...
          RequestSmartphoneId    → 03 41 ...
          RegisterPin            → 03 12 ...  (MUST succeed)
          RegisterName           → 03 22 ...

        SetTimestamp (0x8102) and SetAppVersion (0x8103) are omitted —
        the iOS app sends them but our lock never returns a notify, so
        writeAndWait would hang. (Lock likely silently processes them
        either way; they're not load-bearing for managementPhone.)

        Tolerant of read timeouts (AdvDataKey / LockId / SmartphoneId):
        if any read times out, the field comes back as None and the
        coordinator backfills lazily on the first authenticated
        session. RegisterPin MUST succeed — without it the lock has no
        record of us and would reject subsequent connections.
        """
        # 2s per-frame: healthy RTT to the lock is ~120ms; 2s leaves
        # generous slack for queueing/proxy WiFi jitter without
        # multi-minute hangs if the connection has died.
        FRAME_TIMEOUT = 2.0

        adv_data_key: bytes | None = None
        lock_id: str | None = None
        smartphone_id: bytes | None = None

        # --- Step1-equivalent: reads ---
        try:
            body = await self._send(
                _build_request_adv_data_key(), timeout=FRAME_TIMEOUT
            )
            adv_data_key = self._check_ack(body, 0x00, 0x10)[:16]
        except (TimeoutError, ValueError) as e:
            _LOGGER.warning("RequestAdvDataKey did not return cleanly: %s", e)

        try:
            body = await self._send(
                _build_request_lock_id(), timeout=FRAME_TIMEOUT
            )
            payload = self._check_ack(body, 0x03, 0x42)
            if payload and payload[0] == 0x82:
                payload = payload[1:]
            lock_id = payload.split(b"\x00", 1)[0].decode("ascii")
        except (TimeoutError, ValueError) as e:
            _LOGGER.warning("RequestLockId did not return cleanly: %s", e)

        try:
            body = await self._send(
                _build_request_smartphone_id(0), timeout=FRAME_TIMEOUT
            )
            smartphone_id = bytes(self._check_ack(body, 0x03, 0x41))
        except (TimeoutError, ValueError) as e:
            _LOGGER.warning("RequestSmartphoneId did not return cleanly: %s", e)

        # --- Step2-equivalent: write PIN ---
        # This MUST succeed: it's what binds our bonded peer to a
        # smartphone slot on the lock. Without it, every subsequent
        # auth-required command (verify_pin, set_lock_state) is rejected.
        try:
            body = await self._send(
                _build_register_pin(pin), timeout=FRAME_TIMEOUT
            )
            self._check_ack(body, 0x03, 0x12)
        except TimeoutError as e:
            raise RuntimeError(
                "RegisterPin did not ack within "
                f"{FRAME_TIMEOUT}s. The lock probably disconnected before "
                "processing the PIN write — registration did NOT complete. "
                "Press the physical button again and retry. If this keeps "
                "happening, the post-pair window is too tight for sequential "
                "writes and we need to split into separate connections per "
                "iOS-app Step1/Step2/Step3."
            ) from e

        # --- Step3-equivalent: write name (best-effort, lock has us now) ---
        try:
            body = await self._send(
                _build_register_name(name), timeout=FRAME_TIMEOUT
            )
            self._check_ack(body, 0x03, 0x22)
        except (TimeoutError, ValueError) as e:
            _LOGGER.warning(
                "RegisterName did not ack (registration still succeeded, "
                "lock name will be its previous value): %s",
                e,
            )

        return RegistrationResult(
            lock_id=lock_id,
            adv_data_key=adv_data_key,
            smartphone_id=smartphone_id,
        )
