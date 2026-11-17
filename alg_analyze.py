"""Анализ структуры .alg (FPGA/Eeprom прошивок) XGecu."""
import os, math, struct, collections

DIR = r"F:\XGecu_prog\OR\algorithm"
files = sorted(f for f in os.listdir(DIR) if f.startswith("EMMC_"))

def entropy(b):
    if not b: return 0
    c = collections.Counter(b)
    n = len(b)
    return -sum(v / n * math.log2(v / n) for v in c.values())

for name in files:
    path = os.path.join(DIR, name)
    data = open(path, "rb").read()
    nonz = next((i for i, b in enumerate(data) if b), None)
    # найдем первую длинную зону ненулевых байтов
    print(f"\n=== {name}  size={len(data)}  first_nonzero={nonz} ===")
    # сканируем окнами по 256 байт: где границы нулевых зон
    zeros = [i for i in range(min(len(data), 4096)) if data[i] == 0]
    run_start = None
    segments = []
    prev = -10
    for i in range(0, 8192, 1):
        isz = data[i] == 0
        if isz:
            if prev != i - 1:
                segments.append(["zeros", i, i])
            else:
                segments[-1][2] = i
            prev = i
    longz = [s for s in segments if s[2] - s[1] > 32]
    if longz:
        print("  зоны нулей >32б (первые 8):", [(hex(a), hex(b)) for _, a, b in longz[:8]])
    ent_head = entropy(data[512:1024])
    ent_mid = entropy(data[len(data)//2:len(data)//2+4096])
    ent_tail = entropy(data[-4096:])
    print(f"  энтропия: head512+={ent_head:.2f} mid={ent_mid:.2f} tail={ent_tail:.2f}")
    # ASCII строки в первых 2048 и последних 2048
    for label, chunk in (("head", data[:2048]), ("tail", data[-2048:])):
        strs, cur = [], bytearray()
        for b in chunk:
            if 32 <= b < 127:
                cur.append(b)
            else:
                if len(cur) >= 4: strs.append(cur.decode())
                cur = bytearray()
        if len(cur) >= 4: strs.append(cur.decode())
        if strs:
            print(f"  строки[{label}]: {strs[:10]}")
