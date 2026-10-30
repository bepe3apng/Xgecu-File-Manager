from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock
from modes.emmc import operations as ops, protocol as P
from modes.emmc.verification import verify_file
from modes.emmc.session import EmmcSession
from orchestrator import AppOrchestrator

class Feedback(list):
    def __call__(self, msg): self.append(msg)
    def progress(self, pct, speed): self.last_progress = pct

class FakeSession:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.events = []
        self.restarted = False
    def open(self): self.events.append("open"); return True
    def close(self): self.events.append("close")
    def init_hw(self, *args): self.events.append("init")
    def wake_registers(self, *args):
        ecsd = bytearray(512)
        ecsd[226] = 1
        ecsd[212:216] = (256).to_bytes(4, "little")
        return bytes(32), bytes(24), ecsd
    def select_partition(self, name, *args): self.events.append(("select", name))
    def select_user(self, *args): self.select_partition("USER")
    def select(self, *args): pass
    def restart(self, *args): self.events.append("restart"); self.restarted = True
    def start_read(self, lba, count): self.events.append(("read", lba, count))
    def iter_read_chunks(self, count):
        if self.mode == "eof": return
        for i in range(count):
            if self.mode == "short": yield bytes(10)
            else:
                value = 1 if self.mode == "bad" and self.restarted else 0
                yield bytes([value]) * P.CHUNK_BYTES
    def start_write(self, *args): self.events.append("write")
    def send_write_chunk(self, *args): pass
    def wait_write_complete(self, *args, **kwargs): return self.mode != "timeout"

with TemporaryDirectory(dir=".") as folder:
    path = Path(folder) / "source.bin"
    path.write_bytes(bytes(P.CHUNK_BYTES * 60 + 3))
    log = Feedback(); device = FakeSession()
    assert verify_file(device, log, True, "4-bit", "BOOT2", str(path),
                       [(17, 0, path.stat().st_size)])
    assert device.events[:3] == ["restart", ("select", "BOOT2"), ("read", 17, 61)]
    assert log.last_progress == 100

    device, log = FakeSession("bad"), Feedback()
    assert not verify_file(device, log, True, "4-bit", "USER", str(path),
                           [(0, 0, path.stat().st_size)])
    details = [msg for msg in log if msg.startswith("[-] Ошибка верификации:")]
    assert len(details) == 50 and "61" in log[-1]
    for mode in ("short", "eof"):
        try:
            verify_file(FakeSession(mode), Feedback(), True, "4-bit", "USER",
                        str(path), [(0, 0, path.stat().st_size)])
        except RuntimeError: pass
        else: raise AssertionError(mode)
    device = FakeSession()
    assert not verify_file(device, Feedback(), True, "4-bit", "USER", str(path),
                           [(0, 0, 10)], lambda: True)
    assert not device.events
    calls = [False, True]
    assert not verify_file(FakeSession(), Feedback(), True, "4-bit", "USER",
                           str(path), [(0, 0, 10)], lambda: calls.pop(0))

    path.write_bytes(bytes(P.CHUNK_BYTES + 7))
    for area in ("BOOT1", "BOOT2", "USER"):
        device, log = FakeSession(), Feedback()
        with patch.object(ops, "EmmcSession", return_value=device):
            ops.write_partition(log, True, "4-bit", area, str(path), verify=True)
        assert device.events.index("write") < device.events.index("restart")
        assert ("select", area) in device.events[device.events.index("restart"):]
        assert "успешно завершена" in log[-1] and device.events[-1] == "close"
    for mode in ("bad", "timeout"):
        device, log = FakeSession(mode), Feedback()
        with patch.object(ops, "EmmcSession", return_value=device):
            ops.write_partition(log, True, "4-bit", "USER", str(path), verify=True)
        assert not any("успешно завершена" in msg for msg in log)
        if mode == "timeout": assert "restart" not in device.events

    for fn, args in [
        (ops.dump_boot, ("BOOT1", None)),
        (ops.dump_user_area, ()),
        (ops.dump_lba_range, ("partition", 71, 167)),
    ]:
        device, log = FakeSession(), Feedback()
        out = str(Path(folder) / "dump.bin")
        with patch.object(ops, "EmmcSession", return_value=device):
            fn(log, True, "4-bit", *args, out, verify=True)
        reads = [event for event in device.events if isinstance(event, tuple) and event[0] == "read"]
        assert len(reads) == 2 and reads[0] == reads[1], (fn, reads)
        assert "restart" in device.events and "успешно" in log[-1]

    # Sparse/segmented images: map file offsets to their actual device LBAs.
    path.write_bytes(bytes(P.CHUNK_BYTES * 4))
    plan = dict(total_bytes=P.CHUNK_BYTES*4, data_bytes=P.CHUNK_BYTES*2,
                skip_bytes=P.CHUNK_BYTES*2, format="raw",
                segments=[(0, 0, P.CHUNK_BYTES), (64, P.CHUNK_BYTES*3, P.CHUNK_BYTES)])
    device, log = FakeSession(), Feedback()
    with patch.object(ops, "EmmcSession", return_value=device), patch.object(ops.ImageProcessor, "analyze", return_value=plan):
        ops.write_lba_range(log, True, "4-bit", "data", 100, 300, str(path), verify=True)
    assert ("read", 100, 1) in device.events and ("read", 164, 1) in device.events
    assert "успешно завершена" in log[-2]

    # No restart when disabled.
    device = FakeSession()
    with patch.object(ops, "EmmcSession", return_value=device):
        ops.write_partition(Feedback(), True, "4-bit", "USER", str(path))
    assert "restart" not in device.events

