"""Protocol 3 Modbus register map.

Source: Despatch "Protocol 3 Modbus Communications" user's manual (E-106,
PN 320813). The owner's manual (E-105) only refers to that document, so
VERIFY these addresses against your copy of E-106 and your controller's
firmware (Modbus needs firmware 2.3 or later) before relying on control writes.

Data formats on the Protocol 3:
  integer           -> base address
  integer, 1 dp     -> base + 0x4000   (value * 10, signed 16 bit)
  IEEE float        -> base * 2 + 0x8000 (two registers)
This app uses the 1-decimal format for analog values: one register per value,
no word-order ambiguity.
"""

DP1_OFFSET = 0x4000


class Reg:
    def __init__(self, addr, dp1=False, signed=False, writable=False):
        self.base = addr
        self.dp1 = dp1
        self.signed = signed or dp1
        self.writable = writable

    @property
    def address(self):
        return self.base + DP1_OFFSET if self.dp1 else self.base

    def decode(self, raw):
        return raw / 10.0 if self.dp1 else raw

    def encode(self, value):
        return int(round(value * 10)) if self.dp1 else int(value)


# ---- Telemetry (read every poll) ----
READ_MAP = {
    "pv":               Reg(1031, dp1=True),   # process variable
    "sensor_break":     Reg(1032),
    "manual_sp":        Reg(3960, dp1=True, writable=True),  # manual / timer setpoint
    "working_sp":       Reg(8256, dp1=True),   # current (profile) setpoint
    "hl_pv":            Reg(9006, dp1=True),   # high-limit thermocouple
    "hl_status":        Reg(9007),             # bit0 open, bit1 low, bit2 high (tripped)
    "hl_alarm_value":   Reg(9022, dp1=True, writable=True),
    "power":            Reg(4329),             # primary output power %
    "current_mode":     Reg(8280),             # 0 select, 1 config, 2 manual, 3 timer, 4 profile
    "manual_status":    Reg(8281),             # 0 stopped, 1 running
    "timer_status":     Reg(8282),
    "timer_remaining":  Reg(8238),             # s (also segment remaining in profile mode)
    "profile_status":   Reg(8241),             # see PROFILE_STATUS
    "selected_profile": Reg(8243, writable=True),
    "active_segment":   Reg(8244),
    "segment_type":     Reg(8258),
    "profile_remaining": Reg(8236),            # s
    "relay_bits":       Reg(2170),             # bit0..4 relay 1..5
}

# Alarm status registers: alarm block base + 4
ALARM_STATUS = {1: 6148, 2: 6180, 3: 6196, 4: 6212, 5: 6164}

# Read once on connect (and on demand)
INFO_MAP = {
    "sp_min": Reg(3944, dp1=True),
    "sp_max": Reg(3945, dp1=True),
    "units":  Reg(1025),     # 0 none, 1 C, 2 F
}

# ---- Control registers ----
MODE_SELECT = Reg(8285, writable=True)       # 1 manual, 2 timer, 3 profile, 4 mode selection
MANUAL_CONTROL = Reg(8283, writable=True)    # 0 stop, 1 start
TIMER_CONTROL = Reg(8284, writable=True)     # 0 stop, 1 start
TIMER_TIME = Reg(8270, writable=True)        # seconds
PROFILE_CONTROL = Reg(8245, writable=True)
MANUAL_RELAYS = {i: Reg(8248 + i, writable=True) for i in range(1, 5)}  # 8249..8252

MODE_SELECT_VALUES = {"manual": 1, "timer": 2, "profile": 3, "select": 4}
PROFILE_COMMANDS = {"run": 1, "hold": 2, "abort": 3, "next": 4, "release": 5, "exit_hold_sp": 6}

CURRENT_MODE = {0: "mode select", 1: "configuration", 2: "manual", 3: "timer", 4: "profile"}
PROFILE_STATUS = {0: "stopped", 1: "running", 2: "held", 3: "delayed", 4: "error", 5: "aborted", 6: "ended"}
SEGMENT_TYPE = {0: "none", 1: "ramp up", 2: "step", 3: "dwell", 4: "hold", 5: "loop", 6: "join", 7: "end", 8: "ramp down"}
UNITS = {0: "", 1: "°C", 2: "°F"}
