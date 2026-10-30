# Git source package

This folder is a standalone copy of the current application source. Originals remain in ../emmc_continue. No commit or push was performed.

## Included

Application modules, KV, assets and font license, README, AGENTS.md, docs, the primary PyInstaller spec and runtime hook, smoke tests, GPT/scatter test inputs, clock research notes and the hardware evidence linked by the documentation.

smoke_extcsd.py uses tests/fixtures/extcsd_sample.bin, a synthetic 544-byte EXT_CSD/CID/CSD container, instead of a private dump outside the project. Other application source files are unchanged. The two known GUI assertion failures documented in docs/TESTING.md remain.

## Setup in a fresh checkout (Windows / PowerShell)

```powershell
.\setup_venv.ps1
# To include EXE build dependencies: .\setup_venv.ps1 -Build
.\.venv\Scripts\python.exe gui.py
```

For building, install requirements-build.txt and run:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm T48_eMMC.spec
```

Run smoke tests from the repository root, for example:

```powershell
.\.venv\Scripts\python.exe -X utf8 smoke_layers.py
```

The older paths in README/docs/AGENTS describe the original development workspace. Use the checkout location and its own environment here. A fresh dependency installation has not been verified; versions reflect the existing working environment.

## Excluded

.git history, venv, caches, build/dist/EXE, private dumps/images/keys, firmware binaries/candidates, decompilations, Ghidra projects and most archival probes/results. Original research notes can mention these historical materials; they remain in the original working folder and are not runtime dependencies. No project license has been invented; the included font retains its own license.
