"""Experimental RPMB reset for 44 audited originals; serialized service worker."""
import hashlib
import json
import secrets
import struct
from pathlib import Path
from .firmware import prepare_firmware, checksum

NATIVE_COUNTER_SOURCE = "79adf81e384993b55b0f4df03bac91b6a90a91ace40f3221cc59c442ee741396"
NATIVE_COUNTER_COMMAND = 0x0FAC0051


def supports_native_counter_reset(path):
    # Physical model follows the exact selected original, never the editable CID.
    return bool(native_counter_plan(path))


def native_counter_plan(path):
    plans_path = Path(__file__).resolve().parents[2] / "assets/firmware/rpmb_reset_plans.json"
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    return plans.get(hashlib.sha256(Path(path).read_bytes()).hexdigest(), {}).get("native_counter_reset")


def prepare_reset_images(path, snapshot):
    source = Path(path).read_bytes()
    plans_path = Path(__file__).resolve().parents[2] / "assets/firmware/rpmb_reset_plans.json"
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    plan = plans.get(hashlib.sha256(source).hexdigest())
    if plan is None:
        raise ValueError("RPMB: SHA-256 не соответствует одному из 44 исследованных оригиналов")
    try:
        ext = bytes.fromhex(snapshot["ext_csd"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("RPMB: нужен корректный EXT_CSD") from error
    if len(ext) != 512 or not ext[168]:
        raise ValueError("RPMB: некорректный EXT_CSD или отсутствует RPMB")
    stock = prepare_firmware(path, vendor_patch=False, snapshot=snapshot)
    native_counter = bool(plan.get("native_counter_reset"))
    image = bytearray(prepare_firmware(path, vendor_patch=True, snapshot=snapshot) if native_counter else stock)
    save = int(plan["save_call"], 16)
    save_instruction = stock[save:save+4]
    for patch in plan["patches"]:
        at = int(patch["offset"], 16)
        old, new = bytes.fromhex(patch["old"]), bytes.fromhex(patch["new"])
        if len(old) != len(new) or image[at:at+len(old)] != old:
            raise ValueError("RPMB: инструкции исходного BIN не совпали")
        image[at:at+len(new)] = new
    if image[save:save+4] != save_instruction:
        raise ValueError("RPMB: изменён исходный вызов сохранения")
    key = 16 if image[4:8] == b"VNC0" and len(image) == 0x3f000 else 24
    struct.pack_into("<I", image, len(image)-4, checksum(image, key))
    return bytes(image), stock


def require_transport(service):
    # USB-internal capability, not an eMMC R1 response.
    request = bytes.fromhex("273b4353") + bytes(28)
    dev = service.session.dev
    if dev.write_pipe(1, request) != len(request):
        raise RuntimeError("RPMB: неполный USB-запрос версии транспорта")
    reply = dev.read_pipe(0x81, 8)
    service.journal.write(json.dumps(dict(capability_query=request.hex(), response=reply.hex())) + "\n")
    if len(reply) != 8 or reply[:2] != b"\x27\x00" or int.from_bytes(reply[4:], "little") != 0x34504d52:
        raise RuntimeError("Сброс RPMB требует T48 SCV3+RPM4 (UpdateT48_V9_RPM4.dat). Перепрошивка T48 выполняется отдельно; eMMC не записана.")


def exchange(service, kind, label, *, auth_key=None):
    if auth_key is not None:
        if kind != 2 or not isinstance(auth_key, (bytes, bytearray)) or len(auth_key) != 32:
            raise ValueError("RPMB: authenticated counter требует прежний ключ длиной 32 байта")
    nonce = secrets.token_bytes(16) if kind == 2 else bytes(16)
    request = bytearray(512)
    request[484:500] = nonce
    request[510:512] = kind.to_bytes(2, "big")
    dev = service.session.dev
    for ep, data in ((1, bytes.fromhex("14000000000000000100000200000000")),
                     (2, bytes(request)),
                     (1, bytes.fromhex("15000002000000000100000200000000"))):
        if dev.write_pipe(ep, data) != len(data):
            raise RuntimeError("RPMB: неполная отправка USB")
    first, tail = dev.read_pipe(0x82, 512), dev.read_pipe(0x82, 16)
    if len(first) != 512 or len(tail) != 16:
        raise RuntimeError("RPMB: неполный ответ USB")
    frame = first[16:] + tail
    item = dict(label=label, response=int.from_bytes(frame[510:512], "big"),
                result=int.from_bytes(frame[508:510], "big"),
                counter=int.from_bytes(frame[500:504], "big"),
                nonce_matches=frame[484:500] == nonce,
                frame_sha256=hashlib.sha256(frame).hexdigest())
    service.result.setdefault("rpmb_probes", []).append(item)
    service.save()
    if item["response"] != (0x200 if kind == 2 else 0x100):
        raise RuntimeError("RPMB: неожиданный тип ответа")
    if kind == 2 and item["result"] == 0 and not item["nonce_matches"]:
        raise RuntimeError("RPMB: nonce ответа не совпал")
    if auth_key is not None:
        from .rpmb_auth import verify_counter_response
        item["hmac_verified"] = False
        service.save()
        authenticated_counter = verify_counter_response(frame, nonce, auth_key)
        if authenticated_counter != item["counter"]:
            raise RuntimeError("RPMB: authenticated counter не совпал")
        item["hmac_verified"] = True
        service.save()
    return item


def counter(service, label, *, auth_key=None):
    service.session.select_partition("RPMB")
    try:
        return exchange(service, 2, label, auth_key=auth_key)
    finally:
        service.session.select_partition("USER")


def run_reset(log, folder, expected, firmware_path, abort_check):
    from .service import ServiceSession, _download_firmware, _phase, _check_firmware_access
    if not expected:
        raise ValueError("Сначала прочитайте чип в сервисном окне")
    image, stock = prepare_reset_images(firmware_path, expected)
    native_counter = supports_native_counter_reset(firmware_path)
    service = ServiceSession(log, folder, abort_check)
    r = service.result
    r.update(operation="rpmb", patch_verification="emulated_RAM_before_save", persistence_prevalidated=False, physical_power_removal_measured=False, stock_restore_verified=False)
    r.update(native_counter_reset_supported=native_counter, native_counter_reset_completed=False, native_counter_plan=native_counter_plan(firmware_path))
    failure = None
    installed = False
    stream_active = False
    restoring = False
    def check(actual):
        if any(actual[k] != expected[k] for k in ("cid", "firmware", "csd_response")):
            raise RuntimeError("RPMB: CID/firmware/CSD изменились; перечитайте чип")
        if bytes.fromhex(actual["ext_csd"])[212:216] != bytes.fromhex(expected["ext_csd"])[212:216] or actual["ffu_status"]:
            raise RuntimeError("RPMB: ёмкость или FFU_STATUS не прошли проверку")
        return actual
    def verify(label):
        item = counter(service, label)
        if item["result"] != 7:
            raise RuntimeError("RPMB: отсутствие ключа не подтверждено (result=" + str(item["result"]) + ")")
    def restore(actual=None, verify_absent=True):
        nonlocal installed, restoring
        restoring = True
        # wake_registers requires a fresh init, not an already selected card.
        actual = check(service.cycle() if actual is None else actual)
        _check_firmware_access(service, actual)
        _download_firmware(service, stock, _phase(service, "restore_stock"), 60, 90)
        installed = False
        for n in range(3):
            actual = check(service.cycle())
            if verify_absent:
                verify("stock_after_restart_" + str(n+1))
            else:
                probe = counter(service, "stock_recovery_" + str(n+1))
                if probe["result"] not in (0, 7):
                    raise RuntimeError("RPMB: ошибка чтения после возврата firmware")
        r["stock_restore_verified"] = True
        return actual
    try:
        before = check(service.open())
        r["before"] = before
        fresh = prepare_reset_images(firmware_path, before)
        if fresh != (image, stock):
            raise RuntimeError("RPMB: выбранный BIN изменился")
        _check_firmware_access(service, before)
        require_transport(service)
        pre = counter(service, "baseline")
        if pre["result"] == 7 and not native_counter:
            r.update(ok=True, rpmb_already_absent=True, after=before)
            return dict(before, report=str(service.path), rpmb_already_absent=True)
        if pre["result"] not in (0, 7):
            raise RuntimeError("RPMB: чтение исходного счётчика завершилось ошибкой (result=" + str(pre["result"]) + ")")
        r.update(initial_rpmb_counter=pre["counter"], nonzero_counter_reset=pre["counter"] != 0)
        service.save()
        for suffix, payload in ((".rpmb.firmware.bin", image), (".stock.firmware.bin", stock)):
            with service.path.with_suffix(suffix).open("xb") as output:
                output.write(payload)
        r.update(candidate_sha256=hashlib.sha256(image).hexdigest(), stock_sha256=hashlib.sha256(stock).hexdigest())
        service.save()
        service.begin()
        log("[*] RPMB: загрузка патча, сброс ключа, возврат обычной firmware.")
        installed = None  # Outcome is unknown if the download fails partway.
        r["firmware_write_started"] = True
        service.save()
        _download_firmware(service, image, _phase(service, "patched_firmware"), 0, 40)
        installed = True
        check(service.cycle())
        if native_counter:
            log("[*] RPMB: штатный CMD62-сброс счётчика: " + r["native_counter_plan"]["label"])
            _phase(service, "native_counter_reset")
            r["native_counter_reset_started"] = True
            r["native_counter_command"] = hex(NATIVE_COUNTER_COMMAND)
            service.save()
            service.cmd(62, 0xEFAC62EC)
            service.cmd(62, NATIVE_COUNTER_COMMAND)
            service.ready()
            for label in ("native_counter_after_command", "native_counter_after_restart"):
                if label.endswith("restart"):
                    check(service.cycle())
                probe = counter(service, label)
                if probe["result"] != pre["result"] or probe["counter"] != 0:
                    raise RuntimeError("RPMB: native counter reset failed; key clearing stopped")
            r["native_counter_reset_completed"] = True
            # result=7 cannot authenticate or independently expose the stored counter.
            r["native_counter_zero_readback_verified"] = pre["result"] == 0
            service.save()
        service.session.select_partition("RPMB")
        try:
            request = bytearray(512)
            request[510:512] = b"\x00\x01"
            _phase(service, "reset_rpmb")
            service.cmd(23, 0x80000001)
            stream_active = True
            service.cmd(25, 0, count=1)
            for offset in range(0, 512, 16):
                if service.cmd(61, offset, data=bytes(request[offset:offset+16])) != offset+16:
                    raise RuntimeError("RPMB: неверный счётчик передачи")
            stream_active = False
            service.ready()
            if exchange(service, 5, "reset_result")["result"] != 0:
                raise RuntimeError("RPMB: команда сброса вернула ошибку")
        finally:
            if not stream_active:
                service.session.select_partition("USER")
        verify("patched_after_trigger")
        actual = check(service.cycle())
        verify("patched_after_restart")
        actual = restore(actual)
        r.update(ok=True, after=actual, rpmb_reset_verified=True)
        log("[+] RPMB: ключ не установлен (result=7) после возврата обычной firmware и трёх init. Физическое снятие питания не измерялось.")
        return dict(actual, report=str(service.path), rpmb_reset_verified=True, stock_restore_verified=True, native_counter_reset_completed=r["native_counter_reset_completed"], native_counter_zero_readback_verified=r.get("native_counter_zero_readback_verified", False))
    except Exception as error:
        if installed and not stream_active and not restoring:
            try:
                restore(verify_absent=False)
            except Exception as recovery_error:
                r["restore_error"] = repr(recovery_error)
        failure = RuntimeError(str(error) + ("; возврат обычной firmware не подтверждён, см. отчёт: " + str(service.path) if not r["stock_restore_verified"] and service.critical else ""))
        raise failure from error
    finally:
        r["experimental_firmware_may_remain"] = installed is not False
        service.finish(failure)
