"""Experimental native firmware/CID service through T48 SCV3."""
import hashlib
import json
from pathlib import Path
import struct
import time
import uuid

from .session import EmmcSession
from .registers import dword_swap
from .firmware import firmware_header_version, inspect_firmware, prepare_firmware, _rstn_guard

SOURCE_SHA = "79adf81e384993b55b0f4df03bac91b6a90a91ace40f3221cc59c442ee741396"
PATCHED_SHA = "47eae2162b7169c39e9100a5ef3197a848c10608861db090e2bbde95f5c10e05"
STOCK_GATE_SHA = "4fe82e0f42c165e807ccdb1f3832eb0afbbb7d6aacb80549dddfcce83abf3428"
SCV3 = 0x33564353
R1_ERRORS = 0xfdf94080


def crc7(data):
    value = 0
    for byte in data:
        for bit in range(7, -1, -1):
            feedback = ((value >> 6) ^ (byte >> bit)) & 1
            value = (value << 1) & 127
            if feedback:
                value ^= 9
    return (value << 1) | 1


def cid_equal(a, b):
    return len(a) == len(b) == 16 and a[:15] == b[:15] and (a[15] & 254) == (b[15] & 254)


def parse_cid(text, current):
    try:
        target = bytearray.fromhex(text)
    except ValueError as error:
        raise ValueError("CID должен содержать 32 шестнадцатеричных символа") from error
    if len(target) != 16 or len(current) != 16:
        raise ValueError("CID должен содержать ровно 16 байт")
    target[15] = crc7(target[:15])
    if cid_equal(target, current):
        raise ValueError("Новый CID совпадает с текущим")
    return bytes(target)


def _snapshot(cid_response, csd_response, ext):
    cid = dword_swap(cid_response[8:24])
    if len(cid) != 16 or len(ext) != 512 or not int.from_bytes(ext[212:216], "little"):
        raise ValueError("Некорректные регистры CID/EXT_CSD или ёмкость eMMC")
    firmware = ext[254:262].hex()
    try:
        header_version = firmware_header_version(dict(firmware=firmware))
    except ValueError:
        # Reading registers does not require a supported firmware download profile.
        header_version = None
    return dict(cid=cid.hex(), cid_response=cid_response.hex(), csd_response=csd_response.hex(),
                ext_csd=ext.hex(), firmware=firmware, ffu_status=ext[26],
                header_version=header_version,
                sectors_programmed=int.from_bytes(ext[302:306], "little"))


class ServiceSession:
    def __init__(self, log, folder, abort_check=None):
        self.log = log
        self.abort_check = abort_check or (lambda: False)
        self.session = EmmcSession(log_cb=log, clock_mhz=8, preset="THGA")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stem = folder / ("emmc_service_" + str(time.time_ns()) + "_" + uuid.uuid4().hex)
        self.path = stem.with_suffix(".json")
        self.journal = stem.with_suffix(".jsonl").open("x", encoding="utf-8", buffering=1)
        self.result = dict(ok=False, settings="THGA / 8 MHz / 1-bit / VCCQ 1.8 V",
                           journal=str(stem.with_suffix(".jsonl")), readbacks=[])
        self.critical = False
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8")

    def cmd(self, number, argument=0, count=0, data=None):
        request = bytearray(32)
        request[:2] = bytes((0x27, number))
        struct.pack_into("<H", request, 2, 0x5343)
        struct.pack_into("<I", request, 4, argument)
        struct.pack_into("<I", request, 12, count)
        if data is not None:
            if len(data) != 16:
                raise ValueError("Ожидается порция 16 байт")
            request[8] = 16
            request[16:] = data
        dev = self.session.dev
        if dev.write_pipe(1, bytes(request)) != 32:
            raise RuntimeError("Неполная команда USB")
        response = dev.read_pipe(0x81, 8)
        self.journal.write(json.dumps(dict(time=time.time(), request=request.hex(), response=response.hex())) + "\n")
        if len(response) != 8 or response[:2] != b"\x27\x00":
            raise RuntimeError("Ошибка транспорта SCV3: " + response.hex())
        value = int.from_bytes(response[4:], "little")
        if number not in (60, 61, 63) and value & R1_ERRORS:
            raise RuntimeError(f"CMD{number}: R1 ERROR {value:#x}")
        return value

    def read(self):
        return _snapshot(*self.session.wake_registers("1-bit"))

    def open(self):
        if not self.session.open():
            raise RuntimeError("T48 не найден или занят")
        self.session.init_hw(True, "1-bit")
        if self.cmd(63) != SCV3:
            raise RuntimeError("Нужна прошивка T48 с расширением SCV3; автоматической перепрошивки T48 нет")
        return self.read()

    def ready(self, seconds=10):
        deadline = time.monotonic() + seconds
        while True:
            state = self.cmd(13, 0xffffffff)
            if state & 0x100 and ((state >> 9) & 15) == 4:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Истёк срок ожидания готовности eMMC")
            time.sleep(.01)

    def begin(self):
        if self.abort_check():
            raise RuntimeError("Операция отменена до записи")
        self.session.dev.begin_write()
        self.critical = True
        self.log("[*] Сервисная запись: STOP будет выполнен после передачи и проверки.")

    def cycle(self):
        # Same device retains begin_write protection across close/open, even on STOP.
        dev = self.session.dev
        if dev.write_pipe(1, bytes.fromhex("0400000000000000")) != 8:
            raise RuntimeError("Не удалось отправить power-off")
        try:
            if not dev.read_pipe(0x81, 16):
                raise RuntimeError("Нет ответа на power-off")
        except OSError as error:
            if getattr(error, "winerror", None) != 121 and error.errno != 121:
                raise
            self.log("[*] Power-off: тайм-аут ответа 121; повторное открытие USB.")
        finally:
            dev.close()
        time.sleep(.3)
        if not self.session.open():
            raise RuntimeError("Не удалось открыть T48 после power-off")
        self.session.init_hw(True, "1-bit")
        actual = self.read()
        self.result["readbacks"].append(actual)
        self.save()
        return actual

    def finish(self, error=None):
        if error:
            self.result["error"] = repr(error)
        try:
            self.session.close()
        finally:
            if self.critical:
                self.session.dev.end_write()
            self.save()
            self.journal.close()
            self.log("[*] Сервисный отчёт: " + str(self.path))