# Erase before writing uses the same selected session and blocks writes on failure.
class EraseSession(FakeSession):
    def __init__(self, mode="ok"):
        super().__init__(mode)
        self.dev = MagicMock()
        self.dev.read_pipe.return_value = bytes(8)
        self.dev.write_pipe.side_effect = lambda ep, data: self.events.append(("usb", bytes(data)))
    def wake_registers(self, *args):
        cid, csd, ecsd = super().wake_registers(*args)
        ecsd[212:216] = (4096).to_bytes(4, "little")
        ecsd[175], ecsd[224] = 1, 1
        return cid, csd, ecsd
    def read_sectors(self, first, sectors):
        self.events.append(("erase_check", first, sectors))
        if self.mode == "read_error":
            raise RuntimeError("USB read failed")
        data = bytes(sectors * P.SECTOR_SIZE)
        if self.mode == "read_short":
            return data[:-1]
        if self.mode == "dirty_tail":
            return data[:-1] + b"\x01"
        return data
    def erase_wait_ready(self):
        self.events.append("erase")
        return self.mode != "erase_timeout"

with TemporaryDirectory(dir=".") as folder:
    path = Path(folder) / "image.bin"
    path.write_bytes(bytes(P.CHUNK_BYTES))
    for area in ("BOOT1", "BOOT2", "USER"):
        device, log = EraseSession(), Feedback()
        with patch.object(ops, "EmmcSession", return_value=device):
            assert ops.write_partition(log, True, "4-bit", area, str(path),
                                       erase_before=True, verify=True)
        end = 255 if area != "USER" else 4095
        assert ("usb", bytes(P.erase_range_cmd(0, end))) in device.events
        assert device.events.index(("select", area)) < device.events.index("erase")
        assert device.events.index("erase") < device.events.index("restart")
        assert "write" not in device.events  # Entire zero image matches erased 00.
        assert not any("Стерто:" in message for message in log)
    for failure in ("erase_timeout", "short", "cancel", "oversize"):
        device, log = EraseSession(failure), Feedback()
        if failure == "short":
            device.dev.read_pipe.return_value = bytes(2)
        path.write_bytes(bytes(256 * 512 + 1) if failure == "oversize" else bytes(P.CHUNK_BYTES))
        abort = (lambda: "erase" in device.events) if failure == "cancel" else None
        with patch.object(ops, "EmmcSession", return_value=device):
            assert not ops.write_partition(log, True, "4-bit", "BOOT1", str(path),
                                           erase_before=True, abort_check=abort)
        assert "write" not in device.events and device.events[-1] == "close"
        if failure == "oversize":
            assert not device.dev.write_pipe.called
    path.write_bytes(b"\x02" * P.CHUNK_BYTES)
    plan = dict(total_bytes=P.CHUNK_BYTES, data_bytes=P.CHUNK_BYTES, skip_bytes=0,
                format="raw", segments=[(0, 0, P.CHUNK_BYTES)])
    for start, end, allowed in ((1024, 2047, True), (1, 1024, False), (4096, 5119, False)):
        device, log = EraseSession(), Feedback()
        with patch.object(ops, "EmmcSession", return_value=device), patch.object(ops.ImageProcessor, "analyze", return_value=plan):
            ops.write_lba_range(log, True, "4-bit", "data", start, end, str(path), erase_before=True)
        assert ("write" in device.events) == allowed
        if allowed:
            assert ("usb", bytes(P.erase_range_cmd(start, end))) in device.events
        else:
            assert not device.dev.write_pipe.called
    device = EraseSession()
    with patch.object(ops, "EmmcSession", return_value=device):
        assert ops.write_partition(Feedback(), True, "4-bit", "USER", str(path))
    assert not device.dev.write_pipe.called

