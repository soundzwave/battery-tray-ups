"""
INA219 bidirectional current/voltage/power monitor driver.

Hardware context
----------------
Board  : Waveshare UPS HAT D
I2C bus: 1  (default; override via config/config.yaml → i2c.bus)
Address: 0x43
Shunt  : 0.1 Ω (verify on PCB)

Register map (all 16-bit, big-endian — MSB sent/received first)
---------------------------------------------------------------
0x00  Configuration   R/W  — ADC settings, PGA gain, operating mode
0x01  Shunt Voltage   R    — signed, raw ADC count
0x02  Bus Voltage     R    — unsigned with status bits
0x03  Power           R    — unsigned, requires calibration
0x04  Current         R    — signed, requires calibration
0x05  Calibration     R/W  — sets current/power register scale

Voltage measurement
-------------------
  Bus Voltage register layout:
    bits [15:3] — raw ADC result (13-bit unsigned)
    bit  [2]    — unused
    bit  [1]    — OVF: math overflow in power/current registers
    bit  [0]    — CNVR: conversion ready (auto-cleared on register read)

  Bus voltage (V) = (register >> 3) × 4 mV

  Shunt Voltage register:
    Signed 16-bit two's complement.
    LSB = 10 μV for all PGA settings.
    PGA only changes the analogue full-scale input range:
      PGA /1 → ±40 mV  → valid range ±4000 counts
      PGA /2 → ±80 mV  → valid range ±8000 counts
      PGA /4 → ±160 mV → valid range ±16000 counts
      PGA /8 → ±320 mV → valid range ±32000 counts  ← we use this

Current and Power measurement
------------------------------
  The INA219 computes current and power internally using the calibration
  register.  You must write CAL before reading Current or Power registers.

  Step 1 — choose Current_LSB (smallest representable current):
    Current_LSB = Max_Expected_Current / 2^15
    (2^15 because the current register is 15-bit + sign)

  Step 2 — compute calibration register value:
    CAL = trunc(0.04096 / (Current_LSB × R_shunt))
    The constant 0.04096 V is defined in INA219 datasheet §8.5.1.

  Step 3 — account for integer truncation of CAL:
    Actual_Current_LSB = 0.04096 / (CAL × R_shunt)
    (avoids systematic offset from rounding)

  Step 4 — read registers:
    Current (A) = Current_Register × Actual_Current_LSB
    Power (W)   = Power_Register   × 20 × Actual_Current_LSB
    (the ×20 factor is fixed hardware in INA219, per datasheet Table 3)

Example with R_shunt=0.1 Ω, Max=3.2 A
  Current_LSB = 3.2 / 32768 ≈ 97.66 μA
  CAL         = trunc(0.04096 / (9.766e-5 × 0.1)) = trunc(4194.3) = 4194
  Actual_LSB  = 0.04096 / (4194 × 0.1) ≈ 97.66 μA   (unchanged here)
  Power_LSB   = 20 × 97.66 μA ≈ 1.953 mW

I2C byte order
--------------
  INA219 sends MSB first.  smbus2.read_word_data() on Linux returns bytes
  in little-endian order (low address byte first), so use
  read_i2c_block_data(..., 2) and reconstruct manually to avoid swapping.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import smbus2

logger = logging.getLogger(__name__)

# ── Register addresses ────────────────────────────────────────────────────────
_REG_CONFIG      = 0x00
_REG_SHUNT_VOLT  = 0x01
_REG_BUS_VOLT    = 0x02
_REG_POWER       = 0x03
_REG_CURRENT     = 0x04
_REG_CALIBRATION = 0x05

# ── Configuration register bit-fields ─────────────────────────────────────────
#  Bit 15   : RST   — software reset (self-clearing)
#  Bit 13   : BRNG  — bus voltage range  0=16 V, 1=32 V
#  Bits 12:11: PG   — PGA gain  00=/1, 01=/2, 10=/4, 11=/8
#  Bits 10:7 : BADC — bus ADC resolution / averaging
#  Bits 6:3  : SADC — shunt ADC resolution / averaging
#  Bits 2:0  : MODE — operating mode
_CFG_BRNG_32V    = 0x2000   # 32 V bus range (more than enough for LiPo)
_CFG_PG_8        = 0x1800   # PGA /8 → ±320 mV shunt, covers ±3.2 A @ 0.1 Ω
_CFG_BADC_128S   = 0x0780   # 128-sample average, 68.1 ms conversion
_CFG_SADC_128S   = 0x0078   # 128-sample average, 68.1 ms conversion
_CFG_MODE_CONT   = 0x0007   # Continuous shunt + bus conversion

# Assembled default: 0x3FFF
_DEFAULT_CONFIG = _CFG_BRNG_32V | _CFG_PG_8 | _CFG_BADC_128S | _CFG_SADC_128S | _CFG_MODE_CONT

# Bus voltage register status bits (after applying to raw value, not after shift)
_BV_OVF_BIT  = 0x0001   # math overflow in power/current registers
_BV_CNVR_BIT = 0x0002   # conversion ready (informational, we poll on timer)


@dataclass(frozen=True, slots=True)
class INA219Reading:
    bus_voltage_v:   float   # V    — battery/load side voltage
    shunt_voltage_mv: float  # mV   — drop across shunt (sign = current direction)
    current_ma:      float   # mA   — positive into load (check invert_current if wrong)
    power_mw:        float   # mW   — always positive
    overflow:        bool    # True if INA219 power/current math overflowed
    is_valid:        bool
    error: Optional[str]

    @classmethod
    def invalid(cls, reason: str) -> "INA219Reading":
        return cls(
            bus_voltage_v=0.0, shunt_voltage_mv=0.0,
            current_ma=0.0, power_mw=0.0,
            overflow=False, is_valid=False, error=reason,
        )


class INA219:
    """
    Thread-safe INA219 driver with automatic reconnect on I2C failure.

    Design notes
    ------------
    - All public methods acquire self._lock; safe to call from any thread.
    - Retry uses exponential backoff; each failed attempt attempts a full
      bus reconnect so a glitch in the I2C clock line can recover.
    - The calibration register is rewritten on every reconnect because
      some boards lose register state after power glitches.
    """

    def __init__(
        self,
        bus_number: int = 1,
        address:    int = 0x43,
        r_shunt_ohm:   float = 0.1,
        max_current_a: float = 3.2,
        retry_count:   int   = 3,
        retry_delay_s: float = 0.1,
    ) -> None:
        self._bus_number   = bus_number
        self._address      = address
        self._r_shunt      = r_shunt_ohm
        self._retry_count  = retry_count
        self._retry_delay  = retry_delay_s
        self._bus: Optional[smbus2.SMBus] = None
        self._lock = threading.Lock()

        self._cal_value, self._current_lsb_a, self._power_lsb_w = (
            self._compute_calibration(max_current_a, r_shunt_ohm)
        )
        logger.debug(
            "INA219 calibration: cal=0x%04X current_lsb=%.6f A power_lsb=%.6f W",
            self._cal_value, self._current_lsb_a, self._power_lsb_w,
        )

    # ── Calibration ──────────────────────────────────────────────────────────

    @staticmethod
    def _compute_calibration(
        max_current_a: float, r_shunt_ohm: float
    ) -> tuple[int, float, float]:
        if max_current_a <= 0 or r_shunt_ohm <= 0:
            raise ValueError("max_current_a and r_shunt_ohm must be positive")

        raw_lsb = max_current_a / 32768.0
        cal = int(0.04096 / (raw_lsb * r_shunt_ohm))
        if cal == 0:
            raise ValueError(
                f"Calibration=0: max_current={max_current_a} A, r_shunt={r_shunt_ohm} Ω — "
                "check configuration values"
            )

        # Recompute LSB from truncated CAL to eliminate rounding bias
        actual_lsb = 0.04096 / (cal * r_shunt_ohm)
        power_lsb  = 20.0 * actual_lsb
        return cal, actual_lsb, power_lsb

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def connect(self) -> bool:
        with self._lock:
            return self._connect_locked()

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()

    def _connect_locked(self) -> bool:
        self._close_locked()
        try:
            self._bus = smbus2.SMBus(self._bus_number)
            self._write_reg(_REG_CALIBRATION, self._cal_value)
            self._write_reg(_REG_CONFIG, _DEFAULT_CONFIG)
            logger.info(
                "INA219 connected — bus=%d addr=0x%02X config=0x%04X cal=0x%04X",
                self._bus_number, self._address, _DEFAULT_CONFIG, self._cal_value,
            )
            return True
        except OSError as exc:
            logger.error("INA219 connect failed: %s", exc)
            self._close_locked()
            return False

    def _close_locked(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except OSError:
                pass
            self._bus = None

    # ── Measurement ──────────────────────────────────────────────────────────

    def read(self) -> INA219Reading:
        """
        Read all measurement registers with retry + exponential backoff.

        On I2C failure: attempts a full bus reconnect before each retry so
        transient errors (clock stretching timeout, bus glitch) can recover
        without application restart.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._retry_count):
            try:
                with self._lock:
                    if self._bus is None:
                        if not self._connect_locked():
                            return INA219Reading.invalid("Cannot connect to I2C bus")
                    return self._read_locked()
            except OSError as exc:
                last_exc = exc
                logger.warning(
                    "INA219 read attempt %d/%d: %s",
                    attempt + 1, self._retry_count, exc,
                )
                if attempt < self._retry_count - 1:
                    time.sleep(self._retry_delay * (2 ** attempt))
                    with self._lock:
                        self._connect_locked()

        return INA219Reading.invalid(f"I2C failed after {self._retry_count} retries: {last_exc}")

    def _read_locked(self) -> INA219Reading:
        # ── Bus voltage ───────────────────────────────────────────────────────
        # Register layout: bits[15:3]=ADC result, bit[1]=OVF, bit[0]=CNVR
        # Voltage = (raw >> 3) × 4 mV  → convert to V
        bv_raw = self._read_u16(_REG_BUS_VOLT)
        overflow = bool(bv_raw & _BV_OVF_BIT)
        bus_voltage_v = ((bv_raw >> 3) * 4) / 1000.0

        # ── Shunt voltage ─────────────────────────────────────────────────────
        # Signed 16-bit, LSB = 10 μV (independent of PGA setting)
        # Convert to mV: counts × 10 μV × (1 mV / 1000 μV) = counts × 0.01 mV
        sv_raw = self._read_s16(_REG_SHUNT_VOLT)
        shunt_voltage_mv = sv_raw * 0.01

        # ── Current ───────────────────────────────────────────────────────────
        # Signed 16-bit, scale = Actual_Current_LSB (computed at init)
        cur_raw = self._read_s16(_REG_CURRENT)
        current_ma = cur_raw * self._current_lsb_a * 1000.0

        # ── Power ─────────────────────────────────────────────────────────────
        # Unsigned 16-bit, scale = 20 × Actual_Current_LSB
        pwr_raw = self._read_u16(_REG_POWER)
        power_mw = pwr_raw * self._power_lsb_w * 1000.0

        return INA219Reading(
            bus_voltage_v=bus_voltage_v,
            shunt_voltage_mv=shunt_voltage_mv,
            current_ma=current_ma,
            power_mw=power_mw,
            overflow=overflow,
            is_valid=True,
            error=None,
        )

    # ── Low-level register I/O ────────────────────────────────────────────────

    def _write_reg(self, reg: int, value: int) -> None:
        # INA219 protocol: register byte, then MSB, then LSB
        self._bus.write_i2c_block_data(
            self._address, reg, [(value >> 8) & 0xFF, value & 0xFF]
        )

    def _read_u16(self, reg: int) -> int:
        # INA219 sends MSB first; read_i2c_block_data preserves byte order
        data = self._bus.read_i2c_block_data(self._address, reg, 2)
        return (data[0] << 8) | data[1]

    def _read_s16(self, reg: int) -> int:
        raw = self._read_u16(reg)
        return raw if raw < 0x8000 else raw - 0x10000

    @property
    def is_connected(self) -> bool:
        return self._bus is not None
