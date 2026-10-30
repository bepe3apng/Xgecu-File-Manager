"""Experimental selector 04 reading with HS_TIMING=0 (outside 26 MHz legacy spec).
No flash writes. Never changes the main application or programmer firmware.
Compare a full 1 MiB against selector 00 over repeated power cycles.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path
from t48_usb import XGecuT48
from probe_clock_matrix import CONFIGS, EXPECTED_CID, EXPECTED_CSD
from modes.emmc import protocol as P


def trial(selector, baseline=None):
    d = XGecuT48()
    opened = False
    r = {"selector": selector, "requested_mhz": 8 if selector == 0 else 30,
         "experimental_legacy_overclock": selector == 4, "transfers": [], "ok": False}
    data = bytearray()
    def cmd(h, size=None):
        p = bytes.fromhex(h)
        if d.write_pipe(1, p) != len(p): raise RuntimeError("Short command write")
        b = d.read_pipe(0x81, size) if size is not None else b""
        r["transfers"].append({"command": h, "response": b.hex()})
        return b
    try:
        if not d.open(): raise RuntimeError("T48 unavailable or busy")
        opened = True
        d.power_off(); time.sleep(0.5)
        cmd("24f0000001000000"); cmd("24e0280000000000000000e5",40)
        cmd("24f1000000000000"); cmd("3e01100000080000",16)
        cmd(CONFIGS["capture30"]); cmd("393100000105a100",32)
        d.set_command_timeout(30000)
        try: b = cmd(f"21{selector:02x}000000000000",8)
        finally: d.set_command_timeout(1500)
        if len(b)!=8 or b[:2]!=bytes.fromhex("2100"): raise RuntimeError("Init failed")
        time.sleep(0.1)
        if cmd(f"05{selector:02x}000000000000",32).hex()!=EXPECTED_CID: raise RuntimeError("CID differs")
        # Firmware enables HS during CID for selector 04. Only change volatile HS_TIMING.
        cmd("274600000000b903",8); time.sleep(0.2)
        if cmd(f"06{selector:02x}000000000000",24).hex()!=EXPECTED_CSD: raise RuntimeError("CSD differs")
        cmd("0848000200000000")
        a,b=d.read_pipe(0x82,512),d.read_pipe(0x82,8)
        if len(a)!=512 or len(b)!=8: raise RuntimeError("Short EXT_CSD")
        e=(a+b)[8:]
        r.update(ext_header=(a+b)[:8].hex(), hs_timing=e[185], bus_width=e[183],
                 sectors=int.from_bytes(e[212:216],"little"),device_type=e[196])
        if (e[185],e[183],r["sectors"],e[196])!=(0,1,122142720,0x57): raise RuntimeError("EXT_CSD validation failed")
        start=time.perf_counter()
        for i in range(64):
            cmd(bytes(P.read_setup(i*P.CHUNK_SECTORS,1)).hex())
            a,b=d.read_pipe(0x82,P.CHUNK_BYTES),d.read_pipe(0x82,16)
            if len(a)!=P.CHUNK_BYTES or len(b)!=16: raise RuntimeError(f"Short chunk {i}")
            chunk=a[16:]+b
            if baseline is not None and chunk!=baseline[i*P.CHUNK_BYTES:(i+1)*P.CHUNK_BYTES]:
                raise RuntimeError(f"Data differs at chunk {i}")
            data.extend(chunk)
        r.update(bytes_read=len(data),sha256=hashlib.sha256(data).hexdigest(),
                 read_seconds=round(time.perf_counter()-start,3),ok=True)
    except Exception as exc: r["error"]=str(exc)
    finally:
        if opened:
            try: d.power_off()
            finally: d.close()
    return r,bytes(data)


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repeats",type=int,default=5,choices=range(1,11))
    args=p.parse_args()
    output=Path(__file__).with_name("probe_30_legacy_read_results.json")
    results=[]
    baseline=None
    for i in range(args.repeats+1):
        r,data=trial(0 if i==0 else 4,baseline)
        r["cycle"]=i
        results.append(r)
        output.write_text(json.dumps(results,indent=2),encoding="utf-8")
        print(json.dumps({k:v for k,v in r.items() if k!="transfers"}),flush=True)
        if not r["ok"]: raise SystemExit(1)
        if i==0: baseline=data
