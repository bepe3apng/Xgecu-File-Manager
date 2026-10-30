"""Декодирование регистров eMMC: CID, CSD и ECSD + справочники производителей."""
import struct

_MID = {
    0x02: "SanDisk", 0x11: "Toshiba/Kioxia", 0x13: "Micron", 0x15: "Samsung",
    0x45: "SanDisk", 0x70: "Kingston", 0x90: "SK Hynix", 0x9B: "YMTC",
    0xD6: "Foresee", 0xFE: "Micron", 0xF4: "Biwin",
}

_MMC_VERSIONS = {
    0: "MMC V4.0", 1: "MMC V4.1", 2: "MMC V4.2", 3: "MMC V4.3",
    4: "MMC V4.4", 5: "MMC V4.41", 6: "MMC V4.51", 7: "MMC V5.0", 8: "MMC V5.1",
}

_LIFE_TIME = {
    0x00: "Not defined",
    0x01: "0%-10% device life time used",
    0x02: "10%-20% device life time used",
    0x03: "20%-30% device life time used",
    0x04: "30%-40% device life time used",
    0x05: "40%-50% device life time used",
    0x06: "50%-60% device life time used",
    0x07: "60%-70% device life time used",
    0x08: "70%-80% device life time used",
    0x09: "80%-90% device life time used",
    0x0A: "90%-100% device life time used",
    0x0B: "Exceeded its maximum estimated device life time",
}

_PRE_EOL = {
    0x00: "Not defined",
    0x01: "Normal",
    0x02: "Warning (Consumed 80% of reserve)",
    0x03: "Urgent",
}


def dword_swap(data: bytes) -> bytearray:
    swapped = bytearray(len(data))
    for i in range(0, len(data), 4):
        swapped[i:i + 4] = data[i:i + 4][::-1]
    return swapped


def rpmb_status(code) -> tuple:
    """Строки статуса RPMB по коду ответа."""
    if code == 7:
        return ("Authentication Key not yet programmed, RPMB not used",
                "RPMB  Partition is Blank, not be Used.")
    if code == 0:
        return ("Authentication Key programmed, RPMB in use",
                "RPMB  Partition is Programmed, in use.")
    return (f"RPMB Response: 0x{code:04X}",
            f"RPMB  Partition status: 0x{code:04X}")


def decode_registers(cid_raw, ecsd) -> dict:
    """Декодирует CID/ECSD в структуру полей для отчёта и таблицы."""
    cid_blk = dword_swap(cid_raw[8:24])

    mid = cid_blk[0]
    pnm = cid_blk[3:9].decode('latin1', errors='ignore').strip('\x00')
    prv = cid_blk[9]
    psn = struct.unpack(">I", bytes(cid_blk[10:14]))[0]

    mdt = cid_blk[14]
    month = (mdt >> 4) & 0x0F
    year = 2013 + (mdt & 0x0F) if (mdt & 0x0F) <= 15 else 1997 + (mdt & 0x0F)
    prod_date = f"{month}-{year}"

    ext_rev = ecsd[192] if len(ecsd) > 192 else 0
    life_a = _LIFE_TIME.get(ecsd[268], "Not defined") if len(ecsd) > 268 else "Not defined"
    life_b = _LIFE_TIME.get(ecsd[269], "Not defined") if len(ecsd) > 269 else "Not defined"
    pre_eol = _PRE_EOL.get(ecsd[267], "Normal") if len(ecsd) > 267 else "Normal"
    dev_type = ecsd[196] if len(ecsd) > 196 else 0
    strobe = "YES" if (len(ecsd) > 184 and (ecsd[184] & 0x01)) else "NO"

    boot_mult = ecsd[226] if len(ecsd) > 226 else 0
    rpmb_mult = ecsd[168] if len(ecsd) > 168 else 0
    sec_count = struct.unpack("<I", ecsd[212:216])[0] if len(ecsd) > 216 else 0
    user_bytes = sec_count * 512

    return {
        "mid": mid,
        "mid_str": f"{mid:02X} ({_MID.get(mid, 'Unknown')})",
        "pnm": pnm,
        "prv": f"{(prv >> 4)}.{prv & 0x0F}",
        "psn": psn,
        "prod_date": prod_date,
        "ver_str": _MMC_VERSIONS.get(ext_rev, f"MMC V{ext_rev}"),
        "life_a": life_a,
        "life_b": life_b,
        "pre_eol": pre_eol,
        "dev_type": dev_type,
        "strobe": strobe,
        "boot_kb": boot_mult * 128,
        "boot_mult": boot_mult,
        "rpmb_kb": rpmb_mult * 128,
        "boot_sectors": (boot_mult * 128 * 1024) // 512,
        "sec_count": sec_count,
        "user_bytes": user_bytes,
        "user_mb": user_bytes // (1024 * 1024),
    }