def _check_firmware_access(service, snapshot, *, allow_previous_failure=False):
    ext = bytes.fromhex(snapshot["ext_csd"])
    if ext[192] < 7:
        raise RuntimeError(f"FFU требует EXT_CSD_REV >= 7; прочитано {ext[192]}")
    if not ext[493] & 1:
        raise RuntimeError(f"FFU не поддерживается: SUPPORTED_MODES[493]={ext[493]:#x}")
    if ext[169] & 1:
        raise RuntimeError(f"FFU запрещено: FW_CONFIG[169]={ext[169]:#x}")
    status = ext[26]
    if allow_previous_failure:
        # FFU_STATUS reports the previous update result, not an FFU-disable bit.
        service.result["previous_ffu_status"] = status
        service.save()
        if status:
            service.log(f"[*] Предыдущая ошибка FFU_STATUS={status:#x}; новая загрузка будет проверена отдельно.")
    elif status:
        raise RuntimeError(f"Ошибка текущего этапа: FFU_STATUS={status:#x}")
    if ext[183] != 0 or service.cmd(60, 4) & 0x1800:
        raise RuntimeError("Настройка 1-bit не подтверждена")


def _phase(service, name):
    phase = dict(name=name, status="started", readbacks=[])
    service.result["phase"] = name
    service.result.setdefault("phases", []).append(phase)
    service.save()
    return phase


def _download_firmware(service, image, phase, progress_start=0, progress_end=100):
    # Caller holds begin_write across all phases, including stock restoration.
    service.cmd(6, 0x031e0100)
    service.ready()
    ext = service.session.read_extcsd()
    if ext[30] != 1:
        raise RuntimeError("MODE_CONFIG не подтвердил вход в FFU")
    sectors = len(image) // 512
    address = int.from_bytes(ext[487:491], "little")
    phase.update(sectors=sectors, ffu_argument=address)
    service.save()
    service.cmd(23, sectors)
    service.cmd(25, address, count=sectors)
    start = time.monotonic()
    for offset in range(0, len(image), 16):
        if time.monotonic() - start > 180:
            raise RuntimeError("Истёк срок передачи firmware")
        if service.cmd(61, offset, data=image[offset:offset+16]) != offset + 16:
            raise RuntimeError("Неверный счётчик передачи firmware")
        if (offset + 16) % 16384 == 0:
            phase["bytes_acknowledged"] = offset + 16
            service.save()
            progress = getattr(service.log, "progress", None)
            if progress:
                progress(progress_start + (progress_end - progress_start) * (offset + 16) / len(image), 0)
    service.ready()
    ext = service.session.read_extcsd()
    phase.update(ext_after_download=ext.hex(), bytes_acknowledged=len(image), status="downloaded")
    service.save()
    if ext[26]:
        raise RuntimeError(f"FFU_STATUS={ext[26]:#x}")
    # Native image applies without an explicit install CMD6.


