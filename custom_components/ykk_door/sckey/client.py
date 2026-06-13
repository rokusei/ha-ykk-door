"""High-level SCK client that wires command builders to the transport."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import datetime as _dt

from .commands import (
    Cmd,
    enter_registration_mode_admin as _build_enter_reg_mode,
    exit_registration_mode_admin as _build_exit_reg_mode,
    request_lock_id as _build_request_lock_id,
    request_smartphone_id as _build_request_smartphone_id,
    register_pin as _build_register_pin,
    verify_pin as _build_verify_pin,
    set_lock_state as _build_set_lock_state,
    set_timestamp as _build_set_timestamp,
)
from .frames import build_frame
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

    async def register(self, pin: str, name: str = "SCK") -> RegistrationResult:
        """Full admin-smartphone registration handshake. Requires the lock
        to be in registration mode (press the physical button)."""
        # 0. Claim the admin-smartphone slot — the lock ignores everything
        # else until this is accepted.
        rc = await self.enter_registration_mode_admin()
        if rc != 1:
            raise RuntimeError(
                f"lock refused admin-smartphone registration (response code {rc})"
            )
        # 1. Get adv-data key
        body = await self._send(build_frame(Cmd.REQUEST_ADV_DATA_KEY.to_bytes(2, "big")))
        # Response: 00 10 <16 key bytes>
        payload = self._check_ack(body, 0x00, 0x10)
        adv_data_key = payload[:16]
        # 2. Get lock ID
        lock_id = await self.request_lock_id()
        # 3. Get smartphone slot (issued by lock)
        smartphone_id = await self.request_smartphone_id()
        # 4. Register PIN
        await self.register_pin(pin)
        # 5. Register name (UTF-16 LE, observed)
        name_bytes = name.encode("utf-16-le")
        register_name_frame = build_frame(
            Cmd.REGISTER_NAME.to_bytes(2, "big") + name_bytes
        )
        await self._send(register_name_frame)
        return RegistrationResult(
            lock_id=lock_id,
            adv_data_key=adv_data_key,
            smartphone_id=bytes(smartphone_id),
        )
