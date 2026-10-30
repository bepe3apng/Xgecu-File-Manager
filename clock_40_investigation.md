# Selector 05 investigation

## Current status: archived after 40 MHz rollback

40 MHz support has been removed from the application. The descriptions below are historical, including references to `DEFAULT_LEGACY_40` and `legacy_40`; these are no longer application settings or session arguments. The CLI entry points of `probe_40_read.py`, `probe_thga_40_recovery.py`, `probe_thga_direct_stream.py`, and `probe_thga_auto_init.py` now stop before USB access. Their old routines are retained only as research records and are not compatible with the current session API. Use `probe_preset_read.py` for current 8/16/20/30 MHz checks.

## THGA experimental application mode

Added `40 МГц (эксп.)` to the application. `DEFAULT_LEGACY_40` and the session's `legacy_40` snapshot select the 30 MHz experimental setup `3e01100000080000`, the 30 MHz configuration flags, selector 05, and volatile HS_TIMING=0 after CID. Standard 40 MHz retains its captured setup and HS_TIMING=1. The default selection is unchanged.

Hardware test on the currently installed THGA (CID `070000008080ffc0300001113047343640660032c0c61b1e0000000000000000`) FAILED at CSD on experimental 40 MHz before any data read. Fresh 8 MHz reference read 1 MiB correctly, SHA-256 `d73cdf89dc29aca8096e818b268fa630114d45daad8406613a957fcd57472254`. Details: `probe_40_thga_results.json`.

A separate test using the captured Xgpro 40 MHz setup also failed CSD on this THGA in both High-Speed and Legacy. `probe_40_capture_results.json` now contains these latest THGA results, replacing the previous chip's raw results; the previous successful measurements remain documented below. Legacy switching at 40 MHz cannot be confirmed because register validation fails. Do not describe experimental 40 MHz as working on THGA. The session aborts the operation on this error. All tests used VCCQ 1.8 V and reads/volatile switches only, with power-off cleanup.


## Successful capture-based test

`python emmc_continue/probe_40_read.py --repeats 3` passed on the chip with CID `070000008080ffc0430001153444544a3e5304522c9749490000000000000000`.

The Xgpro capture `дампы/klmcg4_40mhz.txt` confirms selector 05 for its 40 MHz option. Physical clock frequency was not measured. The probe reproduces setup `3e01300027010700` and the captured 1.8 V configuration (capture30 bytes 23 and 62 cleared).

- Fresh 8 MHz reference: 1 MiB in 0.277471 s, 3.604 MiB/s.
- 40 MHz High-Speed, HS_TIMING=1: 3/3 power cycles passed CID/CSD/EXT_CSD validation and byte-for-byte comparison of 1 MiB; 16.658–16.816 MiB/s.
- 40 MHz Legacy, HS_TIMING=0: 3/3 power cycles passed the same checks; 16.442–16.783 MiB/s. Legacy at this frequency exceeds the 26 MHz specification; use High-Speed for further development.
- All seven reads have SHA-256 `56ea2d9c5568fceb27bc6009e856b297809f7a9437e90f0b40a23d9bf108a354`.
- Sector count 122142720 and DEVICE_TYPE 0x57 matched the reference. EXT_CSD byte 183 returned zero in every trial; the probe accepts 0 or 1 and checks equality against the reference. This does not establish actual bus width. The measured throughput is incompatible with a literal 1-bit SDR interpretation at 40 MHz, so do not infer bus width from this field alone.

Results: `probe_40_capture_results.json`. These short 1 MiB timings do not establish sustained full-dump speed or reliability across the address space. VCCQ remained configured at 1.8 V. No flash writes were performed; cleanup attempts power-off after each trial. The application and programmer firmware were not changed. Application integration and longer reads remain untested.

## Previous unsuccessful configuration

The earlier capture30 configuration with setup `3e01100000080000` failed CSD on selector 05 in both timing variants. Skipping CSD also failed EXT_CSD validation (header `0010000000000000`, zero capacity/device type). Results are preserved in `probe_40_read_results.json` and `probe_40_skip_csd_results.json`. Those trials used a different chip CID and must not be directly compared to claim which configuration difference fixed the failure.

An initial capture-based attempt stopped with `T48 unavailable or busy`. The next attempt exposed a probe assumption requiring EXT_CSD byte 183 to equal 1; this was corrected before the successful run above.

## Firmware findings

`FUN_ram_0001ee78` stores the initialization selector. CID processing (`FUN_ram_0001edb6` -> `FUN_ram_0001e8f2`) requests HS_TIMING=1 for selector >3 and applies the divider through `FUN_ram_00020386`. CID data is obtained before final configuration, so CID alone does not validate the requested clock.

`FUN_ram_00020386` changes only the low eight bits of register 0x40018004. The inspected CSD handler `FUN_ram_0001ef64` does not apply a clock selector. Changing the selector byte in a CSD request alone is not evidence of a clock change.

## Further THGA isolation and offline falling-edge candidate

The current chip still does NOT have validated 40 MHz operation. The fresh 8 MHz reference retains SHA-256 `d73cdf89dc29aca8096e818b268fa630114d45daad8406613a957fcd57472254`.

