"""Streaming readback comparison after an eMMC power cycle."""
import os

from modes.emmc import protocol as P


def verify_file(session, log_cb, is_18v, bus_width, area, path, regions,
                abort_check=None):
    """Regions are (device LBA, file offset, byte length); compare source bytes."""
    from modes.emmc.operations import _SpeedMeter

    if abort_check and abort_check():
        log_cb("[!] Верификация отменена пользователем.")
        return False
    phase_cb = getattr(log_cb, "phase", None)
    if phase_cb:
        phase_cb("верификации")
    log_cb("[*] Верификация: перезапуск eMMC...")
    session.restart(is_18v, bus_width)
    session.select_partition(area)
    total = sum(length for _, _, length in regions)
    if total <= 0:
        raise RuntimeError("Пустой диапазон верификации")
    meter = _SpeedMeter(log_cb, "верификации", total)
    if meter.report:
        meter.report(0, 0)
    errors = 0
    with open(path, "rb") as source:
        for start_lba, file_offset, length in regions:
            if file_offset < 0 or length <= 0 or file_offset + length > os.fstat(source.fileno()).st_size:
                raise RuntimeError("Размер файла изменился перед верификацией")
            source.seek(file_offset)
            count = (length + P.CHUNK_BYTES - 1) // P.CHUNK_BYTES
            session.start_read(start_lba, count)
            compared = 0
            for chunk in session.iter_read_chunks(count):
                if abort_check and abort_check():
                    log_cb("[!] Верификация отменена пользователем.")
                    return False
                if len(chunk) != P.CHUNK_BYTES or compared >= length:
                    raise RuntimeError("Неполный или лишний блок при верификации")
                size = min(P.CHUNK_BYTES, length - compared)
                expected = source.read(size)
                if len(expected) != size:
                    raise RuntimeError("Неполное чтение файла при верификации")
                actual = chunk[:size]
                if actual != expected:
                    errors += 1
                    if errors <= 50:
                        first = next(i for i, (a, b) in enumerate(zip(actual, expected)) if a != b)
                        address = start_lba * P.SECTOR_SIZE + compared + first
                        log_cb(f"[-] Ошибка верификации: {area}, адрес 0x{address:X}, "
                               f"файл 0x{file_offset + compared + first:X}: "
                               f"ожидалось {expected[first]:02X}, прочитано {actual[first]:02X}")
                compared += size
                meter.add(size)
            if compared != length:
                raise RuntimeError(f"Неполная верификация: {compared} из {length} байт")
    meter.finish()
    if errors:
        log_cb(f"[-] Верификация не пройдена: блоков с несовпадениями — {errors}; "
               f"показано — {min(errors, 50)}.")
        return False
    log_cb("[+] Верификация пройдена: данные eMMC совпадают с файлом.")
    return True
