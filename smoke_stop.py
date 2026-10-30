"""STOP interrupts pending USB I/O and releases ownership before the next task."""
import ctypes
import threading
import time
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
import t48_usb as usb
from orchestrator import AppOrchestrator
from modes.emmc.session import EmmcSession

for blocked in ('read', 'write'):
    entered, aborted, finished = threading.Event(), threading.Event(), threading.Event()
    events, logs, states = [], [], []
    created = []
    def initialize(handle, result):
        result._obj.value = 123
        return True
    def abort(handle, endpoint):
        events.append(('abort', endpoint))
        aborted.set()
        return True
    def transfer_read(handle, endpoint, buf, length, transferred, overlapped):
        if blocked == 'read' and endpoint == 0x82:
            entered.set()
            assert aborted.wait(3), 'STOP did not abort pending read'
            ctypes.set_last_error(995)
            return False
        transferred._obj.value = min(8, length)
        return True
    def transfer_write(handle, endpoint, buf, length, transferred, overlapped):
        if blocked == 'write' and endpoint == 0x02:
            entered.set()
            assert aborted.wait(3), 'STOP did not abort pending write'
            ctypes.set_last_error(995)
            return False
        events.append(('write', endpoint))
        transferred._obj.value = length
        return True
    def task_state(active):
        states.append(active)
        if not active:
            finished.set()
    worker = AppOrchestrator(logs.append, lambda *a: None, task_cb=task_state)
    def task():
        session = EmmcSession(logs.append)
        created.append(session)
        assert session.open()
        try:
            if blocked == 'read':
                session.dev.read_pipe_into(0x82, ctypes.create_string_buffer(16))
            else:
                session.dev.write_pipe(0x02, b'data')
            raise AssertionError('Cancelled I/O returned normally')
        finally:
            session.close()
    with ExitStack() as stack:
        stack.enter_context(patch.object(usb, 'find_all_xgecu_paths', return_value=['mock']))
        stack.enter_context(patch.object(usb.kernel32, 'CreateFileW', return_value=456))
        close = stack.enter_context(patch.object(usb.kernel32, 'CloseHandle', return_value=True))
        free = stack.enter_context(patch.object(usb.winusb, 'WinUsb_Free', return_value=True))
        stack.enter_context(patch.object(usb.winusb, 'WinUsb_Initialize', side_effect=initialize))
        policy = stack.enter_context(patch.object(usb.winusb, 'WinUsb_SetPipePolicy', return_value=True))
        stack.enter_context(patch.object(usb.winusb, 'WinUsb_AbortPipe', side_effect=abort))
        stack.enter_context(patch.object(usb.winusb, 'WinUsb_FlushPipe', return_value=True))
        stack.enter_context(patch.object(usb.winusb, 'WinUsb_ReadPipe', side_effect=transfer_read))
        stack.enter_context(patch.object(usb.winusb, 'WinUsb_WritePipe', side_effect=transfer_write))
        assert worker.run_task(task)
        assert entered.wait(3)
        started = time.monotonic()
        worker.stop_operation()
        assert finished.wait(3), 'Worker remained blocked after STOP'
        assert time.monotonic() - started < 3
        assert states == [True, False]
        assert ('write', 0x01) in events, 'Power-off was not attempted'
        assert free.call_count == close.call_count == 1
        assert not usb._devices
        assert {call.args[1] for call in policy.call_args_list} == {1, 2, 0x81, 0x82}
        created[0].dev.close()
        assert free.call_count == close.call_count == 1, 'Close must be idempotent'
        finished.clear()
        def reopen():
            device = usb.XGecuT48()
            assert device.open()
            device.close()
        assert worker.run_task(reopen)
        assert finished.wait(3)
        assert not worker.stop_event.is_set()
        assert free.call_count == close.call_count == 2
        assert not usb._devices
    print('PASS: STOP during pending USB', blocked, ', power-off, release, next task')

session = EmmcSession()
session.dev = MagicMock()
session.dev.power_off.side_effect = RuntimeError('hung device')
session.close()
session.dev.close.assert_called_once()
print('PASS: failed power-off still closes USB')

# STOP inside an announced flash stream must finish it, not leave T48 waiting.
from io import BytesIO
from modes.emmc import operations as ops, protocol as P
for mode in ('plain', 'fill', 'plan'):
    stop = threading.Event()
    events = []
    class FlashStream:
        def start_write(self, lba, count, width):
            self.expected = count
            self.sent = 0
            events.append(('start', lba, count))
        def send_write_chunk(self, index, lba, data):
            assert index == self.sent
            self.sent += 1
            if index == 3:
                stop.set()
        def wait_write_complete(self):
            assert self.sent == self.expected, 'Abandoned flash stream'
            events.append(('complete', self.sent))
            return True
    data = b'\x55' * (128 * P.CHUNK_BYTES)
    device = FlashStream()
    result = ops._write_region(device, BytesIO(data), 0, len(data), '4-bit',
                              ops._SpeedMeter(lambda msg: None, 'write', len(data)),
                              lambda msg: None, stop.is_set,
                              erased_byte=0 if mode == 'fill' else None,
                              write_plan=[(0, len(data), False)] if mode == 'plan' else None)
    assert result is None
    assert events == [('start', 0, 64), ('complete', 64)], events
print('PASS: STOP finishes one bounded stream, starts no next stream (plain/fill/plan)')

stop = threading.Event()
with usb.usb_operation(stop):
    device = usb.XGecuT48()
device.winusb_handle = usb.wintypes.HANDLE(123)
with usb._devices_lock:
    usb._devices.add(device)
try:
    with patch.object(usb.winusb, 'WinUsb_AbortPipe', return_value=True) as abort:
        device.begin_write()
        stop.set()
        usb.cancel_operation(stop)
        abort.assert_not_called()
        device._check_cancelled()  # Finish announced stream despite STOP.
        device.end_write()
        try:
            device._check_cancelled()
        except RuntimeError:
            pass
        else:
            raise AssertionError('Cancellation lost at write boundary')
finally:
    usb._devices.discard(device)
print('PASS: STOP defers USB abort only during active write stream')
