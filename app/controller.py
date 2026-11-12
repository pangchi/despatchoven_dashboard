"""Protocol 3 access over RS-485 Modbus RTU (plus an offline simulator)."""
import logging
import os
import random
import threading
import time

from . import registers as R

log = logging.getLogger(__name__)


class ControllerError(Exception):
    pass


class _Base:
    """Shared read/write logic. Subclasses implement _read_raw / _write_raw."""

    def __init__(self):
        self.lock = threading.RLock()   # one transaction on the bus at a time
        self.info = {}

    # -- low level (implemented by subclass) --
    def _read_raw(self, address, signed):
        raise NotImplementedError

    def _write_raw(self, address, value):
        raise NotImplementedError

    def connect(self):
        pass

    def close(self):
        pass

    # -- helpers --
    def read(self, reg):
        with self.lock:
            return reg.decode(self._read_raw(reg.address, reg.signed))

    def write(self, reg, value, verify=True):
        if not reg.writable:
            raise ControllerError(f"register {reg.base} is not writable")
        raw = reg.encode(value)
        if not (-32768 <= raw <= 65535):
            raise ControllerError("value out of 16-bit range")
        with self.lock:
            self._write_raw(reg.address, raw & 0xFFFF)
            if verify:
                back = self._read_raw(reg.address, reg.signed)
                # command registers may self-clear; only verify plain values
                if back != raw and reg not in (R.PROFILE_CONTROL, R.MODE_SELECT):
                    raise ControllerError(f"write verify failed at {reg.base}: wrote {raw}, read {back}")
        return reg.decode(raw)

    def read_info(self):
        info = {}
        for name, reg in R.INFO_MAP.items():
            info[name] = self.read(reg)
        info["units_label"] = R.UNITS.get(int(info.get("units", 0)), "")
        self.info = info
        return info

    def snapshot(self):
        """Read the full telemetry set. Returns dict of raw-decoded values."""
        data = {}
        with self.lock:
            for name, reg in R.READ_MAP.items():
                data[name] = self.read(reg)
            bits = 0
            for n, addr in R.ALARM_STATUS.items():
                if self._read_raw(addr, False):
                    bits |= 1 << (n - 1)
            data["alarm_bits"] = bits
        return data


