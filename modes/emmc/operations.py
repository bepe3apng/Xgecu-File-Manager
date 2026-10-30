"""Операции уровня сервиса eMMC: опрос, дамп, стирание и запись.

Каждая операция открывает собственную EmmcSession (контекст жизненного
цикла) и использует общий чанк-движок вместо дублированных циклов.
"""
import os
import struct
import time

from image_processor import ImageProcessor
from modes.emmc import partitions as parts_mod
from modes.emmc import registers
from modes.emmc import report as report_mod
from modes.emmc import protocol as P
from modes.emmc.session import EmmcSession
from modes.emmc.verification import verify_file


def _abort_hit(abort_check, log_cb, message):
    """Возвращает True, если операцию нужно прервать (с логом при отмене)."""
    if abort_check and abort_check():
        log_cb(message)
        return True
    return False


def _log_progress(log_cb, pct, last_marker, step):
    marker = f"  ... Прогресс: {pct}%"
    if pct // step != last_marker // step or (pct == 100 and last_marker != 100):
        log_cb(marker)
        return pct
    return last_marker


class _SpeedMeter:
    """Report byte-based progress at most ten times per second, outside the log."""

    def __init__(self, log_cb, action, total_bytes=None):
        self.log = log_cb
        self.report = getattr(log_cb, "progress", None)
        self.total_bytes = total_bytes
        self.started = self.last_time = self.last_report = time.perf_counter()
        self.total = self.last_bytes = 0
        self.speed = 0.0
        phase_cb = getattr(log_cb, "phase", None)
        if phase_cb:
            phase_cb(action)

    def _report(self):
        if self.report:
            pct = min(100.0, self.total * 100.0 / self.total_bytes) if self.total_bytes else 0.0
            self.report(pct, self.speed)

    def add(self, byte_count):
        self.total += byte_count
        now = time.perf_counter()
        interval = now - self.last_time
        if interval >= 1.0:
            self.speed = (self.total - self.last_bytes) / interval / (1024 * 1024)
            self.last_time, self.last_bytes = now, self.total
        if now - self.last_report >= 0.1:
            self._report()
            self.last_report = now

    def finish(self):
        elapsed = max(time.perf_counter() - self.started, 1e-9)
        # Keep the last measured speed; short operations use their average.
        if self.last_time == self.started:
            self.speed = self.total / elapsed / (1024 * 1024)
        self._report()
        self.log(f"    Данные: {self.total / (1024 * 1024):.2f} МиБ | Время: {elapsed:.2f} сек")


def _analyze_write_region(source, offset, length, erased_byte, log_cb, abort_check):
    """Build coalesced transfer runs before erase."""
    source.seek(offset)
    runs = []
    pattern = bytes([erased_byte]) * P.CHUNK_BYTES
    for position in range(0, length, P.CHUNK_BYTES):
        if _abort_hit(abort_check, log_cb, "[!] Анализ образа отменён."):
            return None
        size = min(P.CHUNK_BYTES, length - position)
        data = source.read(size)
        if len(data) != size:
            raise RuntimeError("Неполное чтение образа при анализе")
        skip = _fill_chunk(data) == pattern
        if runs and runs[-1][2] == skip:
            first, previous, _ = runs[-1]
            runs[-1] = (first, previous + size, skip)
        else:
            runs.append((position, size, skip))
    return runs


def _prepare_write_regions(path, regions, erased_byte, log_cb, abort_check):
    log_cb(f"[*] Предварительный анализ образа: заполнение {erased_byte:02X}...")
    meter = _SpeedMeter(log_cb, "анализа образа", sum(size for _, size in regions))
    plans = []
    with open(path, "rb") as source:
        for offset, length in regions:
            runs = _analyze_write_region(source, offset, length, erased_byte, log_cb, abort_check)
            if runs is None:
                return None
            plans.append(runs)
            meter.add(length)
    total = sum(size for _, size in regions)
    skipped = sum(size for runs in plans for _, size, skip in runs if skip)
    meter.finish()
    log_cb(f"[+] Анализ образа: к записи {total - skipped} байт; пропуск {skipped} байт "
           f"({skipped / (1024 * 1024):.2f} МиБ), заполнение {erased_byte:02X}.")
    return plans


