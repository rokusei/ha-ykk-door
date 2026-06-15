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
        """Admin-smartphone (managementPhone) enrollment, sequential.

        Matches the RN app's wire behavior for first-time registration
        (decompiled.js `registerLockStep1/2/3` sagas + the user's
        Android HCI captures in `/media/claude/sckey/captures/`).

        Findings settled v0.1.25 → v0.1.26:
        - 0x8343 EnterRegistrationModeAdminSmartphone is NOT for
          first-time registration. The iOS/Android app DOES NOT send
          it on a freshly-cleared lock (no beep, registration still
          works). It's for ADDING admin smartphones to an
          already-registered lock — triggered from the in-app admin
          menu. The physical button press is what enters reg-mode
          for first registration. v0.1.25 sending it caused the lock
          to disconnect ~400ms after ack, breaking the data exchange.
        - SetTimestamp (0x8102) DOES ack on this lock — captured as
          `01 02 F1 83` in the Android HCI dump. v0.1.17's "never
          acks" observation was from a botched session, not normal
          flow. Sending it first matches both iOS RN saga and the
          captures.

        Wire sequence after pair+notify:
          SetTimestamp           → 01 02
          RequestAdvDataKey      → 00 10 <16-byte key>
          RequestLockId          → 03 42 ...
          RequestSmartphoneId    → 03 41 ...
          RegisterPin            → 03 12 ...  (MUST succeed)
          RegisterName           → 03 22 ...

        SetAppVersion (0x8103) still omitted — only sent in iOS for
        productCode==2 and our lock returns no notify for it.

        Tolerant of read timeouts (SetTimestamp / AdvDataKey / LockId
        / SmartphoneId): if any read times out, we log a warning and
        continue. RegisterPin MUST succeed — without it the bond has
        no smartphone-slot binding and subsequent connections will be
        rejected.
        """
        # 2s per-frame: healthy RTT to the lock is ~120ms; 2s leaves
        # generous slack for queueing/proxy WiFi jitter without
        # multi-minute hangs if the connection has died.
        FRAME_TIMEOUT = 2.0

        adv_data_key: bytes | None = None
        lock_id: str | None = None
        smartphone_id: bytes | None = None

        # --- SetTimestamp first (matches RN saga for managementPhone) ---
        # Best-effort: if the lock doesn't ack on this firmware, we
        # continue. Captured 01-02 ack on `25D053025` confirms it
        # normally does ack.
        try:
            body = await self._send(
                _build_set_timestamp(), timeout=FRAME_TIMEOUT
            )
            self._check_ack(body, 0x01, 0x02)
        except (TimeoutError, ValueError) as e:
            _LOGGER.warning("SetTimestamp did not ack: %s", e)

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
                "Press the physical button again and retry."
            ) from e

        # --- Step2 cont: read current name (iOS does this between PIN and
        # RegisterName — line 287542 of decompiled.js). Without it, the
        # lock silently dropped our RegisterName writes through v0.1.27.
        # The response is the current name in UTF-16-BE, which iOS uses
        # to prefill the UI; we don't care about the value, just that
        # the lock processes it (which transitions it into "ready to
        # accept a new name" state).
        try:
            body = await self._send(
                _build_request_name(), timeout=FRAME_TIMEOUT
            )
            self._check_ack(body, 0x03, 0x23)
        except (TimeoutError, ValueError) as e:
            _LOGGER.warning("RequestName did not return cleanly: %s", e)

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
