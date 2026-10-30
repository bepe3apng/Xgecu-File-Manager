"""Слой сессии устройства XGecu T48: жизненный цикл + стриминг-движок.

Инкапсулирует: открытие/закрытие USB, инициализацию оборудования,
вывод регистров из спящего режима, выбор раздела и единый
чанк-движок чтения/записи (раньше дублировался в 4 местах).
"""
import ctypes
import struct
import time

from t48_usb import XGecuT48
from modes.emmc import protocol as P


class EmmcSession:
    """Контекст жизненного цикла одной операции с чипом eMMC."""

    def __init__(self, log_cb=print, clock_mhz=None, legacy_30=None, preset=None):
        self.log = log_cb
        self.preset = P.DEFAULT_PRESET if preset is None else preset
        if self.preset not in (*P.PRESET_SETUP, "Авто"):
            raise ValueError(f"Неизвестный пресет eMMC: {self.preset}")
        self.clock_mhz = P.DEFAULT_CLOCK_MHZ if clock_mhz is None else clock_mhz
        self.legacy_30 = self.preset == "THGA" and self.clock_mhz == 30 and (
            P.DEFAULT_LEGACY_30 if legacy_30 is None else legacy_30
        )
        if self.clock_mhz not in P.CLOCK_SELECTORS:
            raise ValueError("Поддерживаются частоты 8, 16, 20 и 30 МГц")
        self.dev = XGecuT48()

    # --- Жизненный цикл ---

    def open(self) -> bool:
        if not self.dev.open():
            self.log("[-] Программатор XGecu T48 не найден или занят.")
            return False
        return True

    def close(self):
        try:
            self.dev.power_off()
        except Exception:
            pass  # Power-off is best effort; always release USB below.
        finally:
            self.dev.close()

    def restart(self, is_18v, bus_width):
        """Power cycle the chip while preserving this session's settings."""
        previous_cid = getattr(self, "_last_cid", None)
        command = bytes.fromhex("0400000000000000")
        if self.dev.write_pipe(P.EP_CMD_OUT, command) != len(command):
            raise RuntimeError("Не удалось передать команду отключения питания eMMC")
        try:
            self.dev.read_pipe(P.EP_CMD_IN, 16)
        except OSError as error:
            # Some T48 versions accept power-off without acknowledging it.
            # Recover only that timeout; init, identity and readback must still pass.
            if getattr(error, 'winerror', None) != 121 and error.errno != 121:
                raise
            self.log("[*] Перезапуск eMMC: повторное открытие USB после тайм-аута ответа T48.")
            self.dev.close()
            if not self.open():
                raise RuntimeError("Не удалось повторно открыть T48 перед верификацией") from error
        time.sleep(0.3)
        self.init_hw(is_18v, bus_width)
        cid, _, _ = self.wake_registers(bus_width)
        if previous_cid is not None and cid != previous_cid:
            raise RuntimeError("После перезапуска изменился CID eMMC")

    # --- Инициализация и регистры ---

    def init_hw(self, is_18v, bus_width):
        if self.preset == "Авто":
            from modes.emmc.autoconfig import detect, remembered
            selected = remembered(is_18v, bus_width)
            if selected is None:
                return detect(self, is_18v, bus_width)
            self.preset, self.clock_mhz, self.legacy_30 = selected
            self.log(f"[*] Авто: сохранённый конфиг {self.preset}, {self.clock_mhz} МГц.")
        dev = self.dev
        if bus_width not in ("1-bit", "4-bit"):
            raise ValueError("Поддерживается шина 1-bit или 4-bit")
        if self.clock_mhz == 30 and not is_18v:
            raise ValueError(f"Для {self.clock_mhz} МГц требуется VCCQ 1.8 В")
        self.log(f"[*] eMMC: {self.clock_mhz} МГц, {bus_width}, пресет: {self.preset}")
        if self.legacy_30:
            self.log(f"[*] {self.clock_mhz} МГц: экспериментальный разгон Legacy (HS_TIMING=0; паспортный предел 26 МГц).")
        elif self.clock_mhz == 30:
            self.log(f"[*] {self.clock_mhz} МГц: стандартный High-Speed (HS_TIMING=1).")
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex("24f0000001000000"))
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex("24e0280000000000000000e5"))
        dev.read_pipe(P.EP_CMD_IN, 40)
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex("24f1000000000000"))
        setup = P.PRESET_SETUP[self.preset][self.clock_mhz == 30]
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(setup))
        dev.read_pipe(P.EP_CMD_IN, 16)

        dev.write_pipe(P.EP_CMD_OUT, bytes(P.build_init_config(is_18v, bus_width, self.clock_mhz, self.preset)))
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.config_accept_cmd(is_18v)))
        dev.read_pipe(P.EP_CMD_IN, 32)

        dev.set_command_timeout(30000)
        try:
            dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.bus_mode_cmd(bus_width, self.clock_mhz)))
            response = dev.read_pipe(P.EP_CMD_IN, 8)
        finally:
            dev.set_command_timeout(1500)
        if len(response) != 8 or response[0] != 0x21 or response[1] != 0:
            raise RuntimeError(f"Ошибка инициализации eMMC (ответ 0x21: {response.hex(' ') or 'нет ответа'})")
        time.sleep(0.1)

    def wake_registers(self, bus_width):
        """Выводит чип из спящего режима, возвращает (cid, csd, ecsd)."""
        dev = self.dev
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.wake_cid_cmd(bus_width, self.clock_mhz)))
        cid = dev.read_pipe(P.EP_CMD_IN, 32)
        if len(cid) != 32 or cid[:2] != b"\x07\x00":
            raise RuntimeError(f"Ошибка чтения CID: {cid.hex(' ') or 'нет ответа'}")
        if self.legacy_30:
            # T48 enables HS during CID. Restore Legacy before CMD9 at the same divider.
            command = bytes.fromhex(P.CMD_LEGACY_TIMING)
            if dev.write_pipe(P.EP_CMD_OUT, command) != len(command):
                raise RuntimeError("Неполная команда переключения HS_TIMING")
            response = dev.read_pipe(P.EP_CMD_IN, 8)
            if len(response) != 8 or response[:2] != b"\x27\x00":
                raise RuntimeError(f"Ошибка переключения HS_TIMING: {response.hex(' ')}")
            time.sleep(0.2)
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.wake_csd_cmd(bus_width, self.clock_mhz)))
        csd = dev.read_pipe(P.EP_CMD_IN, 24)
        if len(csd) != 24 or csd[0] != 0x06 or csd[1] != 0:
            raise RuntimeError(f"Ошибка чтения CSD ({bus_width}): {csd.hex(' ') or 'нет ответа'}")
        ecsd = self.read_extcsd()
        if self.clock_mhz == 30:
            expected_width = 1 if bus_width == "4-bit" else 0
            # Standard captures return BUS_WIDTH=0 even with the 4-bit config.
            # Accept captured values without claiming the physical bus width.
            valid_widths = (expected_width,) if self.preset == "THGA" else (0, expected_width)
            expected_timing = 0 if self.legacy_30 else 1
            if (ecsd[185] & 0x0F) != expected_timing or ecsd[183] not in valid_widths or not int.from_bytes(ecsd[212:216], "little"):
                mode = "Legacy" if self.legacy_30 else "High-Speed"
                raise RuntimeError(f"EXT_CSD не подтвердил {mode}, ширину шины или ёмкость на 30 МГц")
        self._last_cid = cid
        return cid, csd, ecsd

    def read_extcsd(self):
        """Read the register without reinitializing or changing configuration."""
        command = bytes.fromhex("0848000200000000")
        if self.dev.write_pipe(P.EP_CMD_OUT, command) != len(command):
            raise RuntimeError("Неполная команда чтения EXT_CSD")
        c1 = self.dev.read_pipe(P.EP_BULK_IN, 512)
        c2 = self.dev.read_pipe(P.EP_BULK_IN, 8)
        if len(c1) != 512 or len(c2) != 8:
            raise RuntimeError(f"Неполный ответ EXT_CSD: {len(c1) + len(c2)} из 520 байт")
        return (c1 + c2)[8:520]

    def write_extcsd_byte(self, index, value, switch_time_ms=0):
        command = P.switch_byte_cmd(index, value)
        self.dev.set_command_timeout(30000)
        try:
            if self.dev.write_pipe(P.EP_CMD_OUT, command) != len(command):
                raise RuntimeError("Неполная команда записи EXT_CSD; состояние поля неизвестно")
            response = self.dev.read_pipe(P.EP_CMD_IN, 8)
            if len(response) != 8 or response[:2] != b"\x27\x00":
                raise RuntimeError(f"Ошибка CMD6 EXT_CSD: {response.hex(' ') or 'нет ответа'}; состояние поля неизвестно")
            time.sleep(max(0.3, switch_time_ms / 1000))
        finally:
            self.dev.set_command_timeout(1500)

    def select(self, hex_cmd: str, delay: float = 0.05):
        """Переключение шины на раздел (BOOT1/BOOT2/RPMB/User)."""
        self.dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(hex_cmd))
        self.dev.read_pipe(P.EP_CMD_IN, 8)
        if delay > 0:
            time.sleep(delay)

    def select_partition(self, part_key: str, delay: float = 0.05):
        cmd = {
            "BOOT1": P.CMD_SELECT_BOOT1,
            "BOOT2": P.CMD_SELECT_BOOT2,
            "RPMB": P.CMD_SELECT_RPMB,
            "USER": P.CMD_SELECT_USER,
        }.get(part_key, P.CMD_SELECT_USER)
        self.select(cmd, delay)

    def select_user(self, delay: float = 0.05):
        self.select(P.CMD_SELECT_USER, delay)

    # --- RPMB ---

    def rpmb_probe(self) -> bytes:
        """Опрос RPMB-метки (заполнен ли Authentication Key). Возвращает хвост ответа."""
        dev = self.dev
        self.select(P.CMD_SELECT_RPMB, 0)

        req = bytearray(512)
        req[484:500] = P.RPMB_KEY_FRAME
        req[510:512] = b"\x00\x02"
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex("14000000000000000100000200000000"))
        dev.write_pipe(P.EP_DATA_OUT, bytes(req))
        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex("15000002000000000100000200000000"))
        dev.read_pipe(P.EP_BULK_IN, 512)
        tail = dev.read_pipe(P.EP_BULK_IN, 16)

        dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex("274600000007b302"))
        dev.read_pipe(P.EP_CMD_IN, 8)
        time.sleep(0.05)
        return tail

    # --- Единый чанк-движок чтения ---

    def start_read(self, start_lba, n_chunks):
        """Отправка WR/READ_SETUP для потока чтения."""
        self.dev.write_pipe(P.EP_CMD_OUT, bytes(P.read_setup(start_lba, n_chunks)))

    def iter_read_chunks(self, n_chunks):
        """Генератор чистых порций данных из BULK-потока чтения."""
        big = ctypes.create_string_buffer(P.CHUNK_BYTES)
        tail = ctypes.create_string_buffer(16)
        for _ in range(n_chunks):
            tb = self.dev.read_pipe_into(P.EP_BULK_IN, big)
            tt = self.dev.read_pipe_into(P.EP_BULK_IN, tail)
            if tb == P.CHUNK_BYTES and tt == 16:
                yield big.raw[16:] + tail.raw
            else:
                raise RuntimeError(f"Неполный USB-блок чтения: данные {tb}/{P.CHUNK_BYTES}, хвост {tt}/16 байт")

    def read_sectors(self, start_lba, n_blocks) -> bytes:
        """Выровненное чтение произвольного диапазона секторов в память."""
        base = start_lba - (start_lba % P.CHUNK_SECTORS)
        off = (start_lba - base) * P.SECTOR_SIZE
        n_chunks = (off + n_blocks * P.SECTOR_SIZE + P.CHUNK_BYTES - 1) // P.CHUNK_BYTES

        self.start_read(base, n_chunks)
        raw = bytearray()
        for chunk in self.iter_read_chunks(n_chunks):
            raw.extend(chunk)
        return bytes(raw)[off: off + n_blocks * P.SECTOR_SIZE]

    # --- Единый чанк-движок записи ---

    def start_write(self, start_lba, n_chunks, bus_width):
        self.dev.begin_write()
        try:
            self.dev.write_pipe(
                P.EP_CMD_OUT,
                bytes(P.write_setup(start_lba, n_chunks, bus_width)),
            )
        except Exception:
            self.dev.end_write()
            raise

    def send_write_chunk(self, chunk_idx, cur_lba, data: bytes):
        packet = bytes(P.write_chunk_header(chunk_idx, cur_lba)) + data
        self.dev.write_pipe(P.EP_DATA_OUT, packet)
        if chunk_idx > 0 and chunk_idx % P.SYNC_EVERY_CHUNKS == 0:
            self.dev.read_pipe(P.EP_CMD_IN, 64)

    def wait_write_complete(self, attempts=50, delay=0.02):
        """Ожидание завершения записи контроллером (опрос CMD13 через 0x39)."""
        try:
            for _ in range(attempts):
                self.dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.CMD_WRITE_POLL))
                resp = self.dev.read_pipe(P.EP_CMD_IN, 64)
                if len(resp) >= 10 and resp[9] == 0x09:
                    return True
                time.sleep(delay)
            return False
        finally:
            self.dev.end_write()

    # --- Стирание ---

    def erase_wait_ready(self, log_cb_msg=None, timeout=10.0):
        start_wait = time.time()
        while time.time() - start_wait < timeout:
            self.dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.CMD_ERASE_SETUP))
            status = self.dev.read_pipe(P.EP_CMD_IN, 8)
            if len(status) >= 8 and status[5] == 0x09 and status[6] == 0x00:
                return True
            time.sleep(0.05)
        return False
