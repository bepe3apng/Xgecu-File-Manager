import ctypes
from ctypes import wintypes
import winreg
import threading
from contextlib import contextmanager

_operation = threading.local()
_devices = set()
_devices_lock = threading.RLock()


@contextmanager
def usb_operation(stop_event):
    previous = getattr(_operation, 'stop_event', None)
    _operation.stop_event = stop_event
    try:
        yield
    finally:
        _operation.stop_event = previous


def cancel_operation(stop_event):
    # Abort pending I/O only. The owning worker performs power-off and close.
    with _devices_lock:
        for device in tuple(_devices):
            if device.stop_event is stop_event and not device._write_active:
                device.abort_transfers()

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
winusb = ctypes.WinDLL('winusb', use_last_error=True)

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x00000001, 0x00000002
OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, FILE_FLAG_OVERLAPPED = 3, 0x80, 0x40000000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
PIPE_TRANSFER_TIMEOUT = 1500

winusb.WinUsb_Initialize.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE)]
winusb.WinUsb_Initialize.restype = wintypes.BOOL
winusb.WinUsb_Free.argtypes = [wintypes.HANDLE]
winusb.WinUsb_Free.restype = wintypes.BOOL
winusb.WinUsb_WritePipe.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, ctypes.c_char_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
winusb.WinUsb_WritePipe.restype = wintypes.BOOL
winusb.WinUsb_ReadPipe.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, ctypes.c_char_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
winusb.WinUsb_ReadPipe.restype = wintypes.BOOL
winusb.WinUsb_SetPipePolicy.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p]
winusb.WinUsb_SetPipePolicy.restype = wintypes.BOOL
for _name in ('WinUsb_AbortPipe', 'WinUsb_FlushPipe'):
    getattr(winusb, _name).argtypes = [wintypes.HANDLE, ctypes.c_ubyte]
    getattr(winusb, _name).restype = wintypes.BOOL
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

def find_all_xgecu_paths(vid="A466", pid="0A53"):
    paths = []
    target = f"vid_{vid.lower()}&pid_{pid.lower()}"
    base_path = r"SYSTEM\CurrentControlSet\Control\DeviceClasses"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as base_key:
            for i in range(winreg.QueryInfoKey(base_key)[0]):
                guid_name = winreg.EnumKey(base_key, i)
                try:
                    with winreg.OpenKey(base_key, guid_name) as guid_key:
                        for j in range(winreg.QueryInfoKey(guid_key)[0]):
                            dev_name = winreg.EnumKey(guid_key, j)
                            if target in dev_name.lower() and dev_name.startswith("##?#"):
                                paths.append("\\\\?\\" + dev_name[4:])
                except OSError: pass
    except OSError: pass
    return list(set(paths))

