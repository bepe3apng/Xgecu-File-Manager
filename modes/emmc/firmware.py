"""Structural Samsung native-image patcher. No model allowlist or USB access."""
import hashlib
import struct
from pathlib import Path


def checksum(image, key_offset):
    return (sum(struct.unpack("<%dI" % ((len(image) - 4) // 4), image[:-4])) & 0xffffffff) ^ struct.unpack_from("<I", image, key_offset)[0]


def firmware_header_version(snapshot):
    """Interpret EXT_CSD FW_VERSION as a numeric anti-rollback version."""
    try:
        raw = bytes.fromhex(snapshot["firmware"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Нет корректной версии firmware из снимка МС") from error
    version = int.from_bytes(raw, "little")
    if len(raw) != 8 or not 0 < version <= 0xffffffff:
        raise ValueError("FW_VERSION не представима положительной 32-битной версией заголовка")
    return version


def _gate(image):
    """Recognize the audited bit10/error-mask/control-flow/CMD62 literal shape."""
    gates = []
    for offset in range(8, len(image) - 24, 2):
        branch = struct.unpack_from("<H", image, offset)[0]
        if branch & 0xff00 != 0xd500:  # BPL
            continue
        if image[offset-8:offset-6] not in (b"\x00\x98", b"\x01\x98"):
            continue  # LDR r0, [sp, #0/#4]
        if image[offset-6:offset-2] not in (bytes.fromhex("4ff40021"), bytes.fromhex("4ff40022")):
            continue  # MOV.W r1/r2, #0x80000
        if image[offset-2:offset] != bytes.fromhex("4005"):
            continue  # LSLS r0, r0, #21: state bit10
        displacement = branch & 255
        if displacement & 128:
            displacement -= 256
        target = offset + 4 + displacement * 2
        if not offset + 10 <= target < min(len(image)-2, offset+112):
            continue
        # MOV r0,rN; BL error handler; B past normal path.
        mov, bl1, bl2, skip = struct.unpack_from("<4H", image, offset+2)
        if mov & 0xff07 != 0x4600 or bl1 & 0xf800 != 0xf000 or bl2 & 0xd000 != 0xd000 or skip & 0xf800 != 0xe000:
            continue
        load = struct.unpack_from("<H", image, target)[0]
        if load & 0xf800 != 0x4800:  # 16-bit LDR literal
            continue
        literal = ((target + 4) & ~3) + (load & 255) * 4
        if literal + 4 > len(image) or image[literal:literal+4] != bytes.fromhex("ec62acef"):
            continue
        delta = target - offset - 4
        replacement = struct.pack("<H", 0xe000 | ((delta // 2) & 0x7ff))
        gates.append((offset, target, replacement))
    if len(gates) != 1:
        raise ValueError(f"Нужен один однозначный gate CMD62; распознано: {len(gates)}")
    return gates[0]


def inspect_firmware(path):
    original = Path(path).read_bytes()
    trailer_size = len(original) % 512
    if trailer_size not in (0, 16, 48):
        raise ValueError("Размер firmware: нужен native секторов по 512 байт, либо хвост 16/48 байт")
    if trailer_size == 48 and original[-32:-22] != bytes.fromhex("d02701320f5903fff6db"):
        raise ValueError("Не распознан упаковочный хвост 32 байта")
    native = original[:-trailer_size] if trailer_size else original
    if len(native) < 512 or native[:4] != bytes.fromhex("bebabeba"):
        raise ValueError("Не распознан заголовок native firmware")
    # VNC0 uses the independently established XOR key at 0x10.
    key = 16 if native[4:8] == b"VNC0" and len(native) == 0x3f000 else 24
    if checksum(native, key) != struct.unpack_from("<I", native, len(native)-4)[0]:
        raise ValueError("Контрольная сумма исходной firmware не совпала с распознанным форматом")
    version = struct.unpack_from("<I", native, 12)[0]
    if not version:
        raise ValueError("Нулевая версия исходного образа")
    offset, target, replacement = _gate(native)
    return native, dict(
        source_sha256=hashlib.sha256(original).hexdigest(),
        native_sha256=hashlib.sha256(native).hexdigest(),
        source_version=version, expected_firmware=version.to_bytes(8, "little").hex(),
        native_bytes=len(native), sectors=len(native)//512, trailer_bytes=trailer_size,
        checksum_key_offset=key, gate_offset=offset, gate_target=target,
        original_instruction=native[offset:offset+2].hex(),
        patched_instruction=replacement.hex(),
        validation="structural_match_hardware_compatibility_unverified")


def _rstn_guard(image):
    """Recognize the audited CMD6[162] store/range/change-flag/commit shape."""
    matches = []
    prefix = bytes.fromhex("95f8a20080ea040111f0ff0f")
    def branch(at, mask):
        word = struct.unpack_from("<H", image, at)[0]
        if word & 0xff00 != mask:
            raise ValueError("Не распознано ветвление RST_n")
        delta = word & 255
        return at + 4 + (delta - 256 if delta & 128 else delta) * 2
    for off in range(0, len(image) - 80, 2):
        if image[off:off+12] != prefix:
            continue
        try:
            guard = off + 14
            word = struct.unpack_from("<H", image, guard)[0]
            if word & 0xfd07 == 0xb900:  # CBNZ r0
                kind, start = "CBNZ", off + 16
                reject = guard + 4 + ((word >> 3) & 31) * 2 + ((word >> 9) & 1) * 64
            elif image[guard:guard+2] == bytes.fromhex("0028"):
                guard += 2
                kind, start = "CMP_BNE", off + 18
                reject = branch(guard, 0xd100)
            else:
                continue
            if branch(off+12, 0xd000) != start:
                continue
            if image[start:start+4] != bytes.fromhex("14f0fc0f"):
                continue
            if branch(start+4, 0xd100) != reject:
                continue
            if image[start+6:start+12] != bytes.fromhex("24f0fc010329"):
                continue
            range_reject = branch(start+12, 0xd200)
            if image[start+14:start+20] != bytes.fromhex("a107b1eb807f"):
                continue
            unchanged = branch(start+20, 0xd000)
            if image[start+22:start+30] != bytes.fromhex("012785f8a240e007"):
                continue
            if branch(start+30, 0xd000) != unchanged:
                continue
            load = image[start+32:start+36]
            if load in (bytes.fromhex("d9f86800"), bytes.fromhex("daf86800")):
                cursor = start + 36
                store = bytes((load[0]-16, 0xf8, 0x68, 0x10))
            elif image[start+32:start+34] == bytes.fromhex("986e"):
                # Audited VNC0 variant loads via r3 and stores via sb.
                cursor = start + 34
                store = bytes.fromhex("c9f86810")
            else:
                continue
            if image[cursor:cursor+4] != bytes.fromhex("40f48011") or image[cursor+4:cursor+8] != store:
                continue
            at = cursor + 8
            word = struct.unpack_from("<H", image, at)[0]
            if word & 0xf800 != 0xe000:
                continue
            delta = word & 0x7ff
            target = at + 4 + (delta-2048 if delta & 1024 else delta)*2
            if not all(0 <= x < len(image)-8 for x in (reject, range_reject, unchanged, target)):
                continue
            # MOVS r6,#1; CBZ r7; BL common persistence handler.
            tail = image[target:target+8]
            if tail[:2] != bytes.fromhex("0126") or int.from_bytes(tail[2:4], "little") & 0xfd07 != 0xb107:
                continue
            a, b = struct.unpack("<2H", tail[4:8])
            if a & 0xf800 != 0xf000 or b & 0xd000 != 0xd000:
                continue
            matches.append(dict(handler_offset=off, guard_offset=guard,
                                guard_kind=kind, common_continuation=target,
                                original_instruction=image[guard:guard+2].hex()))
        except (ValueError, struct.error):
            continue
    if len(matches) != 1:
        raise ValueError(f"Нужен один однозначный обработчик RST_n; распознано: {len(matches)}")
    return matches[0]


def prepare_firmware(path, *, vendor_patch=True, snapshot=None, rstn_patch=False):
    native, info = inspect_firmware(path)
    version = max(info["source_version"], firmware_header_version(snapshot)) if snapshot is not None else info["source_version"]
    # Preserve the historical research API for the one exact known source.
    if snapshot is None and info["source_sha256"] == "79adf81e384993b55b0f4df03bac91b6a90a91ace40f3221cc59c442ee741396":
        version = max(version, 4)
    image = bytearray(native)
    struct.pack_into("<I", image, 12, version)
    if vendor_patch:
        offset = info["gate_offset"]
        image[offset:offset+2] = bytes.fromhex(info["patched_instruction"])
    if rstn_patch:
        offset = _rstn_guard(native)["guard_offset"]
        image[offset:offset+2] = bytes.fromhex("00bf")
    struct.pack_into("<I", image, len(image)-4, checksum(image, info["checksum_key_offset"]))
    return bytes(image)
