"""Command-frame builders for the SCK lock protocol.

All commands are written to the single GATT characteristic; responses arrive
via notify on the same characteristic. Both ends share the framing in
`frames.py`.
"""

import datetime as _dt
from enum import IntEnum

from .frames import build_frame


class Cmd(IntEnum):
    """Observed (opcode << 8) | sub-opcode values. Names match the React
    Native app's command class names where known."""
    REQUEST_ADV_DATA_KEY = 0x8010
    REQUEST_LOCK_ID = 0x8342
    REQUEST_SMARTPHONE_ID = 0x8341
    REQUEST_NAME = 0x8323
    REGISTER_NAME = 0x8322
    REGISTER_PIN = 0x8312
    VERIFY_PIN = 0x8313
    SET_LOCK_STATE = 0x8003       # 3rd byte: 0x01 lock, 0x02 unlock
    SET_TIMESTAMP = 0x8102
    FW_UPDATE_REQUEST_LOCK = 0x8131
    FW_TRANSMISSION_COUNT_LOCK = 0x8132
    FW_SEND_DATA_LOCK = 0x8133


def _two(cmd: Cmd) -> bytes:
    return cmd.to_bytes(2, "big")


def encode_pin(pin: str) -> bytes:
    """111111 -> b'\\x01\\x01\\x01\\x01\\x01\\x01'. PIN must be ASCII digits.

    The wire format encodes each digit as its numeric value in a single byte,
    not as ASCII. So '9' becomes 0x09, not 0x39.
    """
    if not pin.isdigit():
        raise ValueError(f"PIN must be digits, got {pin!r}")
    return bytes(int(d) for d in pin)


def request_lock_id() -> bytes:
    return build_frame(_two(Cmd.REQUEST_LOCK_ID))


def request_smartphone_id(slot: int = 0) -> bytes:
    return build_frame(_two(Cmd.REQUEST_SMARTPHONE_ID) + bytes([slot]))


def verify_pin(pin: str) -> bytes:
    digits = encode_pin(pin)
    # Length marker 0x82 precedes the 6 PIN bytes — matches the capture
    return build_frame(_two(Cmd.VERIFY_PIN) + b"\x82" + digits)


def register_pin(pin: str) -> bytes:
    digits = encode_pin(pin)
    return build_frame(_two(Cmd.REGISTER_PIN) + b"\x82" + digits)


def set_lock_state(action: int, lock_id: str) -> bytes:
    """action: 0x01 = lock, 0x02 = unlock. lock_id is the 9-char ASCII ID
    returned by request_lock_id."""
    if action not in (0x01, 0x02):
        raise ValueError(f"action must be 0x01 or 0x02, got 0x{action:02X}")
    return build_frame(_two(Cmd.SET_LOCK_STATE) + bytes([action, 0x82]) + lock_id.encode("ascii"))


def set_lock(lock_id: str) -> bytes:
    return set_lock_state(0x01, lock_id)


def set_unlock(lock_id: str) -> bytes:
    return set_lock_state(0x02, lock_id)


def set_timestamp(when: _dt.datetime | None = None) -> bytes:
    """Time format observed on the wire: ASCII 'YYYYMMDDHHMM' (12 bytes,
    minute granularity, no seconds) inside a 0x8102 frame."""
    when = when or _dt.datetime.now()
    payload = when.strftime("%Y%m%d%H%M").encode("ascii")
    return build_frame(_two(Cmd.SET_TIMESTAMP) + payload)