class ModbusController(_Base):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.inst = None

    def connect(self):
        import minimalmodbus
        import serial

        with self.lock:
            self.close()
            # Own Serial object, configured in one step: minimalmodbus would otherwise share a cached
            # port object per device name, and one failed reconfiguration would poison later opens.
            port = serial.Serial(
                port=self.cfg.SERIAL_PORT, baudrate=self.cfg.SERIAL_BAUD, bytesize=8,
                parity={"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}[self.cfg.SERIAL_PARITY],
                stopbits=self.cfg.SERIAL_STOPBITS, timeout=self.cfg.SERIAL_TIMEOUT, write_timeout=2.0,
                exclusive=True)
            try:
                inst = minimalmodbus.Instrument(port, self.cfg.MODBUS_ADDRESS, mode=minimalmodbus.MODE_RTU)
            except Exception:
                port.close()
                raise
            inst.clear_buffers_before_each_transaction = True
            inst.close_port_after_each_call = False
            self.inst = inst
            log.info("Opened %s @ %d %s, slave %d", self.cfg.SERIAL_PORT, self.cfg.SERIAL_BAUD,
                     self.cfg.SERIAL_PARITY, self.cfg.MODBUS_ADDRESS)

    def close(self):
        if self.inst is not None:
            try:
                self.inst.serial.close()
            except Exception:
                pass
            self.inst = None

    def _retry(self, fn):
        last = None
        for attempt in range(self.cfg.MODBUS_RETRIES + 1):
            try:
                if self.inst is None:
                    self.connect()
                return fn()
            except Exception as e:  # minimalmodbus raises IOError / ValueError subclasses
                last = e
                time.sleep(0.05 * (attempt + 1))
        raise ControllerError(f"Modbus error: {last}")

    def _read_raw(self, address, signed):
        return self._retry(lambda: self.inst.read_register(address, 0, functioncode=3, signed=signed))

    def _write_raw(self, address, value):
        # value is already masked to 0..65535 (two's complement for negatives)
        self._retry(lambda: self.inst.write_register(address, value, 0, functioncode=6, signed=False))


class SimulatedController(_Base):
    """First-order oven model so the app can be developed without hardware."""

    def __init__(self, cfg=None):
        super().__init__()
        self.regs = {}
        self.pv = 25.0
        self.last = time.monotonic()
        self.profile_t0 = None
        self.segment_s = float(os.getenv("P3_SIM_SEGMENT_S", "120"))
        b = lambda r, v: self.regs.__setitem__(r.address, r.encode(v))
        b(R.READ_MAP["manual_sp"], 100.0)
        b(R.READ_MAP["hl_alarm_value"], 250.0)
        b(R.INFO_MAP["sp_min"], 0.0)
        b(R.INFO_MAP["sp_max"], 260.0)
        self.regs[R.INFO_MAP["units"].address] = 1

    def _set(self, name, v):
        reg = R.READ_MAP[name]
        self.regs[reg.address] = reg.encode(v)

    def _get(self, name):
        reg = R.READ_MAP[name]
        return reg.decode(self.regs.get(reg.address, 0))

    def _step(self):
        now = time.monotonic()
        dt, self.last = now - self.last, now
        mode = self.regs.get(R.MODE_SELECT.address, 4)
        running = False
        sp = self._get("manual_sp")
        cur_mode = {1: 2, 2: 3, 3: 4}.get(mode, 0)
        if cur_mode == 2:
            running = self.regs.get(R.MANUAL_CONTROL.address, 0) == 1
            self._set("manual_status", int(running))
        elif cur_mode == 3:
            running = self.regs.get(R.TIMER_CONTROL.address, 0) == 1
            rem = max(0, self._get("timer_remaining") - dt) if running else self.regs.get(R.TIMER_TIME.address, 0)
            if running and rem <= 0:
                running = False
                self.regs[R.TIMER_CONTROL.address] = 0
            self._set("timer_remaining", int(rem))
            self._set("timer_status", int(running))
        elif cur_mode == 4:
            st = self._get("profile_status")
            running = st in (1, 2)
            if st in (1, 2) and self.profile_t0 is not None:
                if st == 2:
                    self.profile_t0 += dt          # held: profile time stands still, SP stays put
                t = now - self.profile_t0          # demo profile: ramp to 150, then dwell (P3_SIM_SEGMENT_S each)
                seg = self.segment_s
                sp = min(150.0, 25 + t * 125 / seg)
                self._set("active_segment", 1 if t < seg else 2)
                self._set("segment_type", 1 if t < seg else 3)
                self._set("profile_remaining", int(max(0, 2 * seg - t)))
                if st == 1 and t >= 2 * seg:
                    self._set("profile_status", 6)
                    running = False
            self._set("working_sp", sp)
        self._set("current_mode", cur_mode)
        if not running:
            self._set("working_sp", sp)
        hl_tripped = self.pv > self._get("hl_alarm_value")
        power = 0.0
        if running and not hl_tripped:
            power = max(0.0, min(100.0, (sp - self.pv) * 8))
        k = max(1.0, 120.0 / self.segment_s)
        self.pv += dt * k * (power * 0.04 - (self.pv - 25.0) * 0.004) + random.uniform(-0.05, 0.05)
        self._set("pv", self.pv)
        self._set("hl_pv", self.pv + random.uniform(-0.3, 0.3))
        self._set("power", int(power))
        self._set("hl_status", 4 if hl_tripped else 0)
        self._set("relay_bits", sum(1 << (i - 1) for i, r in R.MANUAL_RELAYS.items() if self.regs.get(r.address)))

    def _read_raw(self, address, signed):
        self._step()
        v = self.regs.get(address, 0)
        if not signed and v < 0:
            v &= 0xFFFF
        return v

    def _write_raw(self, address, value):
        if value > 32767:
            value -= 65536
        if address == R.PROFILE_CONTROL.address:
            st_reg = R.READ_MAP["profile_status"].address
            if value == 1:
                self.regs[st_reg] = 1
                self.profile_t0 = time.monotonic()
            elif value == 2:
                self.regs[st_reg] = 2
            elif value == 5:
                self.regs[st_reg] = 1
            elif value == 3:
                self.regs[st_reg] = 5
            self.regs[address] = 0
            return
        self.regs[address] = value


def make_controller(cfg):
    if cfg.simulator:
        log.warning("SERIAL_PORT=SIMULATOR - using simulated controller")
        return SimulatedController(cfg)
    return ModbusController(cfg)
