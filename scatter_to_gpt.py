import struct
import zlib
import uuid
import re
import os
import xml.etree.ElementTree as ET

LINUX_DATA_GUID = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")

def parse_xml_scatter(scatter_text: str, target_storage: str = "EMMC"):
    """Парсер современного XML-скаттера MTK (V2.x.x)."""
    try:
        root = ET.fromstring(scatter_text)
    except Exception:
        return []

    # Ищем блок выбранного типа памяти (по умолчанию EMMC)
    storage_elem = None
    for st in root.findall("storage_type"):
        if st.get("name", "").upper() == target_storage.upper():
            storage_elem = st
            break

    if storage_elem is None:
        storage_elem = root.find("storage_type") or root

    partitions = []
    for part_elem in storage_elem.findall("partition_index"):
        name_elem = part_elem.find("partition_name")
        region_elem = part_elem.find("region")
        start_elem = part_elem.find("physical_start_addr")
        size_elem = part_elem.find("partition_size")
        is_reserved_elem = part_elem.find("is_reserved")
        op_type_elem = part_elem.find("operation_type")

        if name_elem is None or start_elem is None or size_elem is None:
            continue

        name = (name_elem.text or "").strip()
        region = (region_elem.text or "EMMC_USER").strip() if region_elem is not None else "EMMC_USER"
        is_reserved = (is_reserved_elem.text or "false").strip().lower() if is_reserved_elem is not None else "false"
        op_type = (op_type_elem.text or "").strip().upper() if op_type_elem is not None else ""

        # Пропускаем аппаратные BOOT-регионы
        if "BOOT" in region.upper():
            continue
        if region.upper() not in ("EMMC_USER", "USER", "UFS_LU2"):
            continue

        # Пропускаем служебные заголовки, SGPT и зарезервированные области
        if name.lower() in ("pgpt", "sgpt", "preloader", "preloader_backup", "flashinfo"):
            continue
        if is_reserved == "true" or op_type == "RESERVED":
            continue

        try:
            start_addr = int(start_elem.text.strip(), 0)
            size_bytes = int(size_elem.text.strip(), 0)
        except (ValueError, TypeError):
            continue

        if size_bytes == 0:
            continue

        # Отсекаем адреса-заглушки вида 0xFFFF0000
        if 0xFFFF0000 <= start_addr <= 0xFFFFFFFF:
            continue

        partitions.append({
            "name": name,
            "start_lba": start_addr // 512,
            "end_lba": (start_addr + size_bytes) // 512 - 1,
            "size_bytes": size_bytes
        })

    partitions.sort(key=lambda x: x["start_lba"])
    return partitions


def parse_yaml_text_scatter(scatter_text: str):
    """Парсер классического текстового / YAML скаттера MTK."""
    partitions = []
    blocks = re.split(r'(?=- partition_index:|- partition_name:)', scatter_text)
    if len(blocks) <= 1:
        blocks = scatter_text.split('\n\n')

    for block in blocks:
        name_m = re.search(r'partition_name:\s*([^\r\n]+)', block)
        region_m = re.search(r'region:\s*([^\r\n]+)', block)
        start_m = re.search(r'physical_start_addr:\s*(0x[0-9a-fA-F]+|[0-9]+)', block)
        size_m = re.search(r'partition_size:\s*(0x[0-9a-fA-F]+|[0-9]+)', block)
        res_m = re.search(r'is_reserved:\s*([^\r\n]+)', block)

        if not (name_m and start_m and size_m):
            continue

        name = name_m.group(1).strip()
        region = region_m.group(1).strip() if region_m else "EMMC_USER"
        is_reserved = res_m.group(1).strip().lower() if res_m else "false"

        if "BOOT" in region.upper():
            continue
        if name.lower() in ("pgpt", "sgpt", "preloader", "preloader_backup", "flashinfo"):
            continue
        if is_reserved == "true":
            continue

        start_addr = int(start_m.group(1), 0)
        size_bytes = int(size_m.group(1), 0)

        if size_bytes == 0 or (0xFFFF0000 <= start_addr <= 0xFFFFFFFF):
            continue

        partitions.append({
            "name": name,
            "start_lba": start_addr // 512,
            "end_lba": (start_addr + size_bytes) // 512 - 1,
            "size_bytes": size_bytes
        })

    partitions.sort(key=lambda x: x["start_lba"])
    return partitions


def parse_mtk_scatter(scatter_text: str, target_storage: str = "EMMC"):
    """Универсальный парсер: определяет формат скаттера (XML или текст/YAML)."""
    clean_text = scatter_text.strip()
    if clean_text.startswith("<?xml") or "<storage_type" in clean_text or "<root>" in clean_text:
        return parse_xml_scatter(clean_text, target_storage)
    return parse_yaml_text_scatter(clean_text)