# Erase verification covers the entire area and prevents writes on read failures.
with TemporaryDirectory(dir=".") as folder:
    path = Path(folder) / "small.bin"
    path.write_bytes(b"\x02" * P.CHUNK_BYTES)
    for mode in ("ok", "dirty_tail", "read_short", "read_error", "cancel_check"):
        device, log = EraseSession(mode), Feedback()
        abort = (lambda: any(isinstance(e, tuple) and e[0] == "erase_check"
                             for e in device.events)) if mode == "cancel_check" else None
        with patch.object(ops, "EmmcSession", return_value=device):
            result = ops.write_partition(log, True, "4-bit", "USER", str(path),
                                         erase_before=True, abort_check=abort)
        assert bool(result) == (mode == "ok"), (mode, log)
        checks = [e for e in device.events if isinstance(e, tuple) and e[0] == "erase_check"]
        assert checks
        assert ("write" in device.events) == (mode == "ok")
        if mode == "ok":
            assert sum(e[2] for e in checks) == 4096
            assert device.events.index(checks[-1]) < device.events.index("write")
        assert device.events[-1] == "close"
for value in (0x00, 0xFF):
    device, log = EraseSession(), Feedback()
    device.read_sectors = lambda first, count: bytes([value]) * count * P.SECTOR_SIZE
    assert ops._verify_erased(device, log, "BOOT1", 0, 255, value, None)
print("PASS: full erased-area readback, 00/FF, dirty tail, short/error reads, STOP block writes")

# Reconstruct writes over erased memory to check skipped chunks and addresses.
from io import BytesIO

class RegionSession:
    def __init__(self, fill, size):
        self.memory = bytearray([fill]) * size
        self.starts = []
        self.sent = 0
    def start_write(self, lba, count, width):
        self.starts.append((lba, count))
        self.first, self.count, self.index = lba, count, 0
    def send_write_chunk(self, index, lba, data):
        assert index == self.index < self.count
        assert lba == self.first + index * P.CHUNK_SECTORS
        self.memory[lba * P.SECTOR_SIZE:lba * P.SECTOR_SIZE + len(data)] = data
        self.index += 1
        self.sent += len(data)
    def wait_write_complete(self):
        assert self.index == self.count
        return True

for fill in (0x00, 0xFF):
    chunk = P.CHUNK_BYTES
    pattern = bytes([fill]) * chunk
    other = bytes([fill ^ 0xFF]) * chunk
    # Leading, interior and trailing fill; run longer than the bounded buffer.
    payload = pattern * 2 + other * 65 + pattern * 3 + other + pattern
    for optimize in (False, True, "plan"):
        device = RegionSession(fill, len(payload) + chunk * 2)
        log = Feedback()
        meter = ops._SpeedMeter(log, "записи", len(payload))
        skipped = ops._write_region(device, BytesIO(payload), P.CHUNK_SECTORS,
                                    len(payload), "4-bit", meter, log, None,
                                    fill if optimize else None,
                                    write_plan=ops._analyze_write_region(
                                        BytesIO(payload), 0, len(payload), fill, log, None
                                    ) if optimize == "plan" else None)
        if optimize == "plan":
            assert device.starts == [(3 * P.CHUNK_SECTORS, 64),
                                     (67 * P.CHUNK_SECTORS, 1),
                                     (71 * P.CHUNK_SECTORS, 1)]
        assert device.memory[chunk:chunk + len(payload)] == payload
        assert skipped == (6 * chunk if optimize else 0)
        assert device.sent == len(payload) - skipped
        assert meter.total == len(payload)
    device = RegionSession(fill, 2 * chunk)
    assert ops._write_region(device, BytesIO(pattern), 0, chunk, "4-bit",
                             ops._SpeedMeter(Feedback(), "записи", chunk),
                             Feedback(), None, fill) == chunk
    assert not device.starts
    # Preserve existing zero-padding semantics for incomplete final chunks.
    tail = bytes([fill]) * 7
    device = RegionSession(fill, chunk)
    ops._write_region(device, BytesIO(tail), 0, len(tail), "4-bit",
                      ops._SpeedMeter(Feedback(), "записи", len(tail)),
                      Feedback(), None, fill)
    assert device.memory == tail.ljust(chunk, b"\x00")
    device = RegionSession(fill, chunk)
    assert ops._write_region(device, BytesIO(other), 0, chunk, "4-bit",
                             ops._SpeedMeter(Feedback(), "записи", chunk),
                             Feedback(), lambda: True, fill) is None
    assert not device.starts
print("PASS: 00/FF skipping, reconstructed data, offsets, buffer boundaries, tails, STOP and no-erase writes")

# Real restart method with only USB mocked.
device = EmmcSession()
device.dev = MagicMock()
device.dev.write_pipe.return_value = 8
device._last_cid = b"cid"
device.init_hw = MagicMock()
device.wake_registers = MagicMock(return_value=(b"cid", b"", b""))
with patch("modes.emmc.session.time.sleep"):
    device.restart(True, "4-bit")
