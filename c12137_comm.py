"""
c12137_comm.py

Python port of the Hamamatsu C12137 radiation detector USB communication layer.
Uses PyUSB (usb.core / usb.util) to replicate the libusb-0.1 based C API.
"""

import struct
import time
import usb.core
import usb.util

# ── USB identifiers ──────────────────────────────────────────────────────────
USB_VENDOR = 0x0661
USB_PRODUCT = 0x2917
TIMEOUT_MS = 5000

# ── Bulk transfer constants ──────────────────────────────────────────────────
BULK_HEADER = 8
BULK_DATA = 1048
BULK_SIZE = BULK_HEADER + BULK_DATA  # 1056 unsigned‑shorts
ENDPOINT_BULK_IN = 0x82  # EP2 IN

# ── USB control‑request codes ────────────────────────────────────────────────
REQ_RESET_REQUEST = 0x01
REQ_READ_EEPROM = 0x04
REQ_RADIATION_LIMIT = 0x07
REQ_ENERGY_THRESHOLD = 0x0C
REQ_I2C_RXD_REQUEST = 0x22
REQ_SIEVERT_UPDATE_CYCLE = 0x38
REQ_CLEAR_BULK_BUFFER = 0xF0

I2C_REQ_TEMP_A = 3

# ── EEPROM addresses ────────────────────────────────────────────────────────
EEPROM_COMP_LEVEL = 0x0A   # default 30 keV
EEPROM_ENERGY_LOWER = 0x0C  # default 30 keV
EEPROM_ENERGY_UPPER = 0x0E  # default 2000 keV
EEPROM_CONVERT_USV = 0x10   # default 1000

# ── Return codes ─────────────────────────────────────────────────────────────
RDMUSB_SUCCESS = 0
RDMUSB_INVALID_HANDLE = 1
RDMUSB_UNSUCCESS = 2
RDMUSB_INVALID_VALUE = 3
RDMUSB_NOT_UPDATED = 4
RDMUSB_PACKET_ERROR = 5

# ── bmRequestType helpers ────────────────────────────────────────────────────
_CTRL_OUT = usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_OTHER
_CTRL_IN = usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_OTHER


def _swap16(val: int) -> int:
    """Byte‑swap a 16‑bit value (big‑endian ↔ little‑endian)."""
    return ((val >> 8) & 0xFF) | ((val & 0xFF) << 8)


# ── Temperature conversion (LM94021, GS0=0 GS1=0) ──────────────────────────
def _volt_to_celsius(adc_digit: int) -> float:
    """
    LM94021 0–50 °C  (1034–760 mV)
    ADC 0–1250 mV / 16‑bit
    T = −0.00348 × digit + 188.686
    """
    return -0.00348 * float(adc_digit) + 188.686


# ═══════════════════════════════════════════════════════════════════════════════
#  High‑level device wrapper
# ═══════════════════════════════════════════════════════════════════════════════