def _write_region(session, source, start_lba, length, bus_width, meter,
                  log_cb, abort_check, erased_byte=None, write_plan=None):
    if write_plan is not None:
        origin = source.tell()
        skipped = 0
        for offset, size, skip in write_plan:
            if _abort_hit(abort_check, log_cb, "[!] Запись прервана пользователем."):
                return None
            if skip:
                skipped += size
                meter.add(size)
                continue
            source.seek(origin + offset)
            result = _write_region(session, source, start_lba + offset // P.SECTOR_SIZE,
                                   size, bus_width, meter, log_cb, abort_check)
            if result is None:
                return None
        return skipped

    """Write a file region, skipping only whole erased transfer chunks."""
    count = (length + P.CHUNK_BYTES - 1) // P.CHUNK_BYTES
    if erased_byte is None:
        # Never abandon an announced stream. Buffer <=1 MiB before starting it,
        # and honor STOP only after all its chunks and completion status arrive.
        for first in range(0, count, P.SYNC_EVERY_CHUNKS):
            if _abort_hit(abort_check, log_cb, "[!] Запись прервана пользователем."):
                return None
            batch = []
            for index in range(first, min(first + P.SYNC_EVERY_CHUNKS, count)):
                size = min(P.CHUNK_BYTES, length - index * P.CHUNK_BYTES)
                data = source.read(size)
                if len(data) != size:
                    raise RuntimeError("Файл изменился или неполное чтение образа")
                batch.append((_fill_chunk(data), size))
            if _abort_hit(abort_check, log_cb, "[!] Запись прервана пользователем."):
                return None
            base = start_lba + first * P.CHUNK_SECTORS
            session.start_write(base, len(batch), bus_width)
            for index, (data, size) in enumerate(batch):
                session.send_write_chunk(index, base + index * P.CHUNK_SECTORS, data)
                meter.add(size)
            if not session.wait_write_complete():
                raise RuntimeError("Тайм-аут завершения записи eMMC")
        if _abort_hit(abort_check, log_cb, "[!] Запись прервана пользователем."):
            return None
        return 0

    pattern = bytes([erased_byte]) * P.CHUNK_BYTES
    pending = []
    pending_lba = start_lba
    skipped = 0

    def flush():
        if not pending:
            return True
        if _abort_hit(abort_check, log_cb, "[!] Запись прервана пользователем."):
            return False
        session.start_write(pending_lba, len(pending), bus_width)
        for index, (data, size) in enumerate(pending):
            session.send_write_chunk(index, pending_lba + index * P.CHUNK_SECTORS, data)
            meter.add(size)
        if not session.wait_write_complete():
            raise RuntimeError("Тайм-аут завершения записи eMMC")
        pending.clear()
        return True

    for index in range(count):
        if _abort_hit(abort_check, log_cb, "[!] Запись прервана пользователем."):
            return None
        size = min(P.CHUNK_BYTES, length - index * P.CHUNK_BYTES)
        data = source.read(size)
        if len(data) != size:
            raise RuntimeError("Файл изменился или неполное чтение образа")
        data = _fill_chunk(data)
        if data == pattern:
            if not flush():
                return None
            skipped += size
            meter.add(size)
        else:
            if not pending:
                pending_lba = start_lba + index * P.CHUNK_SECTORS
            pending.append((data, size))
            if len(pending) == 64 and not flush():
                return None
    if not flush() or _abort_hit(abort_check, log_cb, "[!] Запись прервана пользователем."):
        return None
    return skipped


def _fill_chunk(data):
    return data.ljust(P.CHUNK_BYTES, b"\x00") if len(data) < P.CHUNK_BYTES else data


# ============================================================
# Опрос чипа и разметки
# ============================================================