- `probe_thga_hs_boundary_results.json`: explicitly setting HS_TIMING=1 at selector 03 (20 MHz) fails EXT_CSD validation, as does selector 04 (30 MHz). Returning to HS_TIMING=0 on selector 04 reads 1 MiB byte-for-byte correctly at 2.683 MiB/s. Thus the observed failure is not confined to selector 05.
- `probe_thga_hs200_boundary_results.json`: HS200 at selectors 03/04 and HS_TIMING=0x11/0x21/0x31/0x41 at selector 04 all fail EXT_CSD validation. Failed readback does not prove the requested driver setting was applied.
- `probe_thga_auto_init_results.json`: the A3 SD/MMC initialization path times out at both selector 00 and 05 (response starts `80038000`). It is not a demonstrated way to change frequency for this wiring.
- The complete 64-entry USB dispatch surface is exported to `t48_usb_surface.txt`. Command 05 dispatches to CID processing; its selector byte is ignored. Command 21 stores the selector. Command 27 with the 16-bit field at offset 2 equal to 0x00ff is a no-op, not a clock-setting operation.
- Command 3e checks socket contacts; its payload is not established as a clock setting.

The WCH header [ch32v30x_sdio.h](https://github.com/openwch/ch32v307/blob/main/EVT/EXAM/SRC/Peripheral/inc/ch32v30x_sdio.h) defines `SDIO_ClockEdge_Falling = 0x2000`. The structure layout and register-writing implementation match the inspected firmware SDIO helper. The inspected eMMC initialization supplies zero for the edge field (rising). No usable stock USB operation to change this field has been identified. This supports an experiment, not a diagnosis or a claim that falling-edge operation will fix the chip.

`build_t48_falling_candidate.py` builds separate OFFLINE candidates in `firmware_candidates/`:
- `t48_falling_div1_UNTESTED.bin`
- `UpdateT48_falling_div1_UNTESTED.dat`
- `t48_falling_div1_UNTESTED.json`
- Independent Ghidra instruction listing: `t48_falling_disassembly.txt`

The candidate replaces the first instruction of divider setter 0x20386 with a jump to a 48-byte routine at 0x3ec60, inside an original 0xff region. It preserves CLKCR except divider and edge, selects falling edge for divider 1 (selectors 05/06/07), and rising edge for other dividers. It changes 50 image bytes. It therefore also affects SD and other cards if those paths call this setter with divider 1; it is not suitable as an unconditional production firmware change.

Static validation completed: exact original image SHA and hook bytes; original cave bytes; JAL target; 1536 executions of the generated RV32I routine against varied register states/dividers; update encode/decode round-trip; CRCs of all 942 blocks; independent Ghidra disassembly. No Ghidra references into the overwritten hook interior or chosen stub region were reported. A padding region and missing static references do not prove the region is unused by the bootloader or indirect runtime accesses.

Original `OR/UpdateT48.dat` was not modified. No candidate firmware was installed, no bootloader transition was requested, and hardware operation with this patch has not been tested. Bootloader acceptance, compatibility with the connected device firmware, and recovery have not been verified. Installing this candidate changes the T48 programmer itself and requires separate user authorization; prior permission to alter the eMMC is not treated as permission to risk the programmer. All hardware trials in this investigation kept VCCQ configured at 1.8 V.

## Stock firmware: further boundary checks

User declined T48 reflashing. Continue only with stock firmware; the offline candidate is not authorized for installation.

`probe_thga_hs_low_boundary_results.json` establishes a narrower failure boundary on the same THGA, using a fresh 8 MHz reference:

- Nominal selector 02 (16 MHz), HS_TIMING=1: both 4-bit and 1-bit pass EXT_CSD and CSD validation and byte-for-byte comparison of 1 MiB. Measured transfer rates were 2.193 and 1.330 MiB/s respectively.
- Nominal selector 03 (20 MHz), HS_TIMING=1, 1-bit: invalid EXT_CSD, just as previously observed with 4-bit. Reducing bus width does not remove the failure.
- Selector 03, HS_TIMING=1 followed by HS_TIMING=0 without power cycling: valid Legacy registers and identical 1 MiB at 2.354 MiB/s. This trial confirms recovery to Legacy; it does not independently validate the intermediate High-Speed state.
- The reference SHA-256 remains `d73cdf89dc29aca8096e818b268fa630114d45daad8406613a957fcd57472254`.

`probe_thga_direct_stream_results.json` tests data immediately after CID, without first requesting CMD9 or EXT_CSD. Both selector-05 variants fail: the first returned 16 KiB has header `91000000000000000000000000000000`, zero payload, and the first mismatch against reference is at byte 440. Receiving complete USB packets is not evidence that card data was read successfully.

`probe_thga_precondition_results.json` confirms POWER_CLASS=0 by EXT_CSD and matching data in Legacy at selector 03. Preconditioning power class and driver before High-Speed did not establish working High-Speed operation in the tested cases.

Static follow-up: the only named CLKCR writes in the decompilation are the known divider setter, bus-width setters, SDIO initialization/reset, and clock enable. Both available initialization routes combine divider selection with initialization or timing setup. This is a statement about the inspected decompilation, not a proof that no indirect access exists. The special USB 27/2d branch calls 0x180a4, which transmits bytes via GPIO helpers; it does not directly set the SDIO register.

A separate VCC adjustment field is loaded from configuration bytes 24..27 and reaches 0x1ea54; VCCQ is controlled separately. Exact voltage mapping has not been verified, so no voltage sweep was performed. There is still no validated 40 MHz solution for this THGA, and no firmware change was installed.