def create_gpt_binary(partitions: list, total_disk_sectors: int) -> bytes:
    """Генерация бинарного образа таблицы GPT (LBA 0..33) объемом 17408 байт."""
    SECTOR_SIZE = 512
    NUM_ENTRIES = 128
    ENTRY_SIZE = 128
    ENTRIES_SIZE_BYTES = NUM_ENTRIES * ENTRY_SIZE

    # --- 1. LBA 0: Protective MBR ---
    mbr = bytearray(SECTOR_SIZE)
    mbr_part1 = bytearray(16)
    mbr_part1[4] = 0xEE  # GPT Protective
    struct.pack_into("<I", mbr_part1, 8, 1)
    struct.pack_into("<I", mbr_part1, 12, min(total_disk_sectors - 1, 0xFFFFFFFF))
    mbr[0x1BE:0x1CE] = mbr_part1
    mbr[0x1FE:0x200] = b"\x55\xAA"

    # --- 2. LBA 2..33: Partition Entries Array ---
    entries = bytearray(ENTRIES_SIZE_BYTES)
    for i, p in enumerate(partitions[:NUM_ENTRIES]):
        entry = bytearray(ENTRY_SIZE)
        entry[0x00:0x10] = LINUX_DATA_GUID.bytes_le
        entry[0x10:0x20] = uuid.uuid4().bytes_le
        struct.pack_into("<Q", entry, 0x20, p["start_lba"])
        struct.pack_into("<Q", entry, 0x28, p["end_lba"])
        struct.pack_into("<Q", entry, 0x30, 0)
        name_bytes = p["name"].encode("utf-16le")[:72]
        entry[0x38:0x38 + len(name_bytes)] = name_bytes
        entries[i * ENTRY_SIZE : (i + 1) * ENTRY_SIZE] = entry

    entries_crc32 = zlib.crc32(entries) & 0xFFFFFFFF

    # --- 3. LBA 1: GPT Header ---
    header = bytearray(SECTOR_SIZE)
    struct.pack_into("<8s", header, 0x00, b"EFI PART")
    struct.pack_into("<I", header, 0x08, 0x00010000)
    struct.pack_into("<I", header, 0x0C, 92)
    struct.pack_into("<I", header, 0x10, 0)
    struct.pack_into("<I", header, 0x14, 0)
    struct.pack_into("<Q", header, 0x18, 1)                      # Current LBA = 1
    struct.pack_into("<Q", header, 0x20, total_disk_sectors - 1) # Backup LBA
    struct.pack_into("<Q", header, 0x28, 34)                     # First usable LBA
    struct.pack_into("<Q", header, 0x30, total_disk_sectors - 34) # Last usable LBA
    header[0x38:0x48] = uuid.uuid4().bytes_le                    # Disk GUID
    struct.pack_into("<Q", header, 0x48, 2)                      # Partition entries LBA = 2
    struct.pack_into("<I", header, 0x50, NUM_ENTRIES)            # 128 entries
    struct.pack_into("<I", header, 0x54, ENTRY_SIZE)             # 128 bytes
    struct.pack_into("<I", header, 0x58, entries_crc32)

    header_crc32 = zlib.crc32(header[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 0x10, header_crc32)

    return bytes(mbr + header + entries)


def convert_scatter_to_gpt(scatter_path: str, output_gpt_path: str, user_total_mb: int = 0, log_cb=print):
    if not os.path.exists(scatter_path):
        log_cb(f"[-] Файл scatter не найден: {scatter_path}")
        return False

    with open(scatter_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    parts = parse_mtk_scatter(text, target_storage="EMMC")
    if not parts:
        log_cb("[-] Не удалось найти разделы EMMC_USER в scatter-файле.")
        return False

    log_cb(f"[+] Распознано разделов EMMC_USER: {len(parts)}")
    for p in parts[:6]:
        log_cb(f"    - {p['name']:<16} LBA: {p['start_lba']:<10} .. {p['end_lba']:<10} ({p['size_bytes'] / (1024*1024):.1f} MB)")
    if len(parts) > 6:
        log_cb(f"    ... и ещё {len(parts) - 6} разделов.")

    last_lba = max(p["end_lba"] for p in parts)
    total_sectors = (user_total_mb * 1024 * 1024) // 512 if user_total_mb > 0 else (last_lba + 35)
    if total_sectors < last_lba + 35:
        total_sectors = last_lba + 35

    gpt_bin = create_gpt_binary(parts, total_sectors)

    with open(output_gpt_path, "wb") as f:
        f.write(gpt_bin)

    return True

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Использование: python scatter_to_gpt.py <MT6789_Android_scatter.xml> [output_gpt.bin]")
    else:
        in_scatter = sys.argv[1]
        out_bin = sys.argv[2] if len(sys.argv) > 2 else "gpt.bin"
        convert_scatter_to_gpt(in_scatter, out_bin)