def write_extcsd(log_cb, is_18v, bus_width, index, value, expected_data,
                 expected_cid, abort_check=None, allow_otp=False):
    from modes.emmc.extcsd import validate_write, WRITE_FIELDS
    old = validate_write(expected_data, index, value, allow_otp)
    if not expected_cid or len(expected_cid) != 32 or expected_cid[:2] != b"\x07\x00":
        raise ValueError("Перед записью EXT_CSD прочитайте регистр с подключённого чипа")
    if _abort_hit(abort_check, log_cb, "[!] Запись EXT_CSD отменена."):
        return None
    session = EmmcSession(log_cb)
    if not session.open():
        return None
    try:
        session.init_hw(is_18v, bus_width)
        cid, _, current = session.wake_registers(bus_width)
        if cid != expected_cid:
            raise RuntimeError("CID изменился; запись EXT_CSD заблокирована")
        if current[index] != old:
            raise RuntimeError("Исходное значение EXT_CSD изменилось; прочитайте регистр заново")
        validate_write(current, index, value, allow_otp)
        if _abort_hit(abort_check, log_cb, "[!] Запись EXT_CSD отменена."):
            return None
        if old == value:
            log_cb("[*] EXT_CSD: значение уже установлено, запись не требуется.")
            return bytes(current), cid
        phase = getattr(log_cb, "phase", None)
        if phase:
            phase("записи")
        log_cb(f"[*] EXT_CSD [{index}] {WRITE_FIELDS[index]}: {old:02X} → {value:02X}")
        if index == 162:
            log_cb("[!] RST_n: настройка может быть необратимой; чип может отклонить изменение. Результат проверяется чтением.")
        # Once CMD6 is sent, finish readback even if STOP is pressed.
        session.write_extcsd_byte(index, value, current[248] * 10)
        if phase:
            phase("верификации")
        actual = session.read_extcsd()
        if actual[index] != value:
            log_cb(f"[-] Ошибка верификации EXT_CSD [{index}]: ожидалось {value:02X}, прочитано {actual[index]:02X}. Продолжаем запись.")
            return bytes(actual), cid
        log_cb(f"[+] Запись EXT_CSD [{index}] подтверждена чтением: {value:02X}.")
        if index == 162:
            log_cb("[*] Значение RST_N_FUNCTION подтверждено; работа вывода RST_n аппаратно не проверялась.")
        return bytes(actual), cid
    finally:
        session.close()


def read_extcsd(log_cb, is_18v, bus_width, abort_check=None):
    from modes.emmc.extcsd import validate
    if _abort_hit(abort_check, log_cb, "[!] Чтение EXT_CSD отменено."):
        return None
    session = EmmcSession(log_cb)
    if not session.open():
        return None
    try:
        phase = getattr(log_cb, "phase", None)
        if phase:
            phase("чтения")
        session.init_hw(is_18v, bus_width)
        cid, _, data = session.wake_registers(bus_width)
        if _abort_hit(abort_check, log_cb, "[!] Чтение EXT_CSD отменено."):
            return None
        data = validate(data)
        log_cb("[+] EXT_CSD прочитан: 512 байт.")
        return data, cid
    finally:
        session.close()


def read_info_and_partitions(log_cb, is_18v, bus_width, abort_check=None):
    session = EmmcSession(log_cb)
    if not session.open():
        return None, []

    try:
        if _abort_hit(abort_check, log_cb, "[!] Операция отменена."):
            return None, []

        session.init_hw(is_18v, bus_width)
        cid_raw, csd_raw, ecsd = session.wake_registers(bus_width)
        if not cid_raw or len(cid_raw) < 24:
            log_cb("[-] Чип eMMC не отвечает (проверьте контакты/пайку).")
            return None, []

        reg = registers.decode_registers(cid_raw, ecsd)
        rpmb_tail = session.rpmb_probe()
        rpmb_code = struct.unpack(">H", rpmb_tail[12:14])[0] if len(rpmb_tail) >= 14 else 0xFFFF
        rpmb_status_line, _ = registers.rpmb_status(rpmb_code)

        info = {
            "extcsd": bytes(ecsd),
            "cid": bytes(cid_raw),
            "report": report_mod.build(reg, rpmb_status_line) + "\n  EXT_CSD — настройки:\n" + "\n".join(
                f"      [{index}] {name}: 0x{ecsd[index]:02X}"
                for index, name in ((177, "BOOT_BUS_WIDTH"), (178, "BOOT_CONFIG_PROT"),
                                    (179, "PARTITION_CONFIG"), (162, "RST_N_FUNCTION"),
                                    (160, "PARTITIONING_SUPPORT (только чтение)"))) + "\n",
            "mid": reg["mid_str"],
            "pnm": reg["pnm"],
            "boot_kb": reg["boot_kb"],
            "rpmb_kb": reg["rpmb_kb"],
            "user_mb": reg["user_mb"],
            "user_sectors": reg["sec_count"],
            "rpmb_status": rpmb_status_line,
        }

        if abort_check and abort_check():
            log_cb("[!] Операция отменена пользователем.")
            return info, []

        # --- Чтение разметки (GPT или MBR) ---
        log_cb("[*] Поиск разметки диска в User Area...")
        session.select_user(0.05)
        buf = session.read_sectors(0, 64)
        parts = parts_mod.parse(buf, session.read_sectors, log_cb, abort_check)
        return info, parts

    finally:
        session.close()


