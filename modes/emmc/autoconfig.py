"""Read-only configuration selection; never infer reliability from init alone."""
from modes.emmc import protocol as P


_selected = None


def forget():
    """Explicit Init starts a fresh selection; never retain failed old settings."""
    global _selected
    _selected = None


def remembered(is_18v, bus_width):
    if _selected is not None and _selected[0] == (is_18v, bus_width):
        return _selected[1]
    return None


def detect(session, is_18v, bus_width):
    global _selected
    forget()
    original = session.preset, session.clock_mhz, session.legacy_30
    reference = None
    baseline = None
    selected = None
    samples = None

    def cancelled():
        event = getattr(session.dev, "stop_event", None)
        if event is not None and event.is_set():
            raise RuntimeError("Автоподбор отменён (STOP)")

    def reset():
        cancelled()
        try:
            session.dev.power_off()
        except Exception:
            # Some T48 versions do not acknowledge power-off.
            cancelled()

    def probe(preset, clock, legacy=False):
        cancelled()
        reset()
        session.preset, session.clock_mhz, session.legacy_30 = preset, clock, legacy
        session.init_hw(is_18v, bus_width)
        cid, csd, ext = session.wake_registers(bus_width)
        sectors = int.from_bytes(ext[212:216], "little")
        if len(ext) != 512 or sectors < 3 * P.CHUNK_SECTORS:
            raise RuntimeError("Некорректная ёмкость EXT_CSD")
        # Compare immutable identity/geometry; timing and partition access can change.
        identity = (cid, csd, ext[192], ext[196], ext[212:216], ext[226], ext[181])
        if reference is not None and identity != reference:
            raise RuntimeError("CID/CSD или геометрия не совпали с чтением на 8 МГц")
        session.select_user()
        positions = (0, ((sectors // 2) // P.CHUNK_SECTORS) * P.CHUNK_SECTORS,
                     ((sectors - P.CHUNK_SECTORS) // P.CHUNK_SECTORS) * P.CHUNK_SECTORS)
        data = tuple(session.read_sectors(lba, P.CHUNK_SECTORS) for lba in positions)
        if any(len(block) != P.CHUNK_BYTES for block in data):
            raise RuntimeError("Неполное контрольное чтение")
        repeated = tuple(session.read_sectors(lba, P.CHUNK_SECTORS) for lba in positions)
        if data != repeated or (samples is not None and data != samples):
            raise RuntimeError("Контрольные чтения не совпали")
        return identity, data

    try:
        session.log("[*] Авто: подбор пресета и частоты по регистрам и контрольному чтению.")
        for preset in P.PRESET_SETUP:
            try:
                reference, samples = probe(preset, 8)
                baseline = (preset, 8, False)
                selected = baseline
                break
            except Exception as error:
                cancelled()
                session.log(f"[*] Авто: {preset}, 8 МГц не прошёл проверку: {error}")
        if baseline is None:
            raise RuntimeError("Авто: ни один пресет не прошёл проверку на 8 МГц")
        # Prefer the fastest validated mode; standard/HS precedes THGA Legacy.
        candidates = []
        for clock in ((30, 20, 16) if is_18v else (20, 16)):
            for preset in P.PRESET_SETUP:
                candidates.append((preset, clock, False))
            if clock == 30:
                candidates.append(("THGA", 30, True))
        for candidate in candidates:
            try:
                probe(*candidate)
                selected = candidate
                break
            except Exception as error:
                cancelled()
                session.log(f"[*] Авто: {candidate} не прошёл проверку: {error}")
        # Restore and revalidate the winner after any failed probes.
        probe(*selected)
        session.log(f"[+] Авто: выбран {selected[0]}, {selected[1]} МГц"
                    + (" Legacy (эксп.)" if selected[2] else "")
                    + ". Проверка чтением пройдена.")
        # Caller still performs wake_registers normally.
        reset()
        session.preset, session.clock_mhz, session.legacy_30 = selected
        session.init_hw(is_18v, bus_width)
        _selected = ((is_18v, bus_width), selected)
    except Exception:
        session.preset, session.clock_mhz, session.legacy_30 = original
        raise
