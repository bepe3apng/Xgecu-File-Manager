"""Анализ разметки User Area: GPT и MBR."""
import struct

_FS_SIGNATURES = (
    (0x438, b'\x53\xEF', 'EXT4'), (0x400, b'\x10\x20\xF5\xF2', 'F2FS'),
    (0x400, b'\xE2\xE1\xF5\xE0', 'EROFS'), (0x000, b'hsqs', 'SquashFS'),
    (0x003, b'NTFS    ', 'NTFS'), (0x003, b'EXFAT   ', 'exFAT'),
    (0x052, b'FAT32   ', 'FAT32'), (0x036, b'FAT', 'FAT16/12'),
)

_MBR_TYPE_NAMES = {
    0x83: "Linux", 0x07: "NTFS/exFAT", 0x0B: "FAT32",
    0x0C: "FAT32-LBA", 0x05: "Extended", 0x0F: "Ext-LBA",
}


def fs_magic(sec) -> str:
    if not sec or sec.count(0) > len(sec) - 4:
        return "(blank)"
    if sec[0:4] == b"ANDR":
        return "android-boot"
    if sec[0:8] == b"\x88\x16\x88\x58" or sec[0:4] == b"\x3a\xff\x26\xed":
        return "sparse-img"
    for sig_offset, sig_bytes, fs_name in _FS_SIGNATURES:
        if len(sec) >= sig_offset + len(sig_bytes) and \
                sec[sig_offset:sig_offset + len(sig_bytes)] == sig_bytes:
            return fs_name
    return "?"


def _sample_fs(sample_fn, lba, default):
    try:
        return fs_magic(sample_fn(lba, 4))
    except Exception:
        return default


def _gpt_row(first, last, name, fs, is_table=False):
    return {
        "name": name,
        "first": first,
        "last": last,
        "size": (last - first + 1) * 512 / (1024 * 1024),
        "fs": fs,
        **({"is_table": True} if is_table else {}),
    }


def parse_gpt(buf, sample_fn, abort_check=None) -> list:
    """Разбор GPT-заголовка и таблицы записей из первых 64 секторов."""
    gpt = buf[512:1024]
    entry_lba = struct.unpack_from("<Q", gpt, 72)[0]
    num = struct.unpack_from("<I", gpt, 80)[0]
    esize = struct.unpack_from("<I", gpt, 84)[0]
    gpt_table_sectors = int(entry_lba + (num * esize + 511) // 512)

    parts = [_gpt_row(0, gpt_table_sectors - 1, "Таблица разметки GPT", "GPT", True)]

    for i in range(min(num, 128)):
        if abort_check and abort_check():
            break
        off = entry_lba * 512 + i * esize
        if off + esize > len(buf):
            break
        e = buf[off:off + esize]
        if e[:16] == b"\x00" * 16:
            continue
        f_lba, l_lba = struct.unpack_from("<Q", e, 32)[0], struct.unpack_from("<Q", e, 40)[0]
        name = e[56:128].decode("utf-16-le", "replace").rstrip("\x00") or f"part_{i+1}"
        parts.append(_gpt_row(f_lba, l_lba, name, _sample_fs(sample_fn, f_lba, "err")))
    return parts


def parse_mbr(buf, sample_fn, abort_check=None) -> list:
    """Разбор таблицы разделов DOS/MBR из LBA 0."""
    parts = [_gpt_row(0, 0, "Таблица разметки MBR", "MBR", True)]

    for i in range(4):
        if abort_check and abort_check():
            break
        entry = buf[446 + i * 16: 446 + (i + 1) * 16]
        part_type = entry[4]
        if part_type == 0x00:
            continue
        first_lba, num_sectors = struct.unpack_from("<II", entry, 8)
        if num_sectors == 0:
            continue
        type_str = _MBR_TYPE_NAMES.get(part_type, f"Type 0x{part_type:02X}")
        parts.append(_gpt_row(
            first_lba, first_lba + num_sectors - 1,
            f"p{i+1} [{type_str}]",
            _sample_fs(sample_fn, first_lba, "?"),
        ))
    return parts


def parse(buf, sample_fn, log_cb, abort_check=None) -> list:
    """Определяет тип разметки и возвращает список строк таблицы."""
    if len(buf) >= 1024 and buf[512:520] == b"EFI PART":
        log_cb("[+] Обнаружена GPT разметка.")
        return parse_gpt(buf, sample_fn, abort_check)
    if len(buf) >= 512 and buf[510:512] == b"\x55\xAA":
        log_cb("[+] Обнаружена MBR разметка (DOS/MBR Table).")
        return parse_mbr(buf, sample_fn, abort_check)
    log_cb("[-] Разметка GPT и MBR не обнаружена.")
    return []