class XGecuT48:
    def __init__(self):
        self.file_handle = INVALID_HANDLE_VALUE
        self.winusb_handle = wintypes.HANDLE()
        self.stop_event = getattr(_operation, 'stop_event', None)
        self._cleaning_up = False
        self._write_active = False

    def begin_write(self):
        # Serialize with STOP so it cannot abort a stream just being started.
        with _devices_lock:
            self._check_cancelled()
            self._write_active = True

    def end_write(self):
        with _devices_lock:
            self._write_active = False

    def _check_cancelled(self):
        if not self._cleaning_up and not self._write_active and self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError('Операция отменена (STOP)')

    def abort_transfers(self):
        if self.winusb_handle.value:
            for endpoint in (0x01, 0x02, 0x81, 0x82):
                winusb.WinUsb_AbortPipe(self.winusb_handle, endpoint)

    def open(self):
        self._check_cancelled()
        paths = find_all_xgecu_paths()
        if not paths:
            return False
        for path in paths:
            h_file = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, None)
            if h_file != INVALID_HANDLE_VALUE:
                h_winusb = wintypes.HANDLE()
                if winusb.WinUsb_Initialize(h_file, ctypes.byref(h_winusb)):
                    self.file_handle = h_file
                    self.winusb_handle = h_winusb
                    try:
                        timeout = wintypes.ULONG(PIPE_TRANSFER_TIMEOUT)
                        for endpoint in (0x01, 0x02, 0x81, 0x82):
                            if not winusb.WinUsb_SetPipePolicy(self.winusb_handle, endpoint, 3, ctypes.sizeof(timeout), ctypes.byref(timeout)):
                                raise ctypes.WinError(ctypes.get_last_error())
                        with _devices_lock:
                            _devices.add(self)
                        self._check_cancelled()
                    except Exception:
                        self.close()
                        raise
                    return True
                kernel32.CloseHandle(h_file)
        return False

    def set_command_timeout(self, milliseconds):
        timeout = wintypes.ULONG(milliseconds)
        if not winusb.WinUsb_SetPipePolicy(self.winusb_handle, 0x81, 3, ctypes.sizeof(timeout), ctypes.byref(timeout)):
            raise ctypes.WinError(ctypes.get_last_error())

    def power_off(self):
        if not self.winusb_handle.value:
            return
        # Called by the owning worker after its pending I/O has returned.
        with _devices_lock:
            _devices.discard(self)
        self._cleaning_up = True
        try:
            self.abort_transfers()
            self.set_command_timeout(PIPE_TRANSFER_TIMEOUT)
            for endpoint in (0x81, 0x82):
                winusb.WinUsb_FlushPipe(self.winusb_handle, endpoint)
            self.write_pipe(0x01, bytes.fromhex("0400000000000000"))
            if not self.read_pipe(0x81, 16):
                raise RuntimeError('Нет ответа T48 на команду отключения питания')
        finally:
            self._cleaning_up = False

    def close(self):
        with _devices_lock:
            _devices.discard(self)
            try:
                if self.winusb_handle.value:
                    winusb.WinUsb_Free(self.winusb_handle)
            finally:
                self.winusb_handle = wintypes.HANDLE()
                try:
                    if self.file_handle != INVALID_HANDLE_VALUE:
                        kernel32.CloseHandle(self.file_handle)
                finally:
                    self.file_handle = INVALID_HANDLE_VALUE

    def write_pipe(self, pipe_id: int, data: bytes) -> int:
        self._check_cancelled()
        transferred = wintypes.ULONG()
        buf = ctypes.create_string_buffer(data, len(data))
        if not winusb.WinUsb_WritePipe(self.winusb_handle, pipe_id, buf, len(data), ctypes.byref(transferred), None):
            error = ctypes.get_last_error()
            raise OSError(error, f"USB write EP 0x{pipe_id:02X}: {ctypes.FormatError(error).strip()}; {transferred.value}/{len(data)} bytes")
        if transferred.value != len(data):
            raise RuntimeError(f"Incomplete USB write EP 0x{pipe_id:02X}: {transferred.value}/{len(data)} bytes")
        return transferred.value

    def read_pipe(self, pipe_id: int, length: int) -> bytes:
        self._check_cancelled()
        transferred = wintypes.ULONG()
        buf = ctypes.create_string_buffer(length)
        if not winusb.WinUsb_ReadPipe(self.winusb_handle, pipe_id, buf, length, ctypes.byref(transferred), None):
            self._check_cancelled()
            raise ctypes.WinError(ctypes.get_last_error())
        self._check_cancelled()
        return buf.raw[:transferred.value]

    def read_pipe_into(self, pipe_id: int, buf) -> int:
        self._check_cancelled()
        transferred = wintypes.ULONG()
        if not winusb.WinUsb_ReadPipe(self.winusb_handle, pipe_id, buf, ctypes.sizeof(buf), ctypes.byref(transferred), None):
            self._check_cancelled()
            raise ctypes.WinError(ctypes.get_last_error())
        self._check_cancelled()
        return transferred.value