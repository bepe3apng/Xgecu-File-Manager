"""EXT_CSD inspection and validated configuration writes."""

FIELDS = (
    (192, 1, "EXT_CSD_REV"), (194, 1, "CSD_STRUCTURE"),
    (196, 1, "DEVICE_TYPE"), (212, 4, "SEC_COUNT"),
    (226, 1, "BOOT_SIZE_MULT"), (168, 1, "RPMB_SIZE_MULT"),
    (179, 1, "PARTITION_CONFIG"), (177, 1, "BOOT_BUS_WIDTH"),
    (178, 1, "BOOT_CONFIG_PROT"), (173, 1, "BOOT_WP"),
    (174, 1, "BOOT_WP_STATUS"), (171, 1, "USER_WP"),
    (183, 1, "BUS_WIDTH"), (185, 1, "HS_TIMING"),
    (175, 1, "ERASE_GROUP_DEF"), (224, 1, "HC_ERASE_GRP_SIZE"),
    (221, 1, "HC_WP_GRP_SIZE"), (181, 1, "ERASED_MEM_CONT"),
    (160, 1, "PARTITIONING_SUPPORT"),
    (155, 1, "PARTITION_SETTING_COMPLETED"),
    (156, 1, "PARTITIONS_ATTRIBUTE"),
    (157, 3, "MAX_ENH_SIZE_MULT"), (136, 4, "ENH_START_ADDR"),
    (140, 3, "ENH_SIZE_MULT"),
    (143, 3, "GP_SIZE_MULT_1"), (146, 3, "GP_SIZE_MULT_2"),
    (149, 3, "GP_SIZE_MULT_3"), (152, 3, "GP_SIZE_MULT_4"),
    (166, 1, "WR_REL_PARAM"), (167, 1, "WR_REL_SET"),
    (222, 1, "REL_WR_SEC_C"), (162, 1, "RST_N_FUNCTION"),
    (163, 1, "BKOPS_EN"), (246, 1, "BKOPS_STATUS"),
    (33, 1, "CACHE_CTRL"), (249, 4, "CACHE_SIZE"),
    (61, 1, "DATA_SECTOR_SIZE"), (63, 1, "NATIVE_SECTOR_SIZE"),
    (267, 1, "PRE_EOL_INFO"),
    (268, 1, "DEVICE_LIFE_TIME_EST_TYP_A"),
    (269, 1, "DEVICE_LIFE_TIME_EST_TYP_B"),
    (308, 1, "CMDQ_SUPPORT"), (307, 1, "CMDQ_DEPTH"),
)
REVISIONS = {0: "1.0 / MMC 4.0", 1: "1.1 / MMC 4.1",
             2: "1.2 / MMC 4.2", 3: "1.3 / MMC 4.3",
             5: "1.5 / MMC 4.41", 6: "1.6 / MMC 4.5",
             7: "1.7 / MMC 5.0", 8: "1.8 / MMC 5.1"}


# Only configuration bytes whose CMD6 semantics are implemented here.
WRITE_FIELDS = {
    179: "PARTITION_CONFIG",
    177: "BOOT_BUS_WIDTH",
    178: "BOOT_CONFIG_PROT",
    162: "RST_N_FUNCTION",
}


def validate_write(data, index, value, allow_otp=False):
    data = validate(data)
    if index == 160:
        raise ValueError("PARTITIONING_SUPPORT [160] — поле только для чтения")
    if index not in WRITE_FIELDS:
        raise ValueError("Запись этого поля EXT_CSD не поддерживается")
    if not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError("Значение должно быть байтом HEX: 00–FF")
    if data[192] < 3:
        raise ValueError("Запись поддерживается для EXT_CSD_REV >= 3")
    old = data[index]
    if index in (177, 179) and data[178] & 0x11 and old != value:
        raise ValueError("Настройки загрузки защищены BOOT_CONFIG_PROT [178]")
    if index == 179:
        if value & 0x80 or ((value >> 3) & 7) not in (0, 1, 2, 7):
            raise ValueError("PARTITION_CONFIG: допустимы BOOT_ENABLE 0, 1, 2 или 7; бит 7 зарезервирован")
        if (value & 7) != (old & 7):
            raise ValueError("PARTITION_ACCESS (биты 2:0) нужно оставить без изменения")
    elif index == 177:
        if value & 0xE0 or (value & 3) == 3 or ((value >> 3) & 3) == 3:
            raise ValueError("BOOT_BUS_WIDTH: зарезервированное значение")
    elif index == 178:
        if value & ~0x11:
            raise ValueError("BOOT_CONFIG_PROT: допустимы только биты 0 и 4")
        if old & ~value:
            raise ValueError("BOOT_CONFIG_PROT: установленную защиту нельзя снять записью")
        if (value & 0x10) and not (old & 0x10) and not allow_otp:
            raise ValueError("Требуется подтверждение постоянной защиты BOOT_CONFIG_PROT")
    elif index == 162:
        if value not in (0, 1, 2):
            raise ValueError("RST_N_FUNCTION: 00 — исходное, 01 — включить, 02 — отключить")
        if old != value and not allow_otp:
            raise ValueError("Требуется подтверждение попытки изменения RST_N_FUNCTION; настройка может быть необратимой")
    return old


