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
        self, pin: str, name: str = "SCK"
    ) -> RegistrationResult:
        """Admin-smartphone (managementPhone) enrollment, pipelined.

        Trimmed from the full RN app saga to fit the lock's hard ~200ms
        post-pair window: drops SetAppVersion (productCode=2 only — our
        lock's productCode is unconfirmed, and 0.1.16 traces showed the
        lock never returned a notify for opcode 0103) and SetTimestamp
        (same — no 0102 notify ever observed). The lock also delivers
        notifications out-of-order relative to the write sequence
        (observed 0010 arriving before 0343), so we match responses by
        opcode prefix rather than pipeline index.

        Wire sequence after pair+notify (6 frames, all on the same link):
          enter (0x8343)         → 03 43 01 (lock beeps)
          RequestAdvDataKey      → 00 10 <16-byte key>
          RequestLockId          → 03 42 ...
          RequestSmartphoneId    → 03 41 ...
          RegisterPin            → 03 12 ...
          RegisterName           → 03 22 ...

        Requires the lock to be in registration mode (press the physical
        button) at the time of the call.
        """
        frames = [
            _build_enter_reg_mode(),
            _build_request_adv_data_key(),
            _build_request_lock_id(),
            _build_request_smartphone_id(0),
            _build_register_pin(pin),
            _build_register_name(name),
        ]
        responses = await self._t.write_pipeline(frames)
        # Match responses by opcode prefix — the lock delivers notifies
        # out-of-order relative to write order. Each command has a unique
        # 2-byte response prefix.
        by_prefix = {bytes(r[:2]): r for r in responses}

        def _need(prefix: bytes) -> bytes:
            body = by_prefix.get(prefix)
            if body is None:
                got = ", ".join(p.hex() for p in by_prefix)
                raise RuntimeError(
                    f"missing response for opcode {prefix.hex()} "
                    f"(received: {got or 'none'})"
                )
            return body

        enter_payload = self._check_ack(_need(b"\x03\x43"), 0x03, 0x43)
        rc = enter_payload[0] if enter_payload else 0
        if rc != 1:
            raise RuntimeError(
                f"lock refused admin-smartphone registration (response code {rc})"
            )
        adv_data_key = self._check_ack(_need(b"\x00\x10"), 0x00, 0x10)[:16]
        lid_payload = self._check_ack(_need(b"\x03\x42"), 0x03, 0x42)
        if lid_payload and lid_payload[0] == 0x82:
            lid_payload = lid_payload[1:]
        lock_id = lid_payload.split(b"\x00", 1)[0].decode("ascii")
        smartphone_id = self._check_ack(_need(b"\x03\x41"), 0x03, 0x41)
        self._check_ack(_need(b"\x03\x12"), 0x03, 0x12)  # RegisterPin
        self._check_ack(_need(b"\x03\x22"), 0x03, 0x22)  # RegisterName
        return RegistrationResult(
            lock_id=lock_id,
            adv_data_key=adv_data_key,
            smartphone_id=bytes(smartphone_id),
        )
