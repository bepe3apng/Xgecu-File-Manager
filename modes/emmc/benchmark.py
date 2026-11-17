"""Sequential User Area throughput tests; no image files or skipped fill blocks."""
import os
import time

from modes.emmc import protocol as P
from modes.emmc.session import EmmcSession
from modes.emmc.operations import _erase_before_write


def run_benchmark(log, is_18v, bus_width, kind, sample, abort_check):
    if kind not in ("read", "write"):
        raise ValueError("Unknown benchmark")
    session = EmmcSession(log)
    if not session.open():
        raise RuntimeError("T48 недоступен")
    try:
        session.init_hw(is_18v, bus_width)
        _, csd_raw, ecsd = session.wake_registers(bus_width)
        from .registers import user_sector_count
        sectors = user_sector_count(ecsd, csd_raw)
        if sectors <= 0 or sectors % P.CHUNK_SECTORS:
            raise ValueError("Некорректная геометрия User Area")
        if abort_check():
            return False
        session.select_partition("USER")
        if kind == "write":
            erased = 0xFF if ecsd[181] & 1 else 0x00
            if not _erase_before_write(session, log, "USER", 0, sectors - 1,
                                       abort_check, erased):
                return False
        phase = getattr(log, "phase", None)
        if phase:
            phase("чтения" if kind == "read" else "записи")
        # Generated once, outside measured I/O; every byte is transferred.
        pattern = os.urandom(P.CHUNK_BYTES) if kind == "write" else None
        total = sectors * P.SECTOR_SIZE
        done = 0
        started = previous = time.perf_counter()
        for lba in range(0, sectors, P.SYNC_EVERY_CHUNKS * P.CHUNK_SECTORS):
            if abort_check():
                log("[!] Тест остановлен. Выполненные изменения не отменены.")
                return False
            count = min(P.SYNC_EVERY_CHUNKS, (sectors - lba) // P.CHUNK_SECTORS)
            if kind == "read":
                session.start_read(lba, count)
                received = 0
                for block in session.iter_read_chunks(count):
                    if len(block) != P.CHUNK_BYTES:
                        raise RuntimeError("Неполный блок чтения")
                    received += 1
                if received != count:
                    raise RuntimeError("Неполный поток чтения")
            else:
                session.start_write(lba, count, bus_width)
                for index in range(count):
                    session.send_write_chunk(index, lba + index * P.CHUNK_SECTORS, pattern)
                if not session.wait_write_complete():
                    raise RuntimeError("Тайм-аут завершения записи")
            now = time.perf_counter()
            size = count * P.CHUNK_BYTES
            done += size
            speed = size / max(now - previous, 1e-9) / (1024 * 1024)
            previous = now
            sample(now - started, speed, done * 100.0 / total)
        if abort_check():
            log("[!] Тест остановлен.")
            return False
        elapsed = max(time.perf_counter() - started, 1e-9)
        log(f"[+] Тест завершён: {done / (1024 * 1024):.2f} МиБ, "
            f"{elapsed:.2f} с, средняя {done / elapsed / (1024 * 1024):.2f} МиБ/с.")
        if kind == "write":
            log("[*] Это тест скорости записи; проверка сохранённых данных не выполнялась.")
        return True
    finally:
        session.close()