def _verify_cycles(service, wanted, baseline, phase, expected_firmware="0300000000000000", expected_rstn=None):
    for _ in range(2):
        actual = service.cycle()
        actual["phase"] = phase["name"]
        phase["readbacks"].append(actual)
        cid = bytes.fromhex(actual["cid"])
        actual["cid_matches"] = cid_equal(cid, wanted)
        actual["crc7_valid"] = (crc7(cid[:15]) & 254) == (cid[15] & 254)
        service.save()
        if not actual["cid_matches"] or not actual["crc7_valid"]:
            raise RuntimeError("CID/CRC7 после перезапуска не совпал с ожидаемым")
        if actual["firmware"] != expected_firmware or actual["ffu_status"]:
            raise RuntimeError("Firmware/status после перезапуска не прошли проверку")
        if actual["csd_response"] != baseline["csd_response"] or bytes.fromhex(actual["ext_csd"])[212:216] != bytes.fromhex(baseline["ext_csd"])[212:216]:
            raise RuntimeError("CSD или ёмкость изменились после перезапуска")
    if expected_rstn is not None and any(bytes.fromhex(r["ext_csd"])[162] != expected_rstn for r in phase["readbacks"]):
        raise RuntimeError("RST_n после перезапуска не совпал с ожидаемым")
    phase["status"] = "verified"
    service.save()
    return actual


