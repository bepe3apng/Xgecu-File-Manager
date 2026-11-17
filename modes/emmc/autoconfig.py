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
    reference = samples = selected = None
    check_single = bus_width == "4-bit"
    standard = next(preset for preset in P.PRESET_SETUP if preset != "THGA")

    def cancelled():
        event = getattr(session.dev, "stop_event", None)
        if event is not None and event.is_set():
            raise RuntimeError("Автоподбор отменён (STOP)")

    def reset():
        cancelled()
        try:
            session.dev.power_off()
        except Exception:
            cancelled()

    def probe(preset, clock, legacy=False):
        cancelled()
        reset()
        session.preset, session.clock_mhz, session.legacy_30 = preset, clock, legacy
        session.init_hw(is_18v, bus_width)
        cid, csd, ext = session.wake_registers(bus_width)
        from .registers import user_sector_count
        sectors = user_sector_count(ext, csd)
        if len(ext) != 512 or sectors < 3 * P.CHUNK_SECTORS:
            raise RuntimeError("Некорректная ёмкость EXT_CSD")
        identity = (cid, csd, ext[192], ext[196], ext[212:216], ext[226], ext[181])
        if reference is not None and identity != reference:
            raise RuntimeError("CID/CSD или геометрия не совпали с чтением на 8 МГц")
        session.select_user()
        positions = (0, ((sectors // 2) // P.CHUNK_SECTORS) * P.CHUNK_SECTORS,
                     ((sectors - P.CHUNK_SECTORS) // P.CHUNK_SECTORS) * P.CHUNK_SECTORS)
        # Reject corrupt modes before entering the asynchronous 0D handler.
        if check_single:
            singles = []
            for index, lba in enumerate(positions):
                cancelled()
                block = session.read_single_sector(lba)
                if len(block) != P.SECTOR_SIZE:
                    raise RuntimeError("Неполное предварительное чтение CMD17")
                if samples is not None and block != samples[index][:P.SECTOR_SIZE]:
                    raise RuntimeError("CMD17 не совпал с эталоном; поток не запускался")
                singles.append(block)
            singles = tuple(singles)
        data = tuple(session.read_sectors(lba, P.CHUNK_SECTORS) for lba in positions)
        if check_single and singles != tuple(block[:P.SECTOR_SIZE] for block in data):
            raise RuntimeError("Данные CMD17 и потокового чтения не совпали")
        if any(len(block) != P.CHUNK_BYTES for block in data):
            raise RuntimeError("Неполное контрольное чтение")
        repeated = tuple(session.read_sectors(lba, P.CHUNK_SECTORS) for lba in positions)
        if data != repeated or (samples is not None and data != samples):
            raise RuntimeError("Контрольные чтения не совпали")
        cancelled()
        return identity, data, sectors

    def read_check(blocks, expected=None):
        cancelled()
        data = session.read_sectors(0, blocks)
        if len(data) != blocks * P.SECTOR_SIZE:
            raise RuntimeError("Неполное контрольное чтение")
        if expected is not None and data != expected:
            raise RuntimeError("Контрольное чтение не совпало с эталоном 8 МГц")
        cancelled()
        return data

    try:
        session.log("[*] Авто: первый проверенный режим: THGA Legacy30 → стандартный HS30"
                    " → стандартный20 → стандартный8; эталон на стандартном8.")
        identity, data, sectors = probe(standard, 8)
        blocks = min(2048, sectors // P.CHUNK_SECTORS * P.CHUNK_SECTORS)
        quick_reference = None
        if check_single:
            quick_reference = read_check(blocks)
            read_check(blocks, quick_reference)
        reference, samples = identity, data
        candidates = [("THGA", 30, True), (standard, 30, False)] if is_18v else []
        candidates += [(standard, 20, False), (standard, 8, False)]
        for candidate in candidates:
            try:
                probe(*candidate)
                if check_single:
                    read_check(blocks, quick_reference)
                selected = candidate
                break
            except OSError:
                raise
            except Exception as error:
                cancelled()
                session.log(f"[*] Авто: {candidate} не прошёл проверку: {error}")
        if selected is None:
            raise RuntimeError("Авто: ни один режим не прошёл проверку чтением")
        session.log(f"[+] Авто: выбран {selected[0]}, {selected[1]} МГц"
                    + (" Legacy (эксп.)" if selected[2] else "")
                    + ". Проверка чтением пройдена.")
        # Preserve the init/wake contract used by existing operation callers.
        reset()
        session.preset, session.clock_mhz, session.legacy_30 = selected
        session.init_hw(is_18v, bus_width)
        cancelled()
        _selected = ((is_18v, bus_width), selected)
    except Exception:
        session.preset, session.clock_mhz, session.legacy_30 = original
        raise
