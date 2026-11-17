"""Experimental, SHA-bound KMV3W VMX0 counter/key-state reset via service worker."""
import hashlib
from pathlib import Path

from . import rpmb
from .registers import dword_swap
from .service import ServiceSession, _phase
from .vmx0_offline import SOURCE_NAME, SOURCE_SHA, prepare_offline

PAYLOAD_BYTES = 0x23000
EXPECTED_FIRMWARE = "0000100100030000"
EXPECTED_CSD = "d02701320f5903fff6dbffef8e40400c"


def prepare(snapshot):
    """Restrict research transport to the observed independent register profile.

    Matching registers is a restriction, not proof of installed-code compatibility.
    CID is editable and is deliberately not used to select the firmware family.
    """
    if not snapshot:
        raise ValueError("VMX0: сначала прочитайте чип")
    ext = bytes.fromhex(snapshot["ext_csd"])
    csd = dword_swap(bytes.fromhex(snapshot["csd_response"])[8:24]).hex()
    if (len(ext) != 512 or snapshot["firmware"] != EXPECTED_FIRMWARE or
            csd != EXPECTED_CSD or int.from_bytes(ext[212:216], "little") != 30777344 or
            ext[226] != 32 or ext[168] != 4 or ext[192] != 7):
        raise ValueError("VMX0: регистры не соответствуют исследованному профилю KMV3W; запись запрещена")
    if ext[183] or ext[169] & 1 or not ext[493] & 1:
        raise ValueError("VMX0: режим 1-bit/доступ к обновлению не подтверждён")
    source = Path(__file__).resolve().parents[2] / "assets" / "vmx0" / SOURCE_NAME
    stock, patched = prepare_offline(source)
    return patched[:PAYLOAD_BYTES], stock[:PAYLOAD_BYTES]


def download(service, image, phase):
    if len(image) != PAYLOAD_BYTES:
        raise ValueError("VMX0: передаётся только payload 0x23000 без хвоста контейнера")
    service.session.write_extcsd_byte(133, 1)
    service.ready()
    if service.session.read_extcsd()[133] != 1:
        raise RuntimeError("VMX0: EXT_CSD[133] не подтвердил вход")
    sectors = len(image) // 512
    service.cmd(23, sectors)
    phase.update(stream_active=True, firmware_outcome="unknown", bytes_acknowledged=0)
    service.save()
    # A failed start can already have reached the device: do not guess stream state.
    service.cmd(25, 0xC7810000, count=sectors)
    for offset in range(0, len(image), 16):
        if service.cmd(61, offset, data=image[offset:offset+16]) != offset + 16:
            raise RuntimeError("VMX0: неверное подтверждение передачи; состояние потока неизвестно")
        if (offset + 16) % 16384 == 0:
            phase["bytes_acknowledged"] = offset + 16
            service.save()
    phase.update(stream_active=False, bytes_acknowledged=len(image))
    service.save()
    service.ready()
    ext = service.session.read_extcsd()
    phase.update(install_status=ext[253], standard_ffu_status=ext[26])
    service.save()
    if ext[253] != 1:
        raise RuntimeError("VMX0: установка не подтверждена, EXT_CSD[253]=" + str(ext[253]))
    phase.update(status="installed", firmware_outcome="install_status_success")
    service.save()