def run_service(log, action, folder, expected=None, firmware_path="", cid_text="", abort_check=None):
    """Called only through the serialized driver/orchestrator worker."""
    if action == "vmx0_reset":
        from .vmx0_service import run_reset
        return run_reset(log, folder, expected, abort_check)
    if action == "rpmb":
        from .rpmb import run_reset
        return run_reset(log, folder, expected, firmware_path, abort_check)
    if action not in ("probe", "firmware", "cid", "rstn"):
        raise ValueError("Неизвестная сервисная операция")
    expected = dict(expected) if expected else None
    if action != "probe" and expected is None:
        raise ValueError("Сначала прочитайте чип в сервисном окне")
    target = parse_cid(cid_text, bytes.fromhex(expected["cid"])) if action == "cid" else None
    reset_rstn = action == "rstn"
    restore = action in ("cid", "rstn")
    if reset_rstn and bytes.fromhex(expected["ext_csd"])[162] not in (1, 2):
        raise ValueError("RST_N_FUNCTION должен быть 1 или 2 для сброса")
    profile = inspect_firmware(firmware_path)[1] if action != "probe" else None
    # Preflight from the confirmed snapshot; regenerate from the fresh init below.
    image = prepare_firmware(firmware_path, snapshot=expected, vendor_patch=not reset_rstn, rstn_patch=reset_rstn) if action != "probe" else None
    stock = prepare_firmware(firmware_path, vendor_patch=False, snapshot=expected) if restore else None
    service = ServiceSession(log, folder, abort_check)
    r = service.result
    r["operation"] = action
    failure = None
    try:
        before = service.open()
        r["before"] = before
        service.save()
        if action == "probe":
            actual = before
        else:
            if not cid_equal(bytes.fromhex(before["cid"]), bytes.fromhex(expected["cid"])) or before["firmware"] != expected["firmware"]:
                raise RuntimeError("CID или firmware изменились после чтения; перечитайте чип")
            if before["csd_response"] != expected["csd_response"] or bytes.fromhex(before["ext_csd"])[212:216] != bytes.fromhex(expected["ext_csd"])[212:216]:
                raise RuntimeError("CSD или ёмкость изменились после чтения; перечитайте чип")
            if reset_rstn and bytes.fromhex(before["ext_csd"])[162] != bytes.fromhex(expected["ext_csd"])[162]:
                raise RuntimeError("RST_N_FUNCTION изменился; перечитайте чип")
            fresh_profile = inspect_firmware(firmware_path)[1]
            fresh_image = prepare_firmware(firmware_path, snapshot=before, vendor_patch=not reset_rstn, rstn_patch=reset_rstn)
            fresh_stock = prepare_firmware(firmware_path, vendor_patch=False, snapshot=before) if restore else None
            if fresh_image != image or fresh_stock != stock or fresh_profile != profile:
                raise RuntimeError("Подготовленные образы изменились; повторите чтение чипа")
            image, stock = fresh_image, fresh_stock
            if reset_rstn:
                r["rstn_patch"] = _rstn_guard(inspect_firmware(firmware_path)[0])
            r.update(firmware_profile=profile, header_version=max(profile["source_version"], firmware_header_version(before)),
                     header_version_policy="max(source_version, initial_chip_version)",
                     header_version_semantics="anti_rollback_assumed",
                     initial_chip_firmware=before["firmware"])
            service.ready()
            _check_firmware_access(service, before, allow_previous_failure=True)
            candidate = service.path.with_suffix(".firmware.bin")
            with candidate.open("xb") as output:
                output.write(image)
            r.update(candidate=str(candidate), source_sha256=profile["source_sha256"], candidate_sha256=hashlib.sha256(image).hexdigest())
            if stock is not None:
                stock_path = service.path.with_suffix(".stock_gate.firmware.bin")
                with stock_path.open("xb") as output:
                    output.write(stock)
                r.update(requested_cid=target.hex() if target else before["cid"], stock_gate_candidate=str(stock_path),
                         stock_gate_sha256=hashlib.sha256(stock).hexdigest(), stock_restore_verified=False,
                         physical_power_removal_measured=False)
            service.save()
            service.begin()
            if reset_rstn:
                r["rstn_sequence_started"] = True
                r["rstn_before"] = bytes.fromhex(before["ext_csd"])[162]
                service.save()
            if action == "cid":
                r["cid_sequence_started"] = True
                service.save()
                log("[*] 1/3: загрузка vendor-патча и проверка исходного CID.")
            phase = _phase(service, "patched_firmware")
            _download_firmware(service, image, phase, 0, 40 if stock is not None else 100)
            actual = _verify_cycles(service, bytes.fromhex(before["cid"]), before, phase, expected_firmware=profile["expected_firmware"])
            # Keep the existing top-level fields for standalone firmware consumers.
            r.update(bytes_acknowledged=phase["bytes_acknowledged"], ext_after_download=phase["ext_after_download"])
            if action == "cid":
                log("[*] 2/3: запись полного CID и проверка после перезапусков.")
                phase = _phase(service, "program_cid")
                service.ready()
                service.cmd(62, 0xefac62ec)
                time.sleep(.01)
                service.cmd(62, 0x00cced82)
                service.cmd(26, data=target)
                service.ready()
                actual = _verify_cycles(service, target, before, phase, expected_firmware=profile["expected_firmware"])
                log("[*] 3/3: возврат firmware с исходным gate и повторная проверка CID.")
                phase = _phase(service, "stock_gate_firmware")
                _check_firmware_access(service, actual)
                _download_firmware(service, stock, phase, 60, 100)
                actual = _verify_cycles(service, target, before, phase, expected_firmware=profile["expected_firmware"])
                r["stock_restore_verified"] = True
                log("[+] CID: " + before["cid"] + " → " + actual["cid"] + "; firmware с исходным gate возвращена.")
            elif reset_rstn:
                phase = _phase(service, "reset_rstn")
                service.ready()
                # Use the stock USB CMD6 handler: SCV3 rejects this argument.
                phase["command"] = "CMD6 WRITE_BYTE EXT_CSD[162]=0"
                service.save()
                service.session.write_extcsd_byte(162, 0)
                service.ready()
                phase["immediate_rstn"] = service.session.read_extcsd()[162]
                service.save()
                if phase["immediate_rstn"] != 0:
                    raise RuntimeError("RST_n: немедленное чтение не подтвердило 0")
                actual = _verify_cycles(service, bytes.fromhex(before["cid"]), before, phase,
                                        expected_firmware=profile["expected_firmware"], expected_rstn=0)
                phase = _phase(service, "stock_gate_firmware")
                _check_firmware_access(service, actual)
                _download_firmware(service, stock, phase, 60, 100)
                actual = _verify_cycles(service, bytes.fromhex(before["cid"]), before, phase,
                                        expected_firmware=profile["expected_firmware"], expected_rstn=0)
                r.update(stock_restore_verified=True, rstn_reset_verified=True)
                log("[+] RST_N_FUNCTION сброшен в 0; исходные проверки firmware возвращены.")
            else:
                log("[+] Firmware: " + before["firmware"] + " → " + actual["firmware"])
            log("[+] Каждый этап проверен двумя init после power-off; физическое снятие питания не измерялось.")
        r.update(ok=True, after=actual)
        service.save()
        return dict(actual, report=str(service.path), stock_restore_verified=r.get("stock_restore_verified"),
                    rstn_reset_verified=r.get("rstn_reset_verified"))
    except Exception as error:
        failure = error
        if (r.get("cid_sequence_started") or r.get("rstn_sequence_started")) and not r.get("stock_restore_verified"):
            failure = RuntimeError(str(error) + "; этап: " + r.get("phase", "начало записи") +
                                   ". Возврат firmware с исходным gate не подтверждён; перечитайте чип и изучите отчёт.")
            raise failure from error
        raise
    finally:
        service.finish(failure)