# ============================================================
# Дамп (чтение) областей
# ============================================================

def _dump_stream(session, start_lba, total_sectors, out_path,
                 log_cb, abort_check, progress_step):
    """Единый стрим чтения: setup -> чанки -> файл, с прогрессом и отменой."""
    if start_lba < 0 or total_sectors <= 0:
        raise ValueError("Некорректный диапазон чтения")
    n_chunks = (total_sectors * P.SECTOR_SIZE + P.CHUNK_BYTES - 1) // P.CHUNK_BYTES
    session.start_read(start_lba, n_chunks)

    bytes_to_write = total_sectors * P.SECTOR_SIZE
    bytes_written = 0
    last_pct = -1
    meter = _SpeedMeter(log_cb, "чтения", bytes_to_write)

    with open(out_path, "wb") as f:
        for i, chunk in enumerate(session.iter_read_chunks(n_chunks)):
            if _abort_hit(abort_check, log_cb, "[!] Чтение прервано пользователем."):
                return False
            if len(chunk) != P.CHUNK_BYTES:
                raise RuntimeError(f"Неполный блок чтения {i + 1}: {len(chunk)} из {P.CHUNK_BYTES} байт")
            to_write = min(len(chunk), bytes_to_write - bytes_written)
            f.write(chunk[:to_write])
            bytes_written += to_write
            meter.add(to_write)
            last_pct = _log_progress(log_cb, int(bytes_written * 100 / bytes_to_write),
                                     last_pct, progress_step)

    if bytes_written != bytes_to_write:
        raise RuntimeError(f"Неполный дамп: {bytes_written} из {bytes_to_write} байт")
    meter.finish()
    return True


