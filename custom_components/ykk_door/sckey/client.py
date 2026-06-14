"""High-level SCK client that wires command builders to the transport."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import datetime as _dt

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
    lock_id: str
    adv_data_key: bytes
    smartphone_id: bytes


class SCKClient:
    def __init__(self, transport: SCKTransport):
        self._t = transport

    # --- low-level helpers ----------------------------------------------
    async def _send(self, frame: bytes) -> bytes:
        return await self._t.write_and_wait(frame)

    @staticmethod
    def _check_ack(body: bytes, expect_first: int, expect_second: int) -> bytes:
        """Validate response prefix matches the expected (first, second) byte
        pair, return the remaining payload. Used for parsing typed responses."""
        if len(body) < 2 or body[0] != expect_first or body[1] != expect_second:
            raise ValueError(
                f"unexpected response prefix: got {body[:2].hex()} "
                f"expected {expect_first:02X}{expect_second:02X}"
            )
        return body[2:]

    # --- protocol operations --------------------------------------------
    async def set_timestamp(self, when: _dt.datetime | None = None) -> None:
        body = await self._send(_build_set_timestamp(when))
        # ACK: 01 02 (mirrors 81 02 with high bit dropped)
        self._check_ack(body, 0x01, 0x02)

    async def set_app_version(self, version: str = "2.1.1") -> bool:
        """Send SetAppVersion. Returns True if the lock considers the version
        valid (registerLockStep1 checks `validAppVersion !== 0`; if zero the
        app dispatches REGISTER_LOCK_STEP_FAILED)."""
        body = await self._send(_build_set_app_version(version))
        # ACK: 01 03 + validAppVersion byte
        payload = self._check_ack(body, 0x01, 0x03)
        return bool(payload) and payload[0] != 0

    async def request_adv_data_key(self) -> bytes:
        body = await self._send(_build_request_adv_data_key())
        # Response: 00 10 <16 key bytes>
        return self._check_ack(body, 0x00, 0x10)[:16]

    async def request_name(self) -> bytes:
        """Read the lock's currently stored name. Step2 in the app's flow does
        this purely to surface the lock name in the UI — the registration
        succeeds whether or not we use the value."""
        body = await self._send(_build_request_name())
        # ACK: 03 23 + payload (name in UTF-16 LE, length-prefixed)
        return self._check_ack(body, 0x03, 0x23)

    async def register_name(self, name: str) -> None:
        body = await self._send(_build_register_name(name))
        # ACK: 03 22 (mirrors 83 22)
        self._check_ack(body, 0x03, 0x22)

    async def request_lock_id(self) -> str:
        body = await self._send(_build_request_lock_id())
        # Response: 03 42 + 0x82 + 9 ASCII chars + 0x00 trailer (observed)
        payload = self._check_ack(body, 0x03, 0x42)
        # Skip length-marker byte if present
        if payload and payload[0] == 0x82:
            payload = payload[1:]
        # Strip trailing 0x00 padding observed in capture
        ascii_part = payload.split(b"\x00", 1)[0]
        return ascii_part.decode("ascii")

    async def request_smartphone_id(self, slot: int = 0) -> bytes:
        body = await self._send(_build_request_smartphone_id(slot))
        # Response observed: 03 41 + length marker + payload
        return self._check_ack(body, 0x03, 0x41)

    async def verify_pin(self, pin: str) -> bool:
        body = await self._send(_build_verify_pin(pin))
        payload = self._check_ack(body, 0x03, 0x13)
        return bool(payload) and payload[0] == 0x01

    async def register_pin(self, pin: str) -> bool:
        body = await self._send(_build_register_pin(pin))
        # Response observed: 03 12 82 01 01 01 01 01 01 (echo of PIN bytes)
        payload = self._check_ack(body, 0x03, 0x12)
        return bool(payload)

    async def set_state(self, state: LockState, lock_id: str) -> LockState:
        """Send the lock/unlock command, return the commanded state.

        The lock sends two notifications: an ACK (03 03 ACTION) and an
        unsolicited status broadcast (00 01 ACTION) a few ms later. The
        current transport consumes one notify per write — the ACK — and the
        commanded state is what we get back. A future revision could surface
        the status broadcast for stronger confirmation.
        """
        await self._send(_build_set_lock_state(int(state), lock_id))
        return state

    async def lock(self, lock_id: str) -> LockState:
        return await self.set_state(LockState.LOCKED, lock_id)

    async def unlock(self, lock_id: str) -> LockState:
        return await self.set_state(LockState.UNLOCKED, lock_id)

    # --- composite flows -------------------------------------------------
    async def enter_registration_mode_admin(self) -> int:
        """Tell the lock we want to claim its admin-smartphone slot.

        Must be the first frame after pairing while the lock is in its
        physical registration window — until the lock accepts this, every
        other command is silently dropped. Returns the lock's response code
        (1 = OK, 2 = NG, 3 = limit reached).
        """
        body = await self._send(_build_enter_reg_mode())
        payload = self._check_ack(body, 0x03, 0x43)
        return payload[0] if payload else 0

    async def exit_registration_mode_admin(self) -> None:
        body = await self._send(_build_exit_reg_mode())
        self._check_ack(body, 0x03, 0x44)

    async def register(
        self, pin: str, name: str = "SCK", app_version: str = "2.1.1"
    ) -> RegistrationResult:
        """Full admin-smartphone (managementPhone) enrollment in one
        connection. Mirrors the RN app's enterSmartphoneRegistrationMode +
        registerLockStep1/2/3 sagas exactly for `registrationMode ===
        managementPhone` (=1) and `productCode === 2`.

        Wire sequence after pair+notify, all on the same link:
          enter (0x8343)  → ack 03 43 01 (lock beeps)
          SetAppVersion   → ack 01 03, validAppVersion must be non-zero
          SetTimestamp    → ack 01 02
          RequestAdvDataKey → 00 10 <16-byte key>
          RequestLockId   → 03 42 ...
          RequestSmartphoneId → 03 41 ...
          RegisterPin     → 03 12 ...
          RequestName     → 03 23 ... (Step2 — read existing name, discarded)
          RegisterName    → 03 22 ... (Step3 — write the new name)

        Requires the lock to be in registration mode (press the physical
        button) at the time of the call.
        """
        # === Step "action 1" — enterSmartphoneRegistrationMode (lock beeps)
        rc = await self.enter_registration_mode_admin()
        if rc != 1:
            raise RuntimeError(
                f"lock refused admin-smartphone registration (response code {rc})"
            )
        # === Step 1 — registerLockStep1 (managementPhone, productCode=2)
        if not await self.set_app_version(app_version):
            raise RuntimeError(
                f"lock rejected app version {app_version!r} (validAppVersion=0)"
            )
        await self.set_timestamp()
        adv_data_key = await self.request_adv_data_key()
        lock_id = await self.request_lock_id()
        smartphone_id = await self.request_smartphone_id()
        # === Step 2 — registerLockStep2 (registerPin + RequestName-read)
        await self.register_pin(pin)
        await self.request_name()
        # === Step 3 — registerLockStep3 (registerLockName-write)
        await self.register_name(name)
        return RegistrationResult(
            lock_id=lock_id,
            adv_data_key=adv_data_key,
            smartphone_id=bytes(smartphone_id),
        )
