# Быстрый smoke-тест чистых слоёв без железа.
import io, sys
sys.stdout.reconfigure(encoding="utf-8")
from modes.emmc import partitions, registers, report as report_mod, protocol as P

# 1. Парсинг реального gpt.bin (34 сектора: MBR + header + entries)
with open("gpt.bin", "rb") as f:
    buf = f.read()
fake_dev = {0: buf}          # маппинг base -> данные
def sample(lba, n):
    base = lba - (lba % P.CHUNK_SECTORS)
    out = bytearray()
    # имитируем чтение блоками по 32 сектора от выровненного base
    start = base * 512
    end = (lba + n) * 512
    data = buf[start:start + (end - start + P.CHUNK_BYTES - 1)]
    return bytes(data[end - start - n*512 if False else 0:]) # упрощенно
def sample2(lba, n):
    a = lba * 512
    return buf[a:a + n * 512]

logs = []
parts = partitions.parse(buf, sample2, logs.append)
print("== GPT parse: строк =", len(parts))
for p in parts[:6]:
    print(f"   {p['name']:<22} {p['first']:<9} {p['last']:<9} {p['fs']}")

# 2. Декодирование регистров + отчёт (синтетический чип)
cid = bytearray(24)
cid[8] = 0x15  # Samsung MID на байте 0 после свопа
cid[11:17] = b"KLUDG4U"
ecsd = bytearray(512)
ecsd[192] = 8                     # MMC V5.1
ecsd[226] = 16                    # boot 2048 KB
ecsd[168] = 4                     # rpmb 512 KB
ecsd[196] = 0x14
struct_pack = __import__("struct").pack_into("<I", ecsd, 212, 61341696)
ecsd[267], ecsd[268], ecsd[269] = 1, 3, 2
ecsd[184] = 0x01

reg = registers.decode_registers(bytes(cid), ecsd)
rpmb = registers.rpmb_status(7)[0]
txt = report_mod.build(reg, rpmb)
print("\n== Отчёт (первые 20 строк) ==")
print("\n".join(txt.splitlines()[:20]))

# 3. Скаттер-парсер
import scatter_to_gpt as s2g
with open("MT6768_Android_scatter.txt", "r", encoding="utf-8", errors="ignore") as f:
    data = f.read()
pro = s2g.parse_mtk_scatter(data)
print("\n== Scatter parse: разделов =", len(pro), "| первые:", [p["name"] for p in pro[:5]])

# 4. Протокол-сборщики
assert len(P.read_setup(123, 4567)) == 40
assert len(P.write_setup(1, 2, "4-bit")) == 40
h = P.write_chunk_header(64, 2048)
assert h[0] == 0x02 and h[2] == 0x10
h2 = P.write_chunk_header(65, 2080)
assert h2[0] == 0x00 and h2[2] == 0x00
print("\n>>> SMOKE OK: parse_gpt =", len(parts), "| scatter parts =", len(pro))
