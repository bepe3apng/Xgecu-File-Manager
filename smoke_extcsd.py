"""Offline checks for EXT_CSD inspection; no USB device is opened."""
import ast
from pathlib import Path
from unittest.mock import patch

for path in ("gui.py", "orchestrator.py", "modes/emmc_mode.py",
             "modes/emmc/operations.py", "modes/emmc/extcsd.py"):
    ast.parse(Path(path).read_text(encoding="utf-8-sig"))

from modes.emmc.extcsd import validate, describe, compare

data = bytearray(512)
data[192] = 8
data[212:216] = (122142720).to_bytes(4, "little")
data[226] = 32
assert "MMC 5.1" in describe(data)
assert "122142720" in describe(data)
other = bytearray(data)
other[179] = 72
assert "[179] PARTITION_CONFIG: 00 → 48" in compare(data, other)
assert "0 из 512" in compare(data, data)
for invalid in (b"", bytes(511), bytes(513)):
    try:
        validate(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid size accepted")

from modes.emmc import operations


class FakeSession:
    closed = False

    def __init__(self, *args):
        pass

    def open(self):
        return True

    def init_hw(self, *args):
        pass

    def wake_registers(self, *args):
        return b"cid", b"csd", bytes(data)

    def close(self):
        FakeSession.closed = True


with patch.object(operations, "EmmcSession", FakeSession):
    assert operations.read_extcsd(lambda m: None, True, "4-bit") == (bytes(data), b"cid")
    assert FakeSession.closed
    assert operations.read_extcsd(lambda m: None, True, "4-bit", lambda: True) is None
    FakeSession.closed = False
    with patch.object(FakeSession, "wake_registers", side_effect=RuntimeError("test")):
        try:
            operations.read_extcsd(lambda m: None, True, "4-bit")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected read failure")
    assert FakeSession.closed

import gui
from kivy.lang import Builder

Builder.load_file("gui.kv")
screen = gui.MainScreen()
assert not screen.ids.emmc_sub_tabs.has_screen("sub_extcsd")
screen._show_extcsd(data, "test")
assert screen.extcsd_ready
rows = {row.offset: row for row in screen.ids.write_extcsd_table.children}
assert set(rows) == {177, 178, 179, 162, 160}
write_rows = {row.offset: row for row in screen.ids.write_extcsd_table.children}
assert set(write_rows) == {177, 178, 179, 162, 160}
screen.switch_emmc_tab("sub_write")
assert screen.ids.emmc_sub_tabs.current == "sub_write"
assert "btn_read_write_extcsd" not in screen.ids
assert "btn_load_write_extcsd" not in screen.ids
assert "btn_select_extcsd" in screen.ids
with patch.object(screen, "write_extcsd") as write:
    write_rows[177].ids.write_value.dispatch("on_release")
    write.assert_called_once_with(177)
assert write_rows[160].ids.write_value.disabled
for index, row in rows.items():
    assert row.ids.current_value.text == f"{data[index]:02X}"
    row.ids.field_button.dispatch("on_release")
    assert screen.extcsd_write_field == row.field
screen.switch_emmc_tab("sub_read")
assert screen.ids.emmc_sub_tabs.current == "sub_read"
from modes.emmc.extcsd import load_file, validate_write
from modes.emmc import protocol as P
from modes.emmc.session import EmmcSession
from tempfile import TemporaryDirectory

sample = Path(__file__).resolve().parent / "tests" / "fixtures" / "extcsd_sample.bin"
raw = sample.read_bytes()
loaded, tail = load_file(sample)
assert loaded == raw[:512] and tail == raw[512:] and len(tail) == 32
screen._show_extcsd(loaded, "sample", file_tail=tail)
assert screen.extcsd_file_tail == tail and not screen.extcsd_chip_ready
assert "CID: " in screen.extcsd_text
with TemporaryDirectory() as folder:
    p = Path(folder) / "raw.bin"
    p.write_bytes(loaded)
    assert load_file(p) == (loaded, b"")
    reference = bytearray(loaded)
    reference[177] ^= 1
    p.write_bytes(reference)
    with patch.object(screen, "_extcsd_dialog", return_value=str(p)):
        screen.open_extcsd(compare=True)
    from kivy.clock import Clock
    Clock.tick()
    assert rows[177].ids.file_value.text == f"{reference[177]:02X}"
    assert tuple(rows[177].ids.file_value.color) == (1, 0.6, 0.25, 1)
    assert "1 из 512" in screen.extcsd_comparison
    screen._show_extcsd(loaded, "sample", file_tail=tail)
    Clock.tick()
    assert rows[177].ids.file_value.text == "--"
    assert not screen.extcsd_comparison
    p.write_bytes(bytes(543))
    try:
        load_file(p)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid container accepted")

assert P.switch_byte_cmd(185, 0).hex() == P.CMD_LEGACY_TIMING
cid = b"\x07\x00" + bytes(30)
data[178] = 0
data[162] = 0

def rejected(index, value, snapshot=None, otp=False):
    try:
        validate_write(data if snapshot is None else snapshot, index, value, otp)
    except ValueError:
        return
    raise AssertionError((index, value))

for index, value in ((177, 2), (178, 1), (179, 0x48)):
    validate_write(data, index, value)
rejected(160, 7)
rejected(178, 0x10)
rejected(178, 2)
rejected(162, 1)
validate_write(data, 178, 0x10, True)
validate_write(data, 162, 1, True)
protected = bytearray(data)
protected[178] = 0x10
rejected(179, 0x48, protected)
rejected(178, 0, protected)
protected[162] = 1
for old_rst in (0, 1, 2):
    rst_snapshot = bytearray(data)
    rst_snapshot[162] = old_rst
    for new_rst in (0, 1, 2):
        if old_rst != new_rst:
            rejected(162, new_rst, rst_snapshot)
        assert validate_write(rst_snapshot, 162, new_rst, True) == old_rst
rejected(162, 3, otp=True)

class WriteSession(FakeSession):
    current = bytes(data)
    readback = None
    identity = cid
    sent = []
    def wake_registers(self, *args):
        return self.identity, b"csd", self.current
    def write_extcsd_byte(self, index, value, delay):
        self.sent.append((index, value))
        changed = bytearray(self.current)
        changed[index] = value
        self.current = bytes(changed)
    def read_extcsd(self):
        return self.current if self.readback is None else self.readback

with patch.object(operations, "EmmcSession", WriteSession):
    result = operations.write_extcsd(lambda m: None, True, "4-bit",
                                    179, 0x48, bytes(data), cid)
    assert result[0][179] == 0x48 and WriteSession.sent == [(179, 0x48)]
    for failure in ("cid", "stale", "verify", "cancel"):
        WriteSession.sent.clear()
        WriteSession.identity = b"wrong" if failure == "cid" else cid
        WriteSession.current = bytes(protected) if failure == "stale" else bytes(data)
        if failure == "stale":
            altered = bytearray(data)
            altered[179] = 8
            WriteSession.current = bytes(altered)
        WriteSession.readback = bytes(data) if failure == "verify" else None
        if failure == "cancel":
            assert operations.write_extcsd(lambda m: None, True, "4-bit",
                179, 0x48, bytes(data), cid, lambda: True) is None
        elif failure == "verify":
            errors = []
            actual, _ = operations.write_extcsd(errors.append, True, "4-bit",
                                               179, 0x48, bytes(data), cid)
            assert actual == bytes(data)
            assert any("Ошибка верификации" in message for message in errors)
            assert not any("подтверждена" in message for message in errors)
        else:
            try:
                operations.write_extcsd(lambda m: None, True, "4-bit",
                    179, 0x48, bytes(data), cid)
            except RuntimeError:
                pass
            else:
                raise AssertionError(failure)
        assert len(WriteSession.sent) == (1 if failure == "verify" else 0)

# Confirmed RST_n changes are attempted; a chip retaining its OTP value fails verification.
rst_before = bytearray(data)
rst_before[162] = 1
WriteSession.identity = cid
for requested in (0, 2):
    WriteSession.current, WriteSession.readback = bytes(rst_before), None
    WriteSession.sent.clear()
    with patch.object(operations, "EmmcSession", WriteSession):
        actual, _ = operations.write_extcsd(
            lambda m: None, True, "4-bit", 162, requested, rst_before, cid, allow_otp=True)
    assert actual[162] == requested and WriteSession.sent == [(162, requested)]
    WriteSession.current = WriteSession.readback = bytes(rst_before)
    with patch.object(operations, "EmmcSession", WriteSession):
        errors = []
        actual, _ = operations.write_extcsd(errors.append, True, "4-bit",
                                           162, requested, rst_before, cid, allow_otp=True)
        assert actual[162] == 1
        assert any("Ошибка верификации EXT_CSD [162]" in message for message in errors)
        assert not any("подтвержден" in message for message in errors)
WriteSession.readback = None

screen.ids.emmc_sub_tabs.transition.stop()
screen._show_extcsd(data, "chip", cid)
from kivy.clock import Clock
Clock.tick()
assert "spin_extcsd_field" not in screen.ids
for index, row in rows.items():
    assert row.ids.write_value.disabled == (index == 160)
    assert row.ids.new_value.disabled == (index == 160)

with TemporaryDirectory() as folder:
    p = Path(folder) / "ECSD_CSD.BIN"
    imported = bytearray(data)
    imported[177] = 2
    imported[179] = 0x48
    p.write_bytes(imported + bytes(32))
    snapshot = screen.extcsd_data
    with patch.object(screen, "_extcsd_dialog", return_value=str(p)):
        screen.open_extcsd()
    Clock.tick()
    assert screen.extcsd_data == snapshot and screen.extcsd_chip_cid == cid
    assert screen.extcsd_chip_ready
    assert rows[177].ids.current_value.text == f"{snapshot[177]:02X}"
    assert rows[177].ids.new_value.text == "02"
    assert rows[179].ids.new_value.text == "48"
    assert write_rows[177].ids.new_value.text == "02"
    assert write_rows[179].ids.new_value.text == "48"
    assert not write_rows[179].ids.write_value.disabled
    rows[177].ids.new_value.text = "01"
    assert screen.extcsd_drafts[177] == "01"
    with patch.object(screen, "_extcsd_dialog", return_value=str(p)):
        screen.open_extcsd(compare=True)
    Clock.tick()
    assert screen.extcsd_drafts[177] == "01"
    p.write_bytes(bytes(543))
    before = dict(screen.extcsd_drafts)
    with patch.object(screen, "_extcsd_dialog", return_value=str(p)):
        screen.open_extcsd()
    assert screen.extcsd_chip_ready and screen.extcsd_drafts == before
# Opening a file before reading hardware must populate File/New, never Chip.
with TemporaryDirectory() as folder:
    p = Path(folder) / "config.bin"
    for tail_size in (0, 32):
        screen.extcsd_chip_ready = False
        screen.extcsd_ready = False
        p.write_bytes(imported + bytes(tail_size))
        with patch.object(screen, "_extcsd_dialog", return_value=str(p)):
            screen.open_extcsd()
        Builder.sync()
        Clock.tick()
        assert not screen.extcsd_chip_ready
        for index, row in write_rows.items():
            assert row.ids.current_value.text == "--"
            assert row.ids.file_value.text == f"{imported[index]:02X}"
            assert row.ids.new_value.text == f"{imported[index]:02X}"
            assert row.ids.write_value.disabled
        assert len(screen.extcsd_file_tail) == tail_size

    def fake_read(voltage, bus, result, started):
        started()
        Clock.tick()
        result(bytes(data), cid)
        Clock.tick()
        Builder.sync()

    with patch.object(screen.orchestrator, "run_read_extcsd", side_effect=fake_read):
        screen.read_extcsd()
    for index, row in write_rows.items():
        assert row.ids.current_value.text == f"{data[index]:02X}"
        assert row.ids.file_value.text == f"{imported[index]:02X}"
        assert row.ids.new_value.text == f"{imported[index]:02X}"
    assert screen.extcsd_chip_ready
# Combined chip write: preflight, images first, irreversible protection last.
from modes.emmc.extcsd import write_plan
from modes.emmc.write_files import find_write_files
from orchestrator import AppOrchestrator
from unittest.mock import Mock

baseline = bytearray(512)
baseline[192] = 8
target = bytearray(baseline)
target[177], target[179], target[162], target[178], target[160] = 2, 0x48, 1, 0x10, 7
assert [i for i, _, _ in write_plan(baseline, target)] == [177, 179, 162, 178]
assert not write_plan(target, target)
bad = bytearray(target)
bad[162] = 3
try:
    write_plan(baseline, bad)
except ValueError:
    pass
else:
    raise AssertionError("Invalid batch accepted")

events, messages = [], []
worker = AppOrchestrator(messages.append, lambda *a, **kw: None)
worker.run_task = lambda fn, *a, **kw: fn(*a)
current = bytes(baseline)
identity = cid
def read_config(*a):
    events.append("read")
    return current, identity
def write_config(voltage, bus, index, value, expected, expected_cid, abort, allow_otp):
    global current
    assert expected == current and expected_cid == cid and allow_otp
    validate_write(current, index, value, allow_otp=True)
    events.append(index)
    updated = bytearray(current)
    updated[index] = value
    current = bytes(updated)
    return current, cid
def write_image(voltage, bus, area, path, **kw):
    events.append(area)
    return True
worker.emmc_driver = Mock()
worker.emmc_driver.read_extcsd.side_effect = read_config
worker.emmc_driver.write_extcsd.side_effect = write_config
worker.emmc_driver.write_partition.side_effect = write_image
payload = (bytes(baseline), cid, bytes(target))
with TemporaryDirectory() as folder:
    paths = {}
    for area, filename in (("BOOT1", "Boot1.bin"), ("BOOT2", "Boot2.bin"), ("USER", "userdata.img")):
        path = Path(folder) / filename
        path.write_bytes(bytes(16384))
        paths[area] = str(path)
    config_path = Path(folder) / "ECSD_CSD.BIN"
    config_path.write_bytes(target + bytes(32))
    paths["EXTCSD"] = str(config_path)
    found = find_write_files(folder, {"BOOT1": 16384, "BOOT2": 16384, "USER": 16384})
    assert found["EXTCSD"]["ready"] and found["EXTCSD"]["path"] == str(config_path.resolve())
    screen.boot_size_bytes = 16384
    screen.user_total_sectors = 32
    screen.write_folder = folder
    screen._manual_write_files.clear()
    screen._show_extcsd(baseline, "chip", cid)
    screen._extcsd_settings = (screen.ids.chk_voltage.active, screen.ids.spin_bus.text,
                               screen.emmc_preset, screen.emmc_clock)
    screen.refresh_write_files()
    assert screen.write_files["EXTCSD"]["ready"]
    from tkinter import messagebox
    with patch.object(gui.tk, "Tk", return_value=Mock()), \
         patch.object(messagebox, "askyesno", return_value=False), \
         patch.object(screen.orchestrator, "run_write_all") as launch:
        screen.start_write_all()
        launch.assert_not_called()
    with patch.object(gui.tk, "Tk", return_value=Mock()), \
         patch.object(messagebox, "askyesno", return_value=True), \
         patch.object(screen.orchestrator, "run_write_all", return_value=False) as launch:
        screen.start_write_all()
        assert launch.call_args.kwargs["extcsd"] == payload
        assert launch.call_args.args[2]["EXTCSD"] == str(config_path.resolve())
        assert not screen.operation_running
    result = Mock()
    worker.run_write_all(True, "4-bit", paths, extcsd=payload, result_cb=result)
    assert events == ["read", "BOOT1", "BOOT2", "USER", "read", 177, 179, 162, 178], events
    assert result.call_args.args[0][178] == 0x10
    current = bytes(baseline)
    events.clear()
    def refuse_rst(*args, **kwargs):
        global current
        actual, actual_cid = write_config(*args, **kwargs)
        if args[2] == 162:
            unchanged = bytearray(actual)
            unchanged[162] = baseline[162]
            current = bytes(unchanged)
        return current, actual_cid
    worker.emmc_driver.write_extcsd.side_effect = refuse_rst
    worker.run_write_all(True, "4-bit", paths, extcsd=payload)
    assert events[-2:] == [162, 178]
    assert current[162] == baseline[162] and current[178] == 0x10
    assert any("завершена с ошибками верификации" in message for message in messages)
    worker.emmc_driver.write_extcsd.side_effect = write_config
    for failure in ("cid", "invalid", "image", "cancel"):
        current = bytes(baseline)
        identity = b"wrong" if failure == "cid" else cid
        events.clear()
        worker.stop_event.clear()
        worker.emmc_driver.write_partition.side_effect = write_image
        test_payload = (bytes(baseline), cid, bytes(bad)) if failure == "invalid" else payload
        if failure == "image":
            worker.emmc_driver.write_partition.side_effect = lambda *a, **kw: False
        if failure == "cancel":
            worker.stop_event.set()
        try:
            worker.run_write_all(True, "4-bit", paths, extcsd=test_payload)
        except (ValueError, RuntimeError):
            assert failure in ("cid", "invalid")
        else:
            assert failure in ("image", "cancel")
        assert not any(isinstance(event, int) for event in events), (failure, events)
        if failure in ("cid", "invalid"):
            assert "BOOT1" not in events
    config_path.write_bytes(bytes(543))
    assert not find_write_files(folder, {})["EXTCSD"]["ready"]

# Analysis must expose the hardware snapshot and all five settings in its log.
WriteSession.identity, WriteSession.current = cid, bytes(baseline)
with patch.object(operations, "EmmcSession", WriteSession), \
     patch.object(WriteSession, "rpmb_probe", return_value=bytes(16), create=True), \
     patch.object(WriteSession, "select_user", create=True), \
     patch.object(WriteSession, "read_sectors", return_value=bytes(32768), create=True), \
     patch.object(operations.parts_mod, "parse", return_value=[]):
    info, _ = operations.read_info_and_partitions(lambda m: None, True, "4-bit")
assert info["extcsd"] == bytes(baseline) and info["cid"] == cid
for index in (177, 178, 179, 162, 160):
    assert f"[{index}]" in info["report"]
print("EXT_CSD smoke: OK (file columns, combined write, preflight, OTP order, analysis, readback)")

