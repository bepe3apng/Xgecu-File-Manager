"""Read-only auto detection: selection, fallback, identity, STOP and GUI."""
from types import SimpleNamespace
from threading import Event
from modes.emmc.autoconfig import detect
from modes.emmc import protocol as P


class FakeSession:
    def __init__(self, mode="ok"):
        self.preset, self.clock_mhz, self.legacy_30 = "Авто", 30, False
        self.dev = SimpleNamespace(stop_event=Event(), power_off=lambda: None)
        self.mode = mode
        self.attempts = []
        self.logs = []
        self.log = self.logs.append
    def init_hw(self, voltage, width):
        self.attempts.append((self.preset, self.clock_mhz, self.legacy_30))
        if self.mode == "cancel":
            self.dev.stop_event.set()
            raise RuntimeError("cancel")
        if self.mode == "none" or (self.preset == "стандартный" and self.mode != "standard"):
            raise RuntimeError("init failed")
        if self.mode == "fallback" and self.clock_mhz > 16:
            raise RuntimeError("unstable")
        if self.clock_mhz == 30 and not self.legacy_30 and self.mode != "standard":
            raise RuntimeError("HS failed")
    def wake_registers(self, width):
        ext = bytearray(512)
        ext[212:216] = (4096).to_bytes(4, "little")
        cid = b"other" if self.mode == "identity" and self.clock_mhz > 8 else b"cid"
        return cid, b"csd", bytes(ext)
    def select_user(self): pass
    def read_sectors(self, lba, sectors):
        if self.mode == "dirty" and self.clock_mhz > 8:
            return b"b" * (sectors * P.SECTOR_SIZE)
        return b"a" * (sectors * P.SECTOR_SIZE)


for mode, expected in (
        ("ok", ("THGA", 30, True)),
        ("standard", ("стандартный", 30, False)),
        ("fallback", ("THGA", 16, False)),
        ("dirty", ("THGA", 8, False)),
        ("identity", ("THGA", 8, False))):
    s = FakeSession(mode)
    detect(s, True, "4-bit")
    assert (s.preset, s.clock_mhz, s.legacy_30) == expected, (mode, s.attempts)
s = FakeSession()
detect(s, False, "4-bit")
assert s.clock_mhz == 20 and all(clock != 30 for _, clock, _ in s.attempts)
for mode in ("none", "cancel"):
    s = FakeSession(mode)
    try:
        detect(s, True, "4-bit")
    except RuntimeError:
        pass
    else:
        raise AssertionError(mode)
    assert s.preset == "Авто"
    if mode == "cancel":
        assert len(s.attempts) == 1
print("PASS: auto mode ranking, fallback, data/identity mismatch, voltage and STOP")

from kivy.lang import Builder
from kivy.clock import Clock
from gui import MainScreen
Builder.load_file("gui.kv")
screen = MainScreen()
standard = screen.emmc_clock
screen.set_emmc_preset("Авто")
for _ in range(3):
    Clock.tick()
assert P.DEFAULT_PRESET == "Авто"
assert screen.emmc_clock == "Авто" and screen.ids.spin_clock.disabled
assert "Авто" in screen.ids.spin_preset.values
screen.set_emmc_preset("THGA")
assert screen.emmc_clock != "Авто"
screen.set_emmc_preset("стандартный")
assert screen.emmc_clock == standard
print("PASS: auto preset GUI, disabled manual frequency and independent manual settings")