def run_reset(log, folder, expected, abort_check=None):
    image, stock = prepare(expected)
    service = ServiceSession(log, folder, abort_check)
    r = service.result
    r.update(operation="vmx0_reset", source_sha256=SOURCE_SHA,
             compatibility="observed_register_profile_only", hardware_prevalidated=False,
             stock_restore_verified=False, counter_zero_readback_verified=False,
             counter_hmac_verified=False, key_absence_verified=False,
             key_material_erasure_verified=False, physical_power_removal_measured=False,
             counter_after_key_clear_verifiable=False)
    failure = None
    writing = False
    restoring = False

    def check(actual):
        if any(actual[k] != expected[k] for k in ("cid", "csd_response", "firmware")):
            raise RuntimeError("VMX0: CID/CSD/firmware изменились; дальнейшая запись остановлена")
        prepare(actual)
        return actual

    def probe(label, wanted):
        item = rpmb.counter(service, label)
        if item["result"] != wanted or (wanted == 0 and item["counter"] != 0):
            raise RuntimeError("VMX0: проверка RPMB не прошла: " + label)
        return item

    def restore():
        nonlocal restoring
        restoring = True
        check(service.cycle())
        phase = _phase(service, "restore_vmx0_stock")
        download(service, stock, phase)
        actual = None
        for n in range(3):
            actual = check(service.cycle())
            item = rpmb.counter(service, "stock_restart_" + str(n+1))
            if item["result"] not in (0, 7):
                raise RuntimeError("VMX0: ошибка RPMB после возврата оригинала")
            if r["key_absence_verified"] and item["result"] != 7:
                raise RuntimeError("VMX0: отсутствие ключа после возврата оригинала не подтверждено")
        r["stock_restore_verified"] = True
        service.save()
        return actual

    try:
        before = check(service.open())
        r["before"] = before
        rpmb.require_transport(service)
        baseline = rpmb.counter(service, "baseline")
        if baseline["result"] not in (0, 7):
            raise RuntimeError("VMX0: исходный RPMB ответ содержит ошибку")
        r.update(initial_rpmb_result=baseline["result"],
                 initial_counter=baseline["counter"] if baseline["result"] == 0 else None)
        for suffix, payload in ((".vmx0.patched.bin", image), (".vmx0.stock.bin", stock)):
            with service.path.with_suffix(suffix).open("xb") as output:
                output.write(payload)
        r.update(candidate_sha256=hashlib.sha256(image).hexdigest(),
                 stock_sha256=hashlib.sha256(stock).hexdigest())
        service.save()
        service.begin()
        writing = True
        log("[*] VMX0: экспериментальная загрузка патча, сброс счётчика и ключа, возврат оригинала.")
        download(service, image, _phase(service, "patched_vmx0"))
        check(service.cycle())
        _phase(service, "vmx0_counter_reset")
        service.cmd(62, 0xEFAC62EC)
        service.cmd(62, 0x0FAC0051)
        service.ready()
        for n in range(2):
            check(service.cycle())
            probe("counter_before_key_clear_" + str(n+1), baseline["result"])
        r["counter_zero_readback_verified"] = baseline["result"] == 0
        r["counter_reset_command_completed"] = True
        service.save()
        _phase(service, "vmx0_key_state_reset")
        service.cmd(62, 0xEFAC62EC)
        service.cmd(62, 0x0FAC0050)
        service.ready()
        probe("key_state_after_command", 7)
        check(service.cycle())
        probe("key_state_after_restart", 7)
        r["key_absence_verified"] = True
        service.save()
        actual = restore()
        # Logical absence cannot expose/authenticate the stored counter afterward.
        r.update(ok=True, after=actual, vmx0_sequence_completed=True)
        service.save()
        return dict(actual, report=str(service.path), vmx0_sequence_completed=True,
                    stock_restore_verified=True, key_absence_verified=True,
                    counter_zero_readback_verified=r["counter_zero_readback_verified"])
    except Exception as error:
        phases = r.get("phases", [])
        active = any(p.get("stream_active") for p in phases)
        if writing and not active and not restoring:
            try:
                restore()
            except Exception as recovery_error:
                r["restore_error"] = str(recovery_error)
        failure = RuntimeError(str(error) + "; отчёт: " + str(service.path) +
            ("; возврат оригинала НЕ подтверждён" if writing and not r["stock_restore_verified"] else ""))
        raise failure from error
    finally:
        r["experimental_firmware_may_remain"] = writing and not r["stock_restore_verified"]
        service.finish(failure)