def dump_boot(log_cb, is_18v, bus_width, part_name, size_bytes, out_path, abort_check=None, verify=False):
    if part_name not in ("BOOT1", "BOOT2"):
        raise ValueError("Для чтения BOOT выберите BOOT1 или BOOT2")
    session = EmmcSession(log_cb)
    if not session.open():
        return
    try:
        session.init_hw(is_18v, bus_width)
        _, _, ecsd = session.wake_registers(bus_width)
        capacity = ecsd[226] * 128 * 1024 if len(ecsd) > 226 else 0
        if capacity <= 0:
            raise RuntimeError(f"Не удалось определить размер {part_name} из EXT_CSD")
        # GUI passes None: always use the currently connected chip's geometry.
        size_bytes = capacity if size_bytes is None else size_bytes
        if size_bytes <= 0 or size_bytes > capacity or size_bytes % P.SECTOR_SIZE:
            raise ValueError(f"Некорректный размер дампа {part_name}: {size_bytes}; область {capacity} байт")
        log_cb(f"[*] Чтение {part_name}: {size_bytes} байт ({size_bytes / (1024 * 1024):.2f} МиБ).")
        session.select_partition(part_name, 0.1)
        if _dump_stream(session, 0, size_bytes // P.SECTOR_SIZE, out_path, log_cb, abort_check, 5):
            if verify and not verify_file(session, log_cb, is_18v, bus_width, part_name,
                                              out_path, [(0, 0, size_bytes)], abort_check):
                return
            session.select(P.CMD_SELECT_USER, 0)
            log_cb(f"[+] Дамп {part_name} успешно сохранен!")
            return True
    finally:
        session.close()


def dump_user_area(log_cb, is_18v, bus_width, out_path, abort_check=None, verify=False):
    session = EmmcSession(log_cb)
    if not session.open():
        return
    try:
        log_cb("[*] Инициализация eMMC перед чтением User Area...")
        session.init_hw(is_18v, bus_width)
        _, _, ecsd = session.wake_registers(bus_width)

        sec_count = struct.unpack("<I", ecsd[212:216])[0] if len(ecsd) > 216 else 0
        if sec_count == 0:
            log_cb("[-] Ошибка: Не удалось определить размер User Area из ECSD.")
            return

        size_mb = (sec_count * P.SECTOR_SIZE) / (1024 * 1024)
        log_cb(f"[*] Переключение в User Area и запуск чтения ({size_mb:.2f} MB)...")
        session.select_user(0.05)

        if _dump_stream(session, 0, sec_count, out_path, log_cb, abort_check, 5):
            if verify and not verify_file(session, log_cb, is_18v, bus_width, "USER",
                                              out_path, [(0, 0, sec_count * P.SECTOR_SIZE)], abort_check):
                return
            log_cb("[+] Дамп User Area успешно сохранен!")
            return True
    finally:
        session.close()


def dump_lba_range(log_cb, is_18v, bus_width, part_name, start_lba, end_lba,
                   out_path, abort_check=None, verify=False):
    session = EmmcSession(log_cb)
    if not session.open():
        return
    try:
        log_cb(f"[*] Инициализация оборудования перед чтением {part_name}...")
        session.init_hw(is_18v, bus_width)
        session.wake_registers(bus_width)

        total_sectors = end_lba - start_lba + 1
        size_mb = total_sectors * P.SECTOR_SIZE / (1024 * 1024)
        log_cb(f"[*] Чтение {part_name} [LBA: {start_lba} .. {end_lba}] ({size_mb:.2f} MB)...")
        session.select_user(0.05)

        if _dump_stream(session, start_lba, total_sectors, out_path,
                        log_cb, abort_check, 10):
            if verify and not verify_file(session, log_cb, is_18v, bus_width, "USER",
                                              out_path, [(start_lba, 0, total_sectors * P.SECTOR_SIZE)], abort_check):
                return
            log_cb(f"[+] Раздел {part_name} успешно прочитан и сохранен!")
    finally:
        session.close()


# ============================================================
# Стирание
# ============================================================

def erase_blocks(log_cb, is_18v, bus_width, part_type, start_lba, end_lba,
                 abort_check=None):
    session = EmmcSession(log_cb)
    if not session.open():
        return
    try:
        log_cb("[*] Инициализация оборудования для стирания...")
        session.init_hw(is_18v, bus_width)
        _, _, ecsd = session.wake_registers(bus_width)

        sec_count = struct.unpack("<I", ecsd[212:216])[0] if len(ecsd) > 216 else 0
        boot_sectors = (ecsd[226] * 128 * 1024) // 512 if len(ecsd) > 226 else 0

        if part_type == "USER" and end_lba <= 0:
            if sec_count > 0:
                end_lba = sec_count - 1
            else:
                log_cb("[-] Ошибка: Не удалось прочитать объем User Area из ECSD.")
                return
        elif part_type in ("BOOT1", "BOOT2") and end_lba <= 0:
            end_lba = (boot_sectors - 1) if boot_sectors > 0 else 8191

        session.select_partition(part_type)

        total_sectors = end_lba - start_lba + 1
        log_cb("[*] Стирание %s [LBA: %d .. %d] (%.1f MB)..." % (
            part_type, start_lba, end_lba, total_sectors * P.SECTOR_SIZE / (1024 * 1024)))

        cur_lba = start_lba
        erased_sectors = 0
        last_pct = -1
        start_time = time.time()
        meter = _SpeedMeter(log_cb, "стирания", total_sectors * P.SECTOR_SIZE)

        while cur_lba <= end_lba:
            if abort_check and abort_check():
                log_cb("[!] Стирание прервано пользователем.")
                return

            chunk_end = min(cur_lba + P.ERASE_CHUNK_SECTORS - 1, end_lba)

            session.dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.CMD_ERASE_SETUP))
            session.dev.read_pipe(P.EP_CMD_IN, 8)
            session.dev.write_pipe(P.EP_CMD_OUT, bytes(P.erase_range_cmd(cur_lba, chunk_end)))
            session.dev.read_pipe(P.EP_CMD_IN, 8)

            if not session.erase_wait_ready():
                log_cb(f"[!] Тайм-аут при стирании блока {cur_lba}..{chunk_end}")

            erased_sectors += (chunk_end - cur_lba + 1)
            meter.add((chunk_end - cur_lba + 1) * P.SECTOR_SIZE)
            cur_lba = chunk_end + 1

            pct = int((erased_sectors / total_sectors) * 100)
            if pct // 10 != last_pct // 10 or (pct == 100 and last_pct != 100):
                log_cb(f"  ... Стерто: {pct}%")
                last_pct = pct

        session.select_user(0.05)
        elapsed = time.time() - start_time
        meter.finish()
        log_cb(f"[+] Стирание {part_type} успешно завершено за {elapsed:.2f} сек!")
    finally:
        session.close()


