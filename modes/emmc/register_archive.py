"""Save raw eMMC registers captured by an explicit chip initialization."""
from datetime import datetime
from pathlib import Path
import re
import sys

from .registers import dword_swap


def save_registers(cid_response, csd_response, extcsd, product, root=None):
    if len(cid_response) != 32 or len(csd_response) != 24 or len(extcsd) != 512:
        raise ValueError("Для сохранения нужны полные ответы CID/CSD и 512 байт EXT_CSD")
    if root is None:
        app_folder = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
        root = app_folder / "Emmc Data"
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]', "_", product).strip(" .") or "Unknown"
    stem = name + "_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    index = 0
    while True:
        folder = root / (stem if index == 0 else f"{stem}_{index}")
        try:
            folder.mkdir()
            break
        except FileExistsError:
            index += 1
    for filename, data in (("CID.bin", dword_swap(cid_response[8:24])),
                           ("CSD.bin", dword_swap(csd_response[8:24])),
                           ("EXT_CSD.bin", extcsd)):
        with (folder / filename).open("xb") as output:
            output.write(data)
    return folder
