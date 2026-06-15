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

        Matches the RN app's wire behavior (decompiled.js):
        - `enterRegistrationMode` saga (~286077) sends 0x8343 FIRST,
          on its own. This is what causes the lock to beep and claim
          the admin smartphone slot. v0.1.24 dropped this based on a
          misread of registerLockStep1; the lock then silently failed
          to register us and stopped broadcasting. Confirmed against
          hardware: no 0x8343 = no beep = no registration.
        - `registerLockStep1` (~287105) follows with the data reads
          (AdvDataKey, LockId, SmartphoneId).
        - `registerLockStep2` writes the PIN.
        - `registerLockStep3` writes the smartphone name.

        We do all of these sequentially on one BLE connection (the
        Saga state machine in the app does too — it doesn't reconnect
        between steps).

        Wire sequence after pair+notify:
          enter (0x8343)         → 03 43 01  (MUST succeed; lock beeps)
          RequestAdvDataKey      → 00 10 <16-byte key>
          RequestLockId          → 03 42 ...
          RequestSmartphoneId    → 03 41 ...
          RegisterPin            → 03 12 ...  (MUST succeed)
          RegisterName           → 03 22 ...

        SetTimestamp (0x8102) and SetAppVersion (0x8103) are omitted —
        the RN app sends them but our lock never returns a notify, so
        a writeAndWait would hang. (Lock probably processes them
        silently anyway; not load-bearing for managementPhone.)

        Tolerant of read timeouts (AdvDataKey / LockId / SmartphoneId):
        if any read times out, the field comes back as None and the
        coordinator backfills lazily on the first authenticated
        session. Enter and RegisterPin MUST succeed — without enter
        the lock doesn't claim the slot; without PIN the bond has no
        smartphone-slot binding and subsequent connections will be
        rejected.
        """
        # 2s per-frame: healthy RTT to the lock is ~120ms; 2s leaves
        # generous slack for queueing/proxy WiFi jitter without
        # multi-minute hangs if the connection has died.
        FRAME_TIMEOUT = 2.0

        adv_data_key: bytes | None = None
        lock_id: str | None = None
        smartphone_id: bytes | None = None

        # --- Step 0: claim admin smartphone slot (the beep) ---
        try:
            body = await self._send(
                _build_enter_reg_mode(), timeout=FRAME_TIMEOUT
            )
            payload = self._check_ack(body, 0x03, 0x43)
            rc = payload[0] if payload else 0
            if rc != 1:
                raise RuntimeError(
                    "lock refused EnterRegistrationModeAdminSmartphone "
                    f"(response code {rc}). rc=2 typically means lock "
                    "isn't in reg-mode (press the physical button); "
                    "rc=3 means the admin slot is full and needs the "
                    "current admin to be cleared first."
                )
        except TimeoutError as e:
            raise RuntimeError(
                "EnterRegistrationModeAdminSmartphone did not ack within "
                f"{FRAME_TIMEOUT}s. The lock probably isn't in registration "
                "mode (press the physical button) or isn't reachable. "
                "No beep = lock never received the enter command."
            ) from e

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
