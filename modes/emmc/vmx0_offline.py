"""SHA-bound VMX0 offline export. No hardware transport."""
import hashlib
import json
import struct
from pathlib import Path

SOURCE_SHA = "76abb4078f23b7675c843942ad321925a668eecb420bbc59b7874932affcbfcc"
SOURCE_NAME = "086_KMV3W000LM-B310.bin"
NOTICE = """KMV3W000LM-B310 / VMX0 — экспериментальный офлайн-патч

Изменена входная проверка vendor-команд; checksum пересчитана по наблюдаемому правилу.
Это не подтверждённый постоянный сброс RPMB.
В эмуляции проверены загрузка overlay, диспетчер CID и обнуление рабочего
счётчика до backend. Постоянная запись, транспорт загрузки и правило версии
VMX0 не установлены. На чипе этот BIN не проверен.
Не загружайте его через native-сервис Firmware/CID.
Исходный контейнер и его 32-байтный хвост сохранены в stock.bin;
patched.bin имеет ту же длину и неизменённый хвост.
Для отчёта о тестировании сохраняйте версию EXE, manifest.json, регистры,
журнал и точный результат. Не передавайте ключи и пользовательские данные.
"""

def prepare_offline(source):
    original = Path(source).read_bytes()
    if hashlib.sha256(original).hexdigest() != SOURCE_SHA:
        raise ValueError("VMX0: SHA-256 исходника не соответствует проверенному плану")
    if len(original) != 0x23020 or original[0x1c:0x20] != b"VMX0":
        raise ValueError("VMX0: неверный контейнер")
    payload = original[:0x23000]
    key = struct.unpack_from("<I", payload, 0x24)[0]
    def checksum(data):
        return (sum(struct.unpack("<%dI" % ((len(data)-4)//4), data[:-4])) & 0xffffffff) ^ key
    if checksum(payload) != struct.unpack_from("<I", payload, len(payload)-4)[0]:
        raise ValueError("VMX0: исходная checksum не совпала")
    if original[0x20ddc:0x20dde] != bytes.fromhex("0ad0"):
        raise ValueError("VMX0: исходная ветка не совпала")
    patched = bytearray(original)
    patched[0x20ddc:0x20dde] = bytes.fromhex("00bf")
    struct.pack_into("<I", patched, 0x22ffc, checksum(patched[:0x23000]))
    return original, bytes(patched)

def export_offline(source, folder):
    original, patched = prepare_offline(source)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    manifest = dict(model="KMV3W000LM-B310", format="VMX0",
                    status="experimental_offline_only", hardware_ready=False,
                    persistence_verified=False, source_sha256=SOURCE_SHA,
                    patched_sha256=hashlib.sha256(patched).hexdigest(),
                    container_bytes=len(patched), payload_bytes=0x23000,
                    edits=[dict(offset="0x20ddc", old="0ad0", new="00bf"),
                           dict(offset="0x22ffc", old=original[0x22ffc:0x23000].hex(),
                                new=patched[0x22ffc:0x23000].hex())])
    (folder / "stock.bin").write_bytes(original)
    (folder / "patched.bin").write_bytes(patched)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    (folder / "README_RU.txt").write_text(NOTICE, encoding="utf-8")
    return folder
