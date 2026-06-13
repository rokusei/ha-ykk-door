"""SCK GATT frame format: <body bytes> <CRC16 big-endian>.

CRC is CRC-16/IBM-3740 (a.k.a. CRC-16/AUTOSAR):
poly=0x1021, init=0xFFFF, no reflection, xorout=0xFFFF.
"""

SERVICE_UUID = "a437df7b-60cc-4b5c-98d1-c05e85c88c77"
# The lock exposes two characteristics on the SCK service: notifications come
# in on NOTIFY_UUID; commands must be written to WRITE_UUID. Writing to the
# notify characteristic silently no-ops on the lock.
NOTIFY_UUID = "a4370001-60cc-4b5c-98d1-c05e85c88c77"
WRITE_UUID = "a4370002-60cc-4b5c-98d1-c05e85c88c77"


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def build_frame(body: bytes) -> bytes:
    return body + crc16(body).to_bytes(2, "big")


def parse_frame(frame: bytes) -> bytes:
    if len(frame) < 3:
        raise ValueError(f"frame too short: {frame.hex()}")
    body, crc_bytes = frame[:-2], frame[-2:]
    expected = crc16(body)
    actual = int.from_bytes(crc_bytes, "big")
    if expected != actual:
        raise ValueError(f"CRC mismatch: body={body.hex()} expected={expected:04X} got={actual:04X}")
    return body