class C12137Device:
    """Manages a single Hamamatsu C12137 radiation detector over USB."""

    def __init__(self):
        self._dev: usb.core.Device | None = None

    # ── connection ────────────────────────────────────────────────────────────
    def find_and_open(self) -> bool:
        """Find the device on the bus, set configuration, claim interface."""
        self._dev = usb.core.find(idVendor=USB_VENDOR, idProduct=USB_PRODUCT)
        if self._dev is None:
            return False

        # Detach kernel driver if necessary
        try:
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass

        try:
            self._dev.set_configuration()
        except usb.core.USBError:
            pass

        try:
            usb.util.claim_interface(self._dev, 0)
        except usb.core.USBError:
            pass

        return True

    def close(self):
        if self._dev is not None:
            try:
                usb.util.release_interface(self._dev, 0)
            except usb.core.USBError:
                pass
            try:
                usb.util.dispose_resources(self._dev)
            except usb.core.USBError:
                pass
            self._dev = None

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    # ── low‑level helpers ─────────────────────────────────────────────────────

    def _ctrl_out(self, request: int, value: int = 0, index: int = 0):
        """Vendor control‑transfer OUT (no data stage)."""
        self._dev.ctrl_transfer(_CTRL_OUT, request, value, index, None, TIMEOUT_MS)

    def _ctrl_in_u16(self, request: int, value: int = 0, index: int = 0) -> int:
        """Vendor control‑transfer IN returning a single byte‑swapped uint16."""
        buf = self._dev.ctrl_transfer(_CTRL_IN, request, value, index, 2, TIMEOUT_MS)
        raw = buf[0] | (buf[1] << 8)
        return _swap16(raw)

    # ── public API (mirrors the C functions) ──────────────────────────────────

    def reset(self, level: int = 0) -> int:
        """Reset the module.  *level*: 0–2."""
        try:
            self._ctrl_out(REQ_RESET_REQUEST, level)
            time.sleep(0.1)
            return RDMUSB_SUCCESS
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS

    # -- energy threshold ----------------------------------------------------
    def get_energy_threshold(self) -> tuple[int, int]:
        """Return (status, threshold_index)."""
        try:
            val = self._ctrl_in_u16(REQ_ENERGY_THRESHOLD)
            return RDMUSB_SUCCESS, val
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS, 0

    def set_energy_threshold(self, index_val: int) -> int:
        try:
            self._ctrl_out(REQ_ENERGY_THRESHOLD, index_val)
            return RDMUSB_SUCCESS
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS

    # -- radiation limits ----------------------------------------------------
    def get_radiation_limit(self, area: int) -> tuple[int, int]:
        """area=0 → lower, area=1 → upper.  Return (status, value)."""
        try:
            val = self._ctrl_in_u16(REQ_RADIATION_LIMIT, 0, area)
            return RDMUSB_SUCCESS, val
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS, 0

    def set_radiation_limit(self, area: int, data: int) -> int:
        try:
            self._ctrl_out(REQ_RADIATION_LIMIT, data, area)
            return RDMUSB_SUCCESS
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS

    # -- bulk data -----------------------------------------------------------
    def clear_bulk_buffer(self) -> int:
        try:
            self._ctrl_out(REQ_CLEAR_BULK_BUFFER)
            return RDMUSB_SUCCESS
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS

    def get_data_and_temperature(self) -> tuple[int, int, int, list[int], float]:
        """
        Returns (status, packet_index, size, data_list, temperature).

        The bulk packet is 1056 × uint16:
          [0‑1] = 0x5A5A  (header magic)
          [2]   = data count
          [4]   = index (increments every 100 ms)
          [5]   = temperature ADC digit
          [8…]  = event data words
        """
        read_bytes = BULK_SIZE * 2  # each entry is uint16
        try:
            raw = self._dev.read(ENDPOINT_BULK_IN, read_bytes, timeout=200)
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS, 0, 0, [], 0.0

        if len(raw) != read_bytes:
            return RDMUSB_UNSUCCESS, 0, 0, [], 0.0

        # Unpack as little‑endian uint16 array
        buf = list(struct.unpack(f"<{BULK_SIZE}H", bytes(raw)))

        # Byte‑swap the 8‑word header (match C code big→little swap)
        for i in range(BULK_HEADER):
            buf[i] = _swap16(buf[i])

        if buf[0] != 0x5A5A or buf[1] != 0x5A5A:
            return RDMUSB_PACKET_ERROR, 0, 0, [], 0.0

        pkt_index = buf[4]
        size = buf[2]
        data = buf[8: 8 + size]
        temperature = _volt_to_celsius(buf[5])

        return RDMUSB_SUCCESS, pkt_index, size, data, temperature

    # -- EEPROM --------------------------------------------------------------
    def read_eeprom(self, address: int) -> tuple[int, int]:
        """Return (status, data)."""
        try:
            val = self._ctrl_in_u16(REQ_READ_EEPROM, address, 0x02)
            return RDMUSB_SUCCESS, val
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS, 0

    # -- internal temperature (via I²C) --------------------------------------
    def get_internal_temperature(self) -> tuple[int, float]:
        """Return (status, celsius)."""
        try:
            self._ctrl_out(REQ_I2C_RXD_REQUEST, I2C_REQ_TEMP_A)
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS, 0.0

        time.sleep(0.1)  # 100 ms wait

        try:
            val = self._ctrl_in_u16(REQ_I2C_RXD_REQUEST)
        except usb.core.USBError:
            return RDMUSB_UNSUCCESS, 0.0

        # Temperature conversion (LM94021)
        mv = float(val) * 1250.0 / 65535.0
        celsius = 0.0 + ((mv - 1034.0) * (50.0 - 0.0)) / (760.0 - 1034.0)
        return RDMUSB_SUCCESS, celsius
