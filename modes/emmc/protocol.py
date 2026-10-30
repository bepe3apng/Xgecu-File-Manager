"""Протокол обмена с программатором XGecu T48 в режиме eMMC.

Все hex-команды, адреса конечных точек и константы геометрии
собраны здесь, чтобы остальной код не содержал магических строк.
"""
import struct

# --- Геометрия ---
SECTOR_SIZE = 512
CHUNK_BYTES = 16384            # 16 КБ = 32 сектора за один чанк
CHUNK_SECTORS = CHUNK_BYTES // SECTOR_SIZE
SYNC_EVERY_CHUNKS = 64         # статус-опрос на рубеже каждого 1 МБ
ERASE_CHUNK_SECTORS = 131072   # 64 МБ за одну erase-команду

# --- Адреса конечных точек USB ---
EP_CMD_OUT = 0x01
EP_DATA_OUT = 0x02
EP_CMD_IN = 0x81
EP_BULK_IN = 0x82

# --- Служебные команды ---
CMD_POWER_OFF = "0400000000000000"
CMD_LEGACY_TIMING = "274600000000b903"
# Application setting; each session takes its own snapshot at creation.
DEFAULT_CLOCK_MHZ = 30
DEFAULT_LEGACY_30 = True
DEFAULT_PRESET = "Авто"
PRESET_SETUP = {
    "стандартный": {False: "3e01300027010700", True: "3e01300027010700"},
    "THGA": {False: "3e01100000090000", True: "3e01100000080000"},
}
CLOCK_SELECTORS = {8: "00", 16: "02", 20: "03", 30: "04"}
CMD_SELECT_BOOT1 = "274600000001b301"
CMD_SELECT_BOOT2 = "274600000002b301"
CMD_SELECT_RPMB = "274600000003b301"
CMD_SELECT_USER = "274600000007b302"
CMD_ERASE_SETUP = "274d010000000100"
CMD_WRITE_POLL = "3900000000000000"

# --- RPMB: провайдер-ключ кадра запроса ---
RPMB_KEY_FRAME = bytes.fromhex("3b084ab3558990c4bccd9f486189a7bd")

# --- Шаблоны пакетов ---
CFG_TEMPLATE = bytes.fromhex(
    "033100000005a100200000005400200000020000000000000300000003000000"
    "0000000000000000000000e51000000000000000000000003808000000000054"
)
READ_SETUP_TEMPLATE = bytes.fromhex(
    "0d010000000000000002000020000000000100002000000020000000010000000100000000000000"
)
WRITE_SETUP_TEMPLATE = bytes.fromhex(
    "1f010000000000000002000020000000000100002000000020000000040000000100000000000000"
)


def switch_byte_cmd(index, value):
    """T48 CMD6, WRITE_BYTE: argument 0x03<index><value>00, little-endian."""
    if not (isinstance(index, int) and 0 <= index < 512
            and isinstance(value, int) and 0 <= value <= 255):
        raise ValueError("Некорректный индекс или значение EXT_CSD")
    # CMD6 encodes the index in eight bits; bytes 256–511 are read-only.
    if index > 255:
        raise ValueError("CMD6 не поддерживает запись индексов выше 255")
    return bytes((0x27, 0x46, 0, 0, 0, value, index, 3))


def volt_flag(is_18v):
    return 0x01 if is_18v else 0x00


def bus_hex(bus_width):
    """Параметр частоты команд 05/06/21: 00 соответствует 8 МГц.

    Захваты штатной утилиты показывают, что 04 выбирает 30 МГц,
    а не ширину шины. Способ задания ширины пока не установлен.
    """
    return "00"


def bus_flag(bus_width):
    """Числовой флаг шины для WRITE_SETUP."""
    return 4 if bus_width == "4-bit" else 1


def build_init_config(is_18v, bus_width="4-bit", clock_mhz=8, preset=None):
    if clock_mhz not in CLOCK_SELECTORS:
        raise ValueError("Поддерживаются частоты 8, 16, 20 и 30 МГц")
    preset = DEFAULT_PRESET if preset is None else preset
    if preset not in PRESET_SETUP:
        raise ValueError(f"Неизвестный пресет eMMC: {preset}")
    cfg = bytearray(CFG_TEMPLATE)
    cfg[4] = cfg[21] = volt_flag(is_18v)
    cfg[12] = cfg[63] = 0x54 if bus_width == "4-bit" else 0x51
    if preset == "THGA" and clock_mhz == 30:
        # Capture verified by probe_30_legacy_read.py at VCCQ 1.8 V.
        cfg[23] = 0xC0
        cfg[62] = 0x01
    return cfg


def config_accept_cmd(is_18v):
    return "39310000" + f"{volt_flag(is_18v):02x}" + "05a100"


def wake_cid_cmd(bus_width, clock_mhz=8):
    return f"05{CLOCK_SELECTORS[clock_mhz]}000000000000"


def wake_csd_cmd(bus_width, clock_mhz=8):
    return f"06{CLOCK_SELECTORS[clock_mhz]}000000000000"


def bus_mode_cmd(bus_width, clock_mhz=8):
    return f"21{CLOCK_SELECTORS[clock_mhz]}000000000000"


def read_setup(start_lba, n_chunks):
    setup = bytearray(READ_SETUP_TEMPLATE)
    struct.pack_into("<I", setup, 4, start_lba)
    struct.pack_into("<I", setup, 16, n_chunks)
    return setup


def write_setup(start_lba, n_chunks, bus_width):
    setup = bytearray(WRITE_SETUP_TEMPLATE)
    struct.pack_into("<I", setup, 4, start_lba)
    struct.pack_into("<I", setup, 8, SECTOR_SIZE)
    struct.pack_into("<I", setup, 12, CHUNK_SECTORS)
    struct.pack_into("<I", setup, 16, n_chunks)
    struct.pack_into("<I", setup, 28, bus_flag(bus_width))
    return setup


def write_chunk_header(chunk_idx, cur_lba):
    header = bytearray(16)
    header[0] = 0x02 if (chunk_idx > 0 and chunk_idx % SYNC_EVERY_CHUNKS == 0) else 0x00
    header[2] = 0x10 if (chunk_idx % 2 == 0) else 0x00
    struct.pack_into("<I", header, 4, cur_lba)
    struct.pack_into("<I", header, 8, SECTOR_SIZE)
    struct.pack_into("<I", header, 12, CHUNK_SECTORS)
    return header


def erase_range_cmd(start_lba, end_lba):
    cmd = bytearray(16)
    cmd[0] = 0x0E
    struct.pack_into("<I", cmd, 4, start_lba)
    struct.pack_into("<I", cmd, 8, end_lba)
    return cmd
