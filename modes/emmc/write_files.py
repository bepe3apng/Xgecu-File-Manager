"""Подбор и проверка образов областей eMMC."""
from pathlib import Path
from modes.emmc.protocol import CHUNK_BYTES


AREA_NAMES = {"BOOT1": "boot1", "BOOT2": "boot2", "USER": "userdata", "EXTCSD": "extcsd"}


def check_write_file(path, capacity):
    row = {"path": str(path), "status": "", "ready": False}
    try:
        path = Path(path)
        if not path.is_file():
            row["status"] = "Ошибка: файл не найден"
            return row
        size = path.stat().st_size
        row["path"] = str(path.resolve())
    except OSError as exc:
        row["status"] = f"Ошибка чтения файла: {exc}"
        return row
    if size == 0:
        row["status"] = "Ошибка: файл пуст"
    elif capacity <= 0:
        row["status"] = "Для проверки размера выполните «Анализ eMMC»"
    elif size > capacity:
        row["status"] = f"Ошибка: файл больше области ({size} > {capacity} байт)"
    elif ((size + CHUNK_BYTES - 1) // CHUNK_BYTES) * CHUNK_BYTES > capacity:
        row["status"] = "Ошибка: последний блок записи выходит за границу области"
    elif size < capacity:
        row.update(ready=True, status=f"Предупреждение: файл меньше области ({size} < {capacity} байт)")
    else:
        row.update(ready=True, status=f"Размер совпадает: {capacity:,} байт".replace(",", " "))
    return row


def check_image_file(part, path, capacity=0):
    if part != "EXTCSD":
        return check_write_file(path, capacity)
    from modes.emmc.extcsd import load_file
    row = {"path": str(path), "status": "", "ready": False}
    try:
        data, tail = load_file(path)
        row.update(path=str(Path(path).resolve()), ready=True,
                   status=f"EXT_CSD: {len(data) + len(tail)} байт; запись полей 177, 179, 162, 178")
    except (OSError, ValueError) as exc:
        row["status"] = f"Ошибка EXT_CSD: {exc}"
    return row


def find_write_files(folder, capacities, selected=None):
    selected = selected or {}
    rows = {
        key: {"path": "", "status": "Выберите папку или файл", "ready": False}
        for key in AREA_NAMES
    }
    candidates = {key: [] for key in AREA_NAMES}
    if folder:
        try:
            for path in sorted(Path(folder).iterdir(), key=lambda p: p.name.casefold()):
                name, stem = path.name.casefold(), path.stem.casefold()
                for key, expected in AREA_NAMES.items():
                    names = (expected, "ext_csd", "ecsd_csd") if key == "EXTCSD" else (expected,)
                    if (name in names or stem in names) and path.is_file():
                        candidates[key].append(path)
        except OSError as exc:
            for row in rows.values():
                row["status"] = f"Не удалось прочитать папку: {exc}"
            for key, path in selected.items():
                if key in rows:
                    rows[key] = check_image_file(key, path, capacities.get(key, 0))
            return rows
    for key, files in candidates.items():
        capacity = capacities.get(key, 0)
        if key in selected:
            rows[key] = check_image_file(key, selected[key], capacity)
            continue
        if not files:
            if folder:
                rows[key]["status"] = f"Файл {AREA_NAMES[key]} не найден"
            continue
        checked = [check_image_file(key, path, capacity) for path in files]
        exact = [row for row in checked if row["ready"] and not row["status"].startswith("Предупреждение:")]
        eligible = exact or [row for row in checked if row["ready"]]
        if len(eligible) == 1:
            rows[key] = eligible[0]
        elif len(checked) == 1:
            rows[key] = checked[0]
        else:
            rows[key]["status"] = f"Найдено файлов: {len(files)}. Выберите файл вручную."
    return rows
