"""Формирование текстового отчёта опроса чипа (аналог оригинального ПО XGecu)."""

_DEVICE_TYPE_LINES = (
    (0x01, "High-Speed eMMC at 26 MHz"),
    (0x02, "High-Speed eMMC at 52 MHz"),
    (0x04, "High-Speed DDR eMMC at 52 MHz - 1.8V or 3V I/O"),
    (0x08, "High-Speed DDR eMMC at 52 MHz - 1.2V I/O"),
    (0x10, "HS200 SDR      - 1.8V I/O"),
    (0x20, "HS200 SDR      - 1.2V I/O"),
    (0x40, "HS400 DDR      - 1.8V I/O"),
    (0x80, "HS400 DDR      - 1.2V I/O"),
)


def build(reg: dict, rpmb_status_line: str) -> str:
    dt_flags = "".join(
        f"      {title:<46}: {'YES' if (reg['dev_type'] & flag) else 'NO'}\n"
        for flag, title in _DEVICE_TYPE_LINES
    )

    user_bytes = reg["user_bytes"]
    return (
        f"  <1> -- Production Info.and Device life time--\n\n"
        f"      MID          : {reg['mid_str']}\n"
        f"      PNM          : {reg['pnm']}\n"
        f"      Product Date : {reg['prod_date']}\n"
        f"      Version      : {reg['ver_str']}\n\n"
        f"      Device life time Type A : {reg['life_a']}\n"
        f"      Device life time Type B : {reg['life_b']}\n"
        f"      Device life time (PRE_EOL_INFO) : {reg['pre_eol']}\n\n"
        f"      High Speed Support - DEVICE_TYPE[196]\n\n"
        f"{dt_flags}"
        f"      HS400 STROBE_SUPPORT[184]        : {reg['strobe']}\n\n"
        f"  <2> -- Partition Size Info.--\n\n"
        f"      BOOT1 SIZE   : {reg['boot_kb']} KB\n"
        f"      BOOT2 SIZE   : {reg['boot_kb']} KB\n"
        f"      RPMB SIZE    : {reg['rpmb_kb']} KB\n"
        f"      GPP1 SIZE    : 0 KB\n"
        f"      GPP2 SIZE    : 0 KB\n"
        f"      GPP3 SIZE    : 0 KB\n"
        f"      GPP4 SIZE    : 0 KB\n"
        f"      USER SIZE    : {reg['user_mb']} MB\n"
        f"                      ( 0x {user_bytes >> 32:02X}_{user_bytes & 0xFFFFFFFF:08X} )\n\n"
        f"  <3> -- RPMB Security and Password --\n\n"
        f"      {rpmb_status_line}\n\n"
    )