# ============================================================
# Запись
# ============================================================

def _verify_erased(session, log_cb, part_name, start_lba, end_lba, erased_byte, abort_check):
    """Verify the partition's first 32 MiB, or its entirety if smaller."""
    if erased_byte not in (0x00, 0xFF):
        raise ValueError("Неизвестное содержимое стёртой eMMC; запись отменена")
    full_end_lba = end_lba
    end_lba = min(end_lba, start_lba + 32 * 1024 * 1024 // P.SECTOR_SIZE - 1)
    scope = "весь раздел" if end_lba == full_end_lba else "первые 32 МиБ раздела"
    log_cb(f"[*] Контроль стирания {part_name}: {scope}, LBA {start_lba}..{end_lba}, ожидается {erased_byte:02X}.")
    meter = _SpeedMeter(log_cb, "контроля стирания",
                        (end_lba - start_lba + 1) * P.SECTOR_SIZE)
    step = max(P.CHUNK_SECTORS, 1024 * 1024 // P.SECTOR_SIZE)
    for first in range(start_lba, end_lba + 1, step):
        if _abort_hit(abort_check, log_cb, "[!] Контроль стирания отменён; запись отменена."):
            return False
        sectors = min(step, end_lba - first + 1)
        data = session.read_sectors(first, sectors)
        size = sectors * P.SECTOR_SIZE
        if len(data) != size:
            raise RuntimeError(f"Неполное чтение контроля стирания LBA {first}: {len(data)}/{size}; запись отменена")
        if data != bytes([erased_byte]) * size:
            offset = next(i for i, value in enumerate(data) if value != erased_byte)
            address = first * P.SECTOR_SIZE + offset
            raise RuntimeError(
                f"Контроль стирания {part_name}: LBA {address // P.SECTOR_SIZE}, "
                f"адрес 0x{address:08X}: ожидалось {erased_byte:02X}, "
                f"прочитано {data[offset]:02X}; запись отменена")
        meter.add(size)
    if _abort_hit(abort_check, log_cb, "[!] Запись после контроля стирания отменена."):
        return False
    meter.finish()
    log_cb(f"[+] Контроль стирания {part_name}: проверены {scope}, {(end_lba - start_lba + 1) * P.SECTOR_SIZE / (1024 * 1024):.2f} МиБ.")
    return True


def _erase_before_write(session, log_cb, part_name, start_lba, end_lba, abort_check, erased_byte):
    """Erase the selected, validated area in the current hardware session."""
    log_cb(f"[*] Стирание перед записью: {part_name}, LBA {start_lba}..{end_lba}.")
    meter = _SpeedMeter(log_cb, "стирания", (end_lba - start_lba + 1) * P.SECTOR_SIZE)
    for first in range(start_lba, end_lba + 1, P.ERASE_CHUNK_SECTORS):
        if _abort_hit(abort_check, log_cb, "[!] Стирание перед записью отменено."):
            return False
        last = min(first + P.ERASE_CHUNK_SECTORS - 1, end_lba)
        session.dev.write_pipe(P.EP_CMD_OUT, bytes.fromhex(P.CMD_ERASE_SETUP))
        if len(session.dev.read_pipe(P.EP_CMD_IN, 8)) != 8:
            raise RuntimeError("Неполный ответ подготовки стирания; запись отменена")
        session.dev.write_pipe(P.EP_CMD_OUT, bytes(P.erase_range_cmd(first, last)))
        if len(session.dev.read_pipe(P.EP_CMD_IN, 8)) != 8:
            raise RuntimeError("Неполный ответ команды стирания; запись отменена")
        if not session.erase_wait_ready():
            raise RuntimeError(f"Тайм-аут стирания {first}..{last}; запись отменена")
        meter.add((last - first + 1) * P.SECTOR_SIZE)
    meter.finish()
    log_cb(f"[+] Стирание перед записью {part_name} завершено.")
    return _verify_erased(session, log_cb, part_name, start_lba, end_lba, erased_byte, abort_check)


def write_partition(log_cb, is_18v, bus_width, part_name, in_path, abort_check=None, verify=False,
                    erase_before=False):
    if not os.path.exists(in_path):
        log_cb(f"[-] Файл не найден: {in_path}")
        return

    file_size = os.path.getsize(in_path)
    if file_size == 0:
        log_cb("[-] Файл пустой, запись отменена.")
        return

    session = EmmcSession(log_cb)
    if not session.open():
        return
    try:
        log_cb(f"[*] Инициализация eMMC перед записью {part_name}...")
        session.init_hw(is_18v, bus_width)
        _, _, ecsd = session.wake_registers(bus_width)
        capacity = (ecsd[226] * 128 * 1024 if part_name in ("BOOT1", "BOOT2")
                    else int.from_bytes(ecsd[212:216], "little") * P.SECTOR_SIZE)
        transfer_bytes = ((file_size + P.CHUNK_BYTES - 1) // P.CHUNK_BYTES) * P.CHUNK_BYTES
        if capacity <= 0 or transfer_bytes > capacity:
            log_cb(f"[-] Ошибка: образ не помещается в {part_name} (файл {file_size}, область {capacity} байт).")
            return
        if file_size < capacity:
            log_cb(f"[*] Предупреждение: размер образа {part_name} меньше области ({file_size} < {capacity} байт).")

        if _abort_hit(abort_check, log_cb, "[!] Операция отменена пользователем."):
            return

        erased_byte = (0xFF if ecsd[181] & 1 else 0x00) if erase_before else None
        write_plans = None
        if erase_before:
            write_plans = _prepare_write_regions(in_path, [(0, file_size)], erased_byte,
                                                  log_cb, abort_check)
            if write_plans is None:
                return
        session.select_partition(part_name)
        if erase_before and not _erase_before_write(
                session, log_cb, part_name, 0, capacity // P.SECTOR_SIZE - 1, abort_check,
                0xFF if ecsd[181] & 1 else 0x00):
            return

        log_cb(f"[*] Запись {part_name}: {file_size / (1024*1024):.2f} MB...")
        meter = _SpeedMeter(log_cb, "записи", file_size)
        erased_byte = (0xFF if ecsd[181] & 1 else 0x00) if erase_before else None
        with open(in_path, "rb") as f:
            skipped = _write_region(session, f, 0, file_size, bus_width, meter,
                                    log_cb, abort_check, erased_byte,
                                    write_plan=write_plans[0] if write_plans is not None else None)
        if skipped is None:
            return
        if erase_before:
            log_cb(f"[*] Оптимизация записи {part_name}: пропущено {skipped / (1024*1024):.2f} МиБ заполнения {erased_byte:02X}.")
        session.select_user(0.05)

        meter.finish()
        if verify and not verify_file(session, log_cb, is_18v, bus_width, part_name,
                                          in_path, [(0, 0, file_size)], abort_check):
            return
        log_cb(f"[+] Запись {part_name} успешно завершена!")
        return True
    except Exception as e:
        log_cb(f"[!] Ошибка записи: {e}")
    finally:
        session.close()


def write_lba_range(log_cb, is_18v, bus_width, part_name, start_lba, end_lba,
                    in_path, abort_check=None, verify=False, erase_before=False):
    processor = ImageProcessor(chunk_bytes=P.CHUNK_BYTES, min_skip_bytes=1024 * 1024)
    try:
        log_cb(f"[*] Анализ файла перед записью: {in_path}...")
        plan = processor.analyze(in_path)
    except Exception as e:
        log_cb(f"[-] Ошибка анализа файла: {e}")
        return

    if not plan or not plan["segments"]:
        log_cb("[-] В файле нет полезных данных для записи (файл пуст или целиком состоит из нулей).")
        return

    total_mb = plan["total_bytes"] / (1024 * 1024)
    data_mb = plan["data_bytes"] / (1024 * 1024)
    skip_mb = plan["skip_bytes"] / (1024 * 1024)
    log_cb(f"[+] Формат: {plan['format'].upper()} | Исходный размер: {total_mb:.2f} MB")
    log_cb("[+] К записи: %.2f MB | Будет пропущено: %.2f MB (сегментов: %d)" % (
        data_mb, skip_mb, len(plan["segments"])))

    session = EmmcSession(log_cb)
    if not session.open():
        return
    try:
        log_cb(f"[*] Инициализация eMMC перед записью {part_name}...")
        session.init_hw(is_18v, bus_width)
        _, _, ecsd = session.wake_registers(bus_width)
        session.select_user(0.05)
        write_plans = None
        erased_byte = (0xFF if ecsd[181] & 1 else 0x00) if erase_before else None
        if erase_before:
            capacity = int.from_bytes(ecsd[212:216], "little")
            if not 0 <= start_lba <= end_lba < capacity:
                raise ValueError("Границы стирания выходят за User Area")
            group = ecsd[224] * 1024 if ecsd[175] & 1 else 0
            if not group or start_lba % group or (end_lba + 1) % group:
                raise ValueError("Стирание перед записью: раздел не выровнен по группе стирания eMMC")
            for rel_lba, _, seg_len in plan["segments"]:
                transfer = ((seg_len + P.CHUNK_BYTES - 1) // P.CHUNK_BYTES) * P.CHUNK_SECTORS
                if rel_lba < 0 or start_lba + rel_lba + transfer > end_lba + 1:
                    raise ValueError("Образ выходит за границы раздела; стирание отменено")
            write_plans = _prepare_write_regions(
                in_path, [(pos, size) for _, pos, size in plan["segments"]],
                erased_byte, log_cb, abort_check)
            if write_plans is None:
                return
            if not _erase_before_write(session, log_cb, part_name, start_lba, end_lba, abort_check,
                                       0xFF if ecsd[181] & 1 else 0x00):
                return

        verify_regions = []
        written_bytes = 0
        skipped_fill_bytes = 0
        meter = _SpeedMeter(log_cb, "записи", plan["data_bytes"])

        with open(in_path, "rb") as f:
            for seg_idx, (rel_lba, file_pos, seg_len) in enumerate(plan["segments"], 1):
                if abort_check and abort_check():
                    log_cb(f"[!] Запись {part_name} прервана пользователем.")
                    return

                seg_start_lba = start_lba + rel_lba
                if seg_start_lba > end_lba:
                    log_cb(f"[!] Сегмент {seg_idx} выходит за границы раздела, пропуск.")
                    continue

                allowed_bytes = (end_lba - seg_start_lba + 1) * P.SECTOR_SIZE
                actual_seg_len = min(seg_len, allowed_bytes)
                f.seek(file_pos)
                erased_byte = (0xFF if ecsd[181] & 1 else 0x00) if erase_before else None
                skipped = _write_region(session, f, seg_start_lba, actual_seg_len, bus_width,
                                        meter, log_cb, abort_check, erased_byte,
                                        write_plan=write_plans[seg_idx - 1] if write_plans is not None else None)
                if skipped is None:
                    return
                written_bytes += actual_seg_len
                skipped_fill_bytes += skipped
                verify_regions.append((seg_start_lba, file_pos, actual_seg_len))

                pct = int((written_bytes / plan["data_bytes"]) * 100)
                log_cb(f"  ... Записан сегмент [{seg_idx}/{len(plan['segments'])}] -> {pct}%")

        session.select_user(0.05)

        meter.finish()
        if verify and not verify_file(session, log_cb, is_18v, bus_width, "USER",
                                          in_path, verify_regions, abort_check):
            return
        log_cb(f"[+] Запись {part_name} успешно завершена!")
        log_cb(f"    Записано данных: {(written_bytes - skipped_fill_bytes) / (1024 * 1024):.2f} МиБ; пропуск заполнения: {skipped_fill_bytes / (1024 * 1024):.2f} МиБ; пропуск по карте образа: {skip_mb:.2f} МиБ")
    except Exception as e:
        log_cb(f"[!] Ошибка записи: {e}")
    finally:
        session.close()
