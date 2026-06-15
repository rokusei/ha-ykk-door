"""Parse the SCK lock's BLE advertisement to read real-time state without
opening a GATT connection.

Reverse-engineered from `com.alpha.lockapp` v2.1.1 Hermes bundle 2026-06-10.

Manufacturer-specific data layout (after the 2-byte company ID 0x099D is
stripped by the BLE host stack):

  offset  size  field            notes
  0       1     productCode      plaintext
  1       1     registrationMode plaintext, nonzero while in reg mode
  2       4     lockIdData       plaintext, bit-packed (year:7, month:4,
                                 day:5, serialNumber:16) — encodes the
                                 manufacturing "lot number"
  6       16    encryptedData    AES-128-CBC(key=AdvDataKey, iv=zeros)
                                 = effectively AES-ECB for a single block

Decrypted blob (16 bytes plaintext):

  offset  size  field
  0       1     checksum         XOR of bytes [1..15]
  1       1     productCode      must equal the plaintext productCode
  2       1     locked           0 NA, 1 locked, 2 unlocked
  3       1     connectionRequest        bool
  4       1     connectionRequestDest    int
  5       1     lockUnitFw       hex "XY" -> "X.Y"
  6       1     lockUnitData
  7       1     handleUnitFw
  8       1     inspectionTime           bool
  9       1     lowBatteryWarning        bool
  10-15         padding / reserved
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# Company ID assigned by BLE SIG, advertised in little-endian on the wire.
# The host stack delivers manufacturer_data as a dict keyed by company ID.
COMPANY_ID = 0x099D

# Constants pulled verbatim from the Hermes bundle (.../constants).
IV_ZEROS = b"\x00" * 16
TEMP_KEY_FALLBACK = b"\x01" * 16  # used by the app when AdvDataKey is unknown


class LockedState(IntEnum):
    NA = 0
    LOCKED = 1
    UNLOCKED = 2


@dataclass(frozen=True)
class DecodedAdv:
    product_code: int
    registration_mode: int
    lock_id_year: int
    lock_id_month: int
    lock_id_day: int
    lock_id_serial: int
    locked: LockedState
    connection_request: bool
    connection_request_destination: int
    lock_unit_fw: str
    lock_unit_data: str
    handle_unit_fw: str
    inspection_time: bool
    low_battery_warning: bool

    @property
    def lot_number(self) -> str:
        # The app concatenates year (decimal), month (A..L for 1..12),
        # and day (decimal) to form the human-readable lot number.
        month_letter = chr(ord("A") + self.lock_id_month - 1) if self.lock_id_month else ""
        return f"{self.lock_id_year}{month_letter}{self.lock_id_day:02d}"

    @property
    def lock_id(self) -> str:
        """Full ASCII lock ID as sent in `SetLockCommand` — lot + 4-digit serial."""
        return f"{self.lot_number}{self.lock_id_serial:04d}"


def _aes128_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC decryption of a single 16-byte block. Imported lazily so
    the library only requires `cryptography` when decoding adverts."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _unpack_lock_id(four_bytes: bytes) -> tuple[int, int, int, int]:
    """Bit-unpack the 4-byte lockIdData into (year, month, day, serial).

    The app's processDeviceId flattens each byte LSB-first then reverses to
    MSB-first per byte, then concatenates into a 32-bit big-endian stream
    and slices: year=bits[0..7], month=bits[7..11], day=bits[11..16],
    serial=bits[16..32]. Equivalent to treating the 4 bytes as one big-
    endian 32-bit word and slicing high-to-low.
    """
    if len(four_bytes) != 4:
        raise ValueError(f"lockIdData must be 4 bytes, got {len(four_bytes)}")
    word = int.from_bytes(four_bytes, "big")
    serial = word & 0xFFFF
    day = (word >> 16) & 0x1F
    month = (word >> 21) & 0x0F
    year = (word >> 25) & 0x7F
    return year, month, day, serial


def decode_manufacturer_data(payload: bytes, adv_data_key: bytes) -> DecodedAdv:
    """Decode the manufacturer-specific data payload (with the company-ID
    bytes already stripped).

    `payload` must be exactly 22 bytes (6 plaintext + 16 encrypted).
    `adv_data_key` is the 16-byte key obtained from RequestAdvDataKeyCommand
    during registration.
    """
    if len(payload) < 22:
        raise ValueError(f"expected at least 22 bytes of manufacturer payload, got {len(payload)}")
    # Some adverts carry a trailing connection-request field after the
    # encrypted block. The slot7 structure in the app only reads bytes
    # 0..21; anything after is parsed by a separate (connection) handler
    # we don't decode here.
    if len(adv_data_key) != 16:
        raise ValueError(f"adv_data_key must be 16 bytes, got {len(adv_data_key)}")

    product_code = payload[0]
    registration_mode = payload[1]
    year, month, day, serial = _unpack_lock_id(payload[2:6])
    ciphertext = payload[6:22]

    plaintext = _aes128_cbc_decrypt(ciphertext, adv_data_key, IV_ZEROS)

    checksum = plaintext[0]
    xor_check = 0
    for b in plaintext[1:15]:
        xor_check ^= b
    if checksum != xor_check:
        raise ValueError(
            f"adv checksum mismatch: header=0x{checksum:02X} xor[1..15]=0x{xor_check:02X}"
        )
    if plaintext[1] != product_code:
        raise ValueError(
            f"productCode mismatch between plaintext (0x{product_code:02X}) "
            f"and decrypted ({plaintext[1]:#04x}); wrong adv_data_key?"
        )

    def fw_hex(byte: int) -> str:
        hi, lo = (byte >> 4) & 0xF, byte & 0xF
        return f"{hi}.{lo}"

    return DecodedAdv(
        product_code=product_code,
        registration_mode=registration_mode,
        lock_id_year=year,
        lock_id_month=month,
        lock_id_day=day,
        lock_id_serial=serial,
        locked=LockedState(plaintext[2]),
        connection_request=bool(plaintext[3]),
        connection_request_destination=plaintext[4],
        lock_unit_fw=fw_hex(plaintext[5]),
        lock_unit_data=fw_hex(plaintext[6]),
        handle_unit_fw=fw_hex(plaintext[7]),
        inspection_time=bool(plaintext[8]),
        low_battery_warning=bool(plaintext[9]),
    )


def decode_advertisement(manufacturer_data: dict[int, bytes], adv_data_key: bytes) -> DecodedAdv | None:
    """Convenience wrapper for the `advertisement_data.manufacturer_data` dict
    that BleakScanner delivers — returns None if the SCK company ID isn't
    present in this advert, or if the payload is shorter than the 22-byte
    state-advert block.

    The lock broadcasts two AD variants under the same company ID: a 22-byte
    encrypted state advert (decoded here) and a short connection-request
    advert (typically 1 byte of manufacturer payload, e.g. `0x00`). The
    short variant carries no state — return None so callers can treat it
    as "nothing to do" rather than a decode error.
    """
    payload = manufacturer_data.get(COMPANY_ID)
    if payload is None or len(payload) < 22:
        return None
    return decode_manufacturer_data(payload, adv_data_key)