assert device.dev.write_pipe.call_args.args[1] == bytes.fromhex("0400000000000000")
device.wake_registers.return_value = (b"other", b"", b"")
with patch("modes.emmc.session.time.sleep"):
    try: device.restart(True, "4-bit")
    except RuntimeError: pass
    else: raise AssertionError("CID change accepted")
device.dev.read_pipe_into.side_effect = [P.CHUNK_BYTES, 0]
try: list(device.iter_read_chunks(1))
except RuntimeError: pass
else: raise AssertionError("short USB trailer accepted")

# A missing power-off acknowledgement must not skip init, CID or readback.
for mode in ("timeout", "usb_error", "missing", "cid", "init", "write"):
    device = EmmcSession(log_cb=lambda message: None)
    device.dev = MagicMock()
    device.dev.write_pipe.return_value = 8
    device.dev.read_pipe.side_effect = OSError(121 if mode != "usb_error" else 5, "USB")
    device.dev.open.return_value = mode != "missing"
    device._last_cid = b"cid"
    device.init_hw = MagicMock()
    device.wake_registers = MagicMock(return_value=(b"other" if mode == "cid" else b"cid", b"", b""))
    if mode == "init": device.init_hw.side_effect = RuntimeError("init failed")
    if mode == "write": device.dev.write_pipe.side_effect = OSError(121, "write failed")
    with patch("modes.emmc.session.time.sleep"):
        try:
            device.restart(True, "4-bit")
        except (OSError, RuntimeError):
            assert mode != "timeout", mode
        else:
            assert mode == "timeout", mode
    if mode in ("usb_error", "write"):
        device.dev.open.assert_not_called()
        device.init_hw.assert_not_called()
    elif mode == "missing":
        device.init_hw.assert_not_called()
    else:
        device.dev.close.assert_called_once()
        device.dev.open.assert_called_once()
        device.init_hw.assert_called_once_with(True, "4-bit")
        if mode != "init": device.wake_registers.assert_called_once_with("4-bit")
print("PASS: restart recovers only power-off response timeout; USB/write/init/CID failures remain fatal")

# Route all flags through orchestrator/facade.
orchestrator = AppOrchestrator(lambda msg: None, lambda *args, **kwargs: None)
orchestrator.run_task = lambda fn, *args, **kwargs: fn(*args)
for method, op, args in [
    ("run_dump_boot", "dump_boot", (True,"4-bit","BOOT1",None,"out")),
    ("run_dump_userdata", "dump_user_area", (True,"4-bit","out")),
    ("run_dump_part", "dump_lba_range", (True,"4-bit","part",1,9,"out")),
    ("run_write_task", "write_partition", (True,"4-bit","BOOT2","in")),
    ("run_write_part", "write_lba_range", (True,"4-bit","part",1,9,"in")),
]:
    with patch.object(ops, op) as target:
        getattr(orchestrator, method)(*args, verify=True)
        assert target.call_args.kwargs["verify"] is True

for method, op, args in [
    ("run_write_task", "write_partition", (True, "4-bit", "BOOT1", "in")),
    ("run_write_part", "write_lba_range", (True, "4-bit", "data", 1024, 2047, "in")),
]:
    with patch.object(ops, op) as target:
        getattr(orchestrator, method)(*args, erase_before=True)
        assert target.call_args.kwargs["erase_before"] is True

with TemporaryDirectory(dir=".") as folder:
    source = Path(folder) / "image.bin"
    source.write_bytes(bytes(P.CHUNK_BYTES))
    with patch.object(ops, "write_partition", return_value=True) as target:
        orchestrator.run_write_all(True, "4-bit",
                                  {area: str(source) for area in ("BOOT1", "BOOT2", "USER")},
                                  erase_before=True)
    assert [call.args[3] for call in target.call_args_list] == ["BOOT1", "BOOT2", "USER"]
    assert all(call.kwargs["erase_before"] for call in target.call_args_list)

from kivy.lang import Builder
from gui import MainScreen
Builder.load_file("gui.kv")
screen = MainScreen()
assert not screen.verify_read and screen.verify_write
assert screen.erase_before_write
screen.ids.chk_erase_before_write.active = False
assert not screen.erase_before_write
screen.ids.chk_erase_before_write.active = True
assert screen.erase_before_write
screen.ids.chk_verify_read.active = True
screen.ids.chk_verify_write.active = True
assert screen.verify_read and screen.verify_write
screen.operation_running = True
assert screen.ids.chk_verify_read.disabled and screen.ids.chk_verify_write.disabled
print("PASS: streamed comparisons, 50-message cap, EOF/short USB, cancellation, all read/write routes, sparse regions, restart/CID, GUI flags")
import runpy
runpy.run_path("smoke_layers.py", run_name="__main__")