def write_plan(current, target):
    """Validate every file setting before any write; protection goes last."""
    current, target = validate(current), validate(target)
    simulated = bytearray(current)
    plan = []
    for index in (177, 179, 162, 178):
        value = target[index]
        if index == 179:
            value = (value & ~7) | (current[index] & 7)
        if value == simulated[index]:
            continue
        old = validate_write(simulated, index, value, allow_otp=True)
        plan.append((index, old, value))
        simulated[index] = value
    return plan


def load_file(path):
    """Accept raw EXT_CSD or EXT_CSD + CID + CSD (512 + 16 + 16)."""
    with open(path, "rb") as stream:
        raw = stream.read(545)
    if len(raw) not in (512, 544):
        raise ValueError(f"Файл должен содержать 512 или 544 байта, получено {len(raw)}")
    return raw[:512], raw[512:]


def validate(data):
    data = bytes(data)
    if len(data) != 512:
        raise ValueError(f"EXT_CSD должен содержать ровно 512 байт, получено {len(data)}")
    return data


def field_name(offset):
    for start, size, name in FIELDS:
        if start <= offset < start + size:
            return name
    return "Reserved / другое поле"


def describe(data):
    data = validate(data)
    rev = data[192]
    sectors = int.from_bytes(data[212:216], "little")
    lines = [
        f"EXT_CSD: 512 байт; версия {REVISIONS.get(rev, 'неизвестная')} (REV={rev})",
        f"User Area: {sectors} секторов / {sectors * 512 / 1024**3:.3f} ГиБ",
        f"BOOT1 и BOOT2: по {data[226] * 128} КиБ; RPMB: {data[168] * 128} КиБ",
        "Многобайтовые числа: little-endian. Смещения: десятичные.",
        "Снимок получен после инициализации; BUS_WIDTH и HS_TIMING могут быть изменены ею.",
        "Поля зависят от версии eMMC; нули не доказывают поддержку функции.",
        "",
    ]
    for offset, size, name in FIELDS:
        value = int.from_bytes(data[offset:offset + size], "little")
        detail = ""
        if name == "PARTITION_CONFIG":
            detail = f"  ACK={(value >> 6) & 1}, BOOT_ENABLE={(value >> 3) & 7}, ACCESS={value & 7}"
        elif name == "PRE_EOL_INFO":
            detail = "  " + {0: "не определено", 1: "норма", 2: "предупреждение", 3: "критический уровень"}.get(value, "зарезервировано")
        elif name.startswith("DEVICE_LIFE_TIME"):
            detail = ("  оценка не указана" if value == 0 else
                      f"  износ {(value - 1) * 10}–{value * 10}%" if value <= 10 else
                      "  ресурс превышен" if value == 11 else "  зарезервировано")
        lines.append(f"[{offset:03d}] {name}: 0x{value:0{size * 2}X} ({value}){detail}")
    lines.extend(["", "HEX — смещение слева шестнадцатеричное:"])
    for offset in range(0, 512, 16):
        lines.append(f"{offset:03X}: " + data[offset:offset + 16].hex(" ").upper())
    return "\n".join(lines)


def compare(current, reference):
    current, reference = validate(current), validate(reference)
    differences = [i for i in range(512) if current[i] != reference[i]]
    lines = [f"Различающихся байт: {len(differences)} из 512.",
             "Текущий снимок → выбранный файл:"]
    lines.extend(f"[{i:03d}] {field_name(i)}: {current[i]:02X} → {reference[i]:02X}"
                 for i in differences)
    return "\n".join(lines)
