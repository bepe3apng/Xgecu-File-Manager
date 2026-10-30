"""Оркестратор приложения: единый движок фоновых задач + маршрутизация режимов.

До рефакторинга здесь было 8 копий кода «поток -> try/except -> вызов
драйвера». Теперь каждая задача запускается через один метод `run_task`,
который централизованно управляет stop_event и обработкой ошибок.
"""
import threading
from pathlib import Path
from modes.emmc_mode import EmmcDriver
from t48_usb import usb_operation, cancel_operation


class _OperationFeedback:
    """Text messages and optional numeric progress use separate callbacks."""

    def __init__(self, log_cb, progress_cb, phase_cb=None):
        self.log = log_cb
        self.progress = progress_cb
        self.phase = phase_cb

    def __call__(self, message):
        self.log(message)


class AppOrchestrator:
    def __init__(self, log_cb, table_cb, task_cb=None, progress_cb=None, phase_cb=None):
        self.log = _OperationFeedback(log_cb, progress_cb, phase_cb)
        self.set_table = table_cb
        self.task_cb = task_cb
        self.active_mode = "eMMC"
        self.emmc_driver = EmmcDriver(log_cb=self.log)
        self.stop_event = threading.Event()
        self._operation_lock = threading.Lock()

    # --- Единый движок задач ---

    def run_task(self, fn, *args, clear_stop=True, clear_table_on_error=False):
        """Запускает задачу в фоне с общей обработкой ошибок и отмены."""
        if not self._operation_lock.acquire(blocking=False):
            self.log("[!] Уже выполняется операция. Дождитесь её завершения.")
            return False
        if clear_stop:
            self.stop_event.clear()
        if self.task_cb:
            self.task_cb(True)

        def _task():
            try:
                with usb_operation(self.stop_event):
                    fn(*args)
            except Exception as e:
                self.log(f"\n[!] Ошибка в процессе операции: {e}\n")
                if clear_table_on_error:
                    self.set_table([])
            finally:
                self._operation_lock.release()
                if self.task_cb:
                    self.task_cb(False)

        threading.Thread(target=_task, daemon=True).start()
        return True

    # --- Режимы ---

    def set_mode(self, mode_name):
        self.active_mode = mode_name
        self.log(f"\n[Режим] Выбран активный режим: {mode_name}")
        if mode_name != "eMMC":
            self.log(f"[*] Режим {mode_name} находится в разработке.")

    def stop_operation(self):
        self.log("\n[!] Поступила команда СТОП: завершение текущей операции...")
        self.stop_event.set()
        cancel_operation(self.stop_event)

    # --- Внутренние задачи (тела операций) ---

    def _task_read_info(self, is_18v, bus_width, result_cb=None):
        if self.active_mode != "eMMC":
            self.log(f"[!] Чтение для {self.active_mode} пока не реализовано.")
            self.set_table([])
            return

        info, parts = self.emmc_driver.read_info_and_partitions(
            is_18v, bus_width, abort_check=self.stop_event.is_set
        )
        if info:
            if "report" in info:
                self.log(info["report"])
            if parts:
                self.log(f"[+] В User Area обнаружена GPT/MBR разметка ({len(parts)} разделов).\n")
            else:
                self.log("[-] Разметка не обнаружена или пуста.\n")
            self.set_table(parts, info.get('boot_kb', 0) * 1024,
                           info.get('user_mb', 0), user_sectors=info.get('user_sectors'))
            if result_cb and not self.stop_event.is_set():
                result_cb(info)
        else:
            self.set_table([])

    def _task_erase(self, is_18v, bus_width, part_type, start_lba, end_lba):
        if self.active_mode != "eMMC":
            self.log(f"[!] Стирание для {self.active_mode} пока не реализовано.")
            return
        self.emmc_driver.erase_blocks(
            is_18v, bus_width, part_type, start_lba, end_lba,
            abort_check=self.stop_event.is_set
        )

    def _task_write(self, is_18v, bus_width, target_part, file_path, verify=False, erase_before=False):
        if self.active_mode != "eMMC":
            self.log(f"[!] Запись для {self.active_mode} пока не реализована.")
            return
        self.emmc_driver.write_partition(
            is_18v, bus_width, target_part, file_path,
            abort_check=self.stop_event.is_set, verify=verify, erase_before=erase_before
        )

    def _task_scatter_to_gpt(self, scatter_path, out_path, user_mb=0):
        try:
            self.log("\n[*] Запуск сборки GPT из MTK Scatter...")
            import scatter_to_gpt
            success = scatter_to_gpt.convert_scatter_to_gpt(
                scatter_path, out_path, user_mb, log_cb=self.log
            )
            if success:
                self.log("[+] Готово! Создан файл разметки GPT (17 408 байт).")
                self.log("    Вы можете сразу зашить его в чип через кнопку «Запись» на строке #0.\n")
        except Exception as e:
            self.log(f"[!] Ошибка создания GPT: {e}\n")

    # --- Публичные точки входа (API для GUI сохранен) ---

    def run_write_extcsd(self, is_18v, bus_width, index, value, expected_data,
                         expected_cid, result_cb, started_cb=None, allow_otp=False):
        # Freeze the snapshot approved by the user before starting the worker.
        expected_data, expected_cid = bytes(expected_data), bytes(expected_cid)
        def _body():
            if self.active_mode != "eMMC":
                self.log("[!] Ошибка: EXT_CSD доступен только в режиме eMMC.")
                return
            if started_cb:
                started_cb()
            result = self.emmc_driver.write_extcsd(
                is_18v, bus_width, index, value, expected_data, expected_cid,
                self.stop_event.is_set, allow_otp=allow_otp)
            if result is not None:
                result_cb(*result)
        return self.run_task(_body)

    def run_read_extcsd(self, is_18v, bus_width, result_cb, started_cb=None):
        def _body():
            if self.active_mode != "eMMC":
                self.log("[!] Ошибка: EXT_CSD доступен только в режиме eMMC.")
                return
            if started_cb:
                started_cb()
            result = self.emmc_driver.read_extcsd(
                is_18v, bus_width, self.stop_event.is_set)
            if result is not None:
                result_cb(*result)
        return self.run_task(_body)

    def run_read_info(self, is_18v, bus_width, result_cb=None):
        return self.run_task(self._task_read_info, is_18v, bus_width, result_cb, clear_table_on_error=True)

    def run_dump_boot(self, is_18v, bus_width, part_name, size_bytes, out_path, verify=False):
        def _body():
            self.emmc_driver.dump_boot(
                is_18v, bus_width, part_name, size_bytes, out_path,
                self.stop_event.is_set, verify=verify)
        self.run_task(_body)

    def run_dump_userdata(self, is_18v, bus_width, out_path, verify=False):
        def _body():
            self.emmc_driver.dump_user_area(
                is_18v, bus_width, out_path, self.stop_event.is_set, verify=verify)
        self.run_task(_body)

    def run_dump_part(self, is_18v, bus_width, part_name, start_lba, end_lba, out_path, verify=False):
        def _body():
            self.emmc_driver.dump_lba_range(
                is_18v, bus_width, part_name, start_lba, end_lba, out_path,
                self.stop_event.is_set, verify=verify)
        self.run_task(_body)

    def run_write_part(self, is_18v, bus_width, part_name, start_lba, end_lba, file_path, verify=False, erase_before=False):
        def _body():
            self.emmc_driver.write_lba_range(
                is_18v, bus_width, part_name, start_lba, end_lba, file_path,
                self.stop_event.is_set, verify=verify, erase_before=erase_before)
        self.run_task(_body)

    def run_erase_task(self, is_18v, bus_width, part_type, start_lba, end_lba):
        self.run_task(self._task_erase, is_18v, bus_width, part_type, start_lba, end_lba)

    def run_write_task(self, is_18v, bus_width, target_part, file_path, verify=False, erase_before=False):
        self.run_task(self._task_write, is_18v, bus_width, target_part, file_path, verify, erase_before)

    def run_dump_all(self, is_18v, bus_width, folder, verify=False):
        def _body():
            paths = {
                "BOOT1": Path(folder) / "Boot1.bin",
                "BOOT2": Path(folder) / "Boot2.bin",
                "USER": Path(folder) / "userdata.img",
            }
            if any(path.exists() for path in paths.values()):
                self.log("[-] Ошибка: в папке уже есть Boot1.bin, Boot2.bin или userdata.img. Выберите другую папку.")
                return
            for area, path in paths.items():
                if self.stop_event.is_set():
                    self.log("[!] Полное чтение отменено пользователем.")
                    return
                self.log(f"[*] Полное чтение: {area} → {path}")
                if area == "USER":
                    ok = self.emmc_driver.dump_user_area(
                        is_18v, bus_width, str(path), self.stop_event.is_set, verify=verify)
                else:
                    ok = self.emmc_driver.dump_boot(
                        is_18v, bus_width, area, None, str(path),
                        self.stop_event.is_set, verify=verify)
                if not ok:
                    return
            self.log("[+] Полное чтение BOOT1, BOOT2 и Userdata завершено!")
        return self.run_task(_body)

    def _check_extcsd_file(self, is_18v, bus_width, payload):
        if self.active_mode != "eMMC":
            raise ValueError("EXT_CSD доступен только в режиме eMMC")
        from modes.emmc.extcsd import write_plan
        expected, expected_cid, target = payload
        result = self.emmc_driver.read_extcsd(is_18v, bus_width, self.stop_event.is_set)
        if result is None:
            return None
        current, cid = result
        if cid != expected_cid:
            raise RuntimeError("CID изменился; запись чипа заблокирована")
        for index in (177, 178, 179, 162):
            mask = 0xF8 if index == 179 else 0xFF
            if (current[index] & mask) != (expected[index] & mask):
                raise RuntimeError(f"EXT_CSD [{index}] изменился; выполните анализ заново")
        return current, cid, write_plan(current, target)

    def _apply_extcsd_file(self, is_18v, bus_width, payload, result_cb=None):
        checked = self._check_extcsd_file(is_18v, bus_width, payload)
        if checked is None:
            return False
        current, cid, plan = checked
        self.log("[*] EXT_CSD: PARTITION_ACCESS сохраняется; поле 160 не записывается.")
        for index, old, value in plan:
            if self.stop_event.is_set():
                self.log("[!] Запись EXT_CSD отменена; часть параметров могла быть записана.")
                return False
            result = self.emmc_driver.write_extcsd(
                is_18v, bus_width, index, value, current, cid,
                self.stop_event.is_set, allow_otp=True)
            if result is None:
                return False
            current, cid = result
            if result_cb:
                result_cb(current, cid)
        if not plan and result_cb:
            result_cb(current, cid)
        if any(current[index] != value for index, _, value in plan):
            self.log("[!] Запись EXT_CSD завершена с ошибками верификации; см. журнал.")
        else:
            self.log("[+] Настройки EXT_CSD из файла применены и проверены.")
        return True

    def run_write_extcsd_file(self, is_18v, bus_width, payload, result_cb=None):
        payload = tuple(bytes(value) for value in payload)
        return self.run_task(
            self._apply_extcsd_file, is_18v, bus_width, payload, result_cb)

    def run_write_all(self, is_18v, bus_width, files, verify=False,
                      extcsd=None, result_cb=None, erase_before=False):
        files = dict(files)
        if extcsd is not None:
            extcsd = tuple(bytes(value) for value in extcsd)
        def _body():
            if self.active_mode != "eMMC":
                raise ValueError("Запись доступна только в режиме eMMC")
            if files.get("EXTCSD") and extcsd is None:
                raise ValueError("EXT_CSD не подтверждён; запись чипа заблокирована")
            if any(not Path(files.get(area, "")).is_file()
                   for area in ("BOOT1", "BOOT2", "USER")):
                self.log("[-] Ошибка: для полной записи нужны все три файла.")
                return
            if extcsd is not None and self._check_extcsd_file(is_18v, bus_width, extcsd) is None:
                return
            for area in ("BOOT1", "BOOT2", "USER"):
                if self.stop_event.is_set():
                    self.log("[!] Полная запись отменена пользователем.")
                    return
                self.log(f"[*] Полная запись: {area}")
                if not self.emmc_driver.write_partition(
                        is_18v, bus_width, area, files[area],
                        abort_check=self.stop_event.is_set, verify=verify, erase_before=erase_before):
                    return
            if self.stop_event.is_set():
                self.log("[!] Полная запись отменена пользователем.")
                return
            if extcsd is not None:
                self.log("[*] Полная запись: EXT_CSD")
                if not self._apply_extcsd_file(is_18v, bus_width, extcsd, result_cb):
                    return
            suffix = " и EXT_CSD" if extcsd is not None else ""
            self.log(f"[+] Полная запись BOOT1, BOOT2 и Userdata{suffix} завершена!")
        return self.run_task(_body)

    def run_scatter_to_gpt(self, scatter_path, out_path, user_mb=0):
        self.run_task(self._task_scatter_to_gpt, scatter_path, out_path, user_mb)
