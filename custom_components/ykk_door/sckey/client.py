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

    async def register_phase1_writes(
        self, pin: str, name: str = "SCK"
    ) -> None:
        """Phase 1 of two-phase registration: fire the state-changing
        writes inside the lock's ~250ms post-pair registration window.

        Pipelined fire-and-forget — we don't wait for notifications back
        (the lock can only return 2-3 of them in the window anyway, and
        which ones come back is non-deterministic). The proxy queues all
        writes and pushes them out; the lock processes whatever fits
        before it disconnects.

        Frames (in dispatch order — relevant if the lock processes serially):
          enter (0x8343)        → claim admin reg-mode slot, lock beeps
          RequestSmartphoneId   → make the lock allocate our smartphone slot
                                  (required before RegisterPin has somewhere
                                  to write — the RN app's Step1 saga
                                  invariably calls this before Step2's pin)
          RegisterPin           → commit PIN against the allocated slot
          RegisterName          → commit smartphone name

        After this returns the caller must tear down the transport
        (the lock will disc itself anyway around the same time) and
        re-establish a fresh authenticated connection for
        ``register_phase2_reads``.

        Requires the lock to be in registration mode (press the physical
        button) at the time of the call.
        """
        frames = [
            _build_enter_reg_mode(),
            _build_request_smartphone_id(0),
            _build_register_pin(pin),
            _build_register_name(name),
        ]
        await self._t.write_pipeline_fire(frames)

    async def register_phase2_reads(self, pin: str) -> RegistrationResult:
        """Phase 2 of two-phase registration: pull the data we need to
        operate the lock, on a fresh authenticated connection where the
        post-pair watchdog no longer applies.

        Sequence:
          1. request_lock_id — probe that the lock is responsive on this
             reconnected session at all. Times out only if the lock is
             ignoring everything (broader issue than just PIN).
          2. verify_pin — both authenticates this session and confirms
             phase 1's RegisterPin actually committed. Returns False (NG)
             rather than timing out if the PIN is wrong / unset.
          3. request_smartphone_id, request_adv_data_key — fill in the
             rest of the data the integration needs.
        """
        try:
            lock_id = await self.request_lock_id()
        except TimeoutError as e:
            raise RuntimeError(
                "phase 2: lock did not respond to request_lock_id. Bond "
                "may not be reused, or the lock requires another auth "
                "step we are not sending."
            ) from e
        if not await self.verify_pin(pin):
            raise RuntimeError(
                "phase 2: verify_pin returned NG — phase 1's RegisterPin "
                "did not commit. Lock is responsive (got lock_id) but the "
                "PIN we set is not the one the lock knows. Retry "
                "registration."
            )
        smartphone_id = await self.request_smartphone_id()
        adv_data_key = await self.request_adv_data_key()
        return RegistrationResult(
            lock_id=lock_id,
            adv_data_key=adv_data_key,
            smartphone_id=bytes(smartphone_id),
        )
