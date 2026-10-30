"""Read-only hardware check of application presets; one power cycle per run."""
import argparse
import ctypes
import hashlib
import json
import time
from pathlib import Path
from modes.emmc import protocol as P
from modes.emmc.session import EmmcSession
from t48_usb import winusb


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clock", type=int, choices=P.CLOCK_SELECTORS, required=True)
    parser.add_argument("--preset", choices=("standard", "THGA"), default="standard")
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    preset = next(k for k in P.PRESET_SETUP if k != "THGA") if args.preset == "standard" else "THGA"
    result = dict(clock=args.clock, preset=args.preset, legacy=args.legacy, ok=False, transfers=[])
    session = EmmcSession(lambda msg: print(msg, flush=True), clock_mhz=args.clock,
                          legacy_30=args.legacy, preset=preset)
    opened = False
    started = time.monotonic()
    original_write, original_read = session.dev.write_pipe, session.dev.read_pipe
    def write(pipe, data):
        print("USB OUT " + data.hex(), flush=True)
        count = original_write(pipe, data)
        result["transfers"].append(dict(pipe=pipe, command=data.hex(), count=count))
        if count != len(data):
            error = ctypes.get_last_error()
            raise RuntimeError("Short USB command write: %d/%d; WinError %d: %s" % (count, len(data), error, ctypes.FormatError(error)))
        return count
    def read(pipe, length):
        data = original_read(pipe, length)
        result["transfers"].append(dict(pipe=pipe, requested=length, response=data.hex()))
        print("USB IN %02x %d/%d" % (pipe, len(data), length), flush=True)
        return data
    session.dev.write_pipe, session.dev.read_pipe = write, read
    try:
        if not session.open():
            raise RuntimeError("T48 unavailable or busy")
        opened = True
        timeout = ctypes.c_ulong(2000)
        for pipe in (0x01, 0x02):
            if not winusb.WinUsb_SetPipePolicy(session.dev.winusb_handle, pipe, 3, ctypes.sizeof(timeout), ctypes.byref(timeout)):
                raise ctypes.WinError(ctypes.get_last_error())
        session.dev.power_off()
        time.sleep(0.5)
        session.init_hw(True, "4-bit")
        cid, csd, ext = session.wake_registers("4-bit")
        sectors = int.from_bytes(ext[212:216], "little")
        if sectors == 0 or not any(ext):
            raise RuntimeError("Invalid EXT_CSD")
        result.update(cid=cid.hex(), csd=csd.hex(), sectors=sectors,
                      hs_timing=ext[185], bus_width=ext[183], device_type=ext[196])
        session.select_user()
        digest = hashlib.sha256()
        first_digest = None
        read_started = time.monotonic()
        for index in range(64):
            data = session.read_sectors(index * P.CHUNK_SECTORS, P.CHUNK_SECTORS)
            if len(data) != P.CHUNK_BYTES:
                raise RuntimeError("Short data chunk %d: %d" % (index, len(data)))
            if index == 0:
                first_digest = hashlib.sha256(data).hexdigest()
            digest.update(data)
        result.update(bytes_read=64 * P.CHUNK_BYTES, sha256=digest.hexdigest(),
                      first_32_sha256=first_digest, read_seconds=round(time.monotonic()-read_started, 3))
        output = Path(__file__).with_name("probe_preset_read_results.json")
        history = json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
        references = [r for r in history if r.get("ok") and r.get("clock") == 8 and r.get("cid") == cid.hex()]
        if references:
            result["matches_8mhz"] = result["sha256"] == references[-1]["sha256"]
            if not result["matches_8mhz"]:
                raise RuntimeError("Data differs from 8 MHz baseline")
        elif args.clock != 8:
            raise RuntimeError("No matching 8 MHz baseline")
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if opened:
            try:
                session.close()
                result["power_off_sent"] = any(t.get("command") == P.CMD_POWER_OFF and t.get("count") == 8 for t in result["transfers"][-2:])
            except Exception as exc:
                result["close_error"] = str(exc)
        result["seconds"] = round(time.monotonic()-started, 3)
        output = Path(__file__).with_name("probe_preset_read_results.json")
        history = json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
        history.append(result)
        output.write_text(json.dumps(history, indent=2), encoding="utf-8")
        print(json.dumps({k:v for k,v in result.items() if k != "transfers"}), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
