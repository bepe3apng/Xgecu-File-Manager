"""Диалог firmware/CID. USB выполняется только через AppOrchestrator."""
from pathlib import Path
import json
import hashlib
import tkinter as tk
from tkinter import filedialog, messagebox

from kivy.app import App
from kivy.clock import Clock
from kivy.properties import ObjectProperty, StringProperty, ListProperty
from kivy.uix.popup import Popup

from modes.emmc.service import parse_cid, prepare_firmware, firmware_header_version


class EmmcServicePopup(Popup):
    screen = ObjectProperty(None)
    snapshot = ObjectProperty(None, allownone=True)
    firmware_path = StringProperty("")
    cid_file_path = StringProperty("")
    firmware_options = ListProperty([])
    status = StringProperty("Выполните Инит eMMC и выберите firmware из базы. Смена CID: vendor-патч → запись и проверка CID → исходный gate и повторная проверка. Нужен T48 SCV3.")

    prepared_images = ObjectProperty(None, allownone=True)

    def _prepare_images(self, rstn=False, rpmb=False):
        self.prepared_images = None
        if not self.snapshot or not self.firmware_path:
            return
        if rpmb:
            from modes.emmc.rpmb import prepare_reset_images
            patched, stock = prepare_reset_images(self.firmware_path, self.snapshot)
        else:
            patched = prepare_firmware(self.firmware_path, snapshot=self.snapshot, vendor_patch=not rstn, rstn_patch=rstn)
            stock = prepare_firmware(self.firmware_path, vendor_patch=False, snapshot=self.snapshot)
        self.prepared_images = dict(patched=patched, stock=stock,
                                    header_version=int.from_bytes(patched[12:16], "little"))

    def on_dismiss(self):
        return bool(self.screen and self.screen.operation_running)

    def on_pre_open(self):
        try:
            folder = Path(__file__).resolve().parent / "assets" / "firmware"
            self._firmware_entries = {item["label"]: item for item in json.loads(
                (folder / "catalog.json").read_text(encoding="utf-8"))}
            self.firmware_options = sorted(self._firmware_entries)
        except Exception as error:
            self.status = str(error)

    def choose_firmware(self, label):
        if self.screen.operation_running:
            return
        self.prepared_images = None
        self.firmware_path = ""
        try:
            entry = self._firmware_entries[label]
            path = Path(__file__).resolve().parent / "assets" / "firmware" / entry["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Контрольная сумма записи базы не совпала")
            prepare_firmware(path, snapshot=self.snapshot)
            self.firmware_path = str(path)
            self._prepare_images()
        except Exception as error:
            self.status = str(error)

    def init_emmc(self):
        if self.screen.operation_running:
            return
        self.snapshot = None
        self.prepared_images = None
        self.status = "Инициализация eMMC…"
        self.screen.start_analyze(result_cb=self._init_result)

    def _init_result(self, info):
        from modes.emmc.service import _snapshot
        try:
            result = _snapshot(info["cid"], info["csd_response"], info["extcsd"])
            result["report"] = info.get("register_folder") or "Регистры прочитаны"
            self._result(result)
        except Exception as error:
            self._error(str(error))

    def export_vmx0(self):
        if self.screen.operation_running:
            return
        from datetime import datetime
        from modes.emmc.vmx0_offline import export_offline, SOURCE_NAME
        dialog = tk.Tk()
        dialog.withdraw()
        dialog.attributes("-topmost", True)
        try:
            destination = filedialog.askdirectory(parent=dialog, title="Папка экспорта офлайн-патча VMX0")
        finally:
            dialog.destroy()
        if not destination:
            return
        try:
            source = Path(__file__).resolve().parent / "assets" / "vmx0" / SOURCE_NAME
            folder = Path(destination) / ("KMV3W_VMX0_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            export_offline(source, folder)
            self.status = ("Офлайн-патч VMX0 экспортирован: " + str(folder) +
                           "\nЭксперимент: постоянный сброс и загрузка на чип не проверены. См. README_RU.txt.")
        except Exception as error:
            self.status = "Экспорт VMX0: " + str(error)

    def choose_cid_file(self):
        if self.screen.operation_running:
            return
        dialog = tk.Tk()
        dialog.withdraw()
        dialog.attributes("-topmost", True)
        try:
            path = filedialog.askopenfilename(
                parent=dialog, title="RAW CID: 16 байт без заголовка",
                filetypes=[("RAW CID", "*.bin"), ("Все файлы", "*.*")])
        finally:
            dialog.destroy()
        if path:
            self.cid_file_path = path

    def clear_cid_file(self):
        if not self.screen.operation_running:
            self.cid_file_path = ""

    def _cid_input(self):
        if self.cid_file_path:
            try:
                with Path(self.cid_file_path).open("rb") as source:
                    raw = source.read(17)
            except OSError as error:
                raise ValueError("Не удалось прочитать RAW CID: " + str(error)) from error
            if len(raw) != 16:
                raise ValueError("RAW CID должен содержать ровно 16 байт без заголовка.")
            return raw.hex(), "RAW-файл: " + self.cid_file_path
        return self.ids.service_cid.text, "Ручной ввод"

    def _invalidate(self):
        self.snapshot = None
        self.prepared_images = None
        self.screen.extcsd_chip_ready = False
        self.screen.extcsd_chip_cid = None
        self.screen.extcsd_ready = False
        self.screen.table_data = []
        self.screen.boot_size_bytes = 0
        self.screen.user_total_sectors = 0

    def _result(self, result):
        self.snapshot = result
        self.ids.service_cid.text = result["cid"]
        self.status = ("CID: " + result["cid"] + "\nFirmware: " + result["firmware"] +
                       "\nRST_N_FUNCTION: " + str(bytes.fromhex(result["ext_csd"])[162]) + "\nFFU_STATUS: " + str(result["ffu_status"]) + "\nОтчёт: " + result["report"])
        if result.get("header_version") is None:
            self.status += "\nFW_VERSION прочитана; числовое правило версии для загрузки firmware не установлено. Подготовка сервисной записи недоступна."
        try:
            self._prepare_images()
            if self.prepared_images:
                self.status += "\n0x0C: " + str(self.prepared_images["header_version"])
        except Exception as error:
            self.status += "\nPatcher: " + str(error)
        if result.get("vmx0_sequence_completed"):
            self.status += "\nVMX0: состояние ключа сброшено (result=7), оригинальный BIN возвращён; три init пройдены."
            self.status += ("\nСчётчик=0 прочитан после двух перезапусков до сброса ключа (без HMAC)."
                            if result.get("counter_zero_readback_verified") else
                            "\nКлюч исходно отсутствовал; ноль счётчика независимо не подтверждён.")
            self.status += "\nПосле сброса ключа счётчик не проверяем; затирание байтов ключа и физическое снятие питания не подтверждены."
        elif result.get("rpmb_reset_verified"):
            self.status += "\nRPMB: ключ не установлен (result=7); обычная firmware возвращена, три init пройдены. Физическое отключение питания проверяется отдельно."
            if result.get("native_counter_reset_completed"):
                self.status += "\nШтатная команда сброса счётчика выполнена."
                self.status += (" Счётчик=0 проверен до очистки ключа." if result.get("native_counter_zero_readback_verified") else " Ключ исходно отсутствовал; независимое чтение счётчика недоступно.")
        elif result.get("rpmb_already_absent"):
            self.status += "\nRPMB: ключ уже не установлен (result=7). Запись не выполнялась."
        elif result.get("rstn_reset_verified"):
            self.status += "\nRST_N_FUNCTION=0 проверен; исходные проверки firmware возвращены."
        elif result.get("stock_restore_verified"):
            self.status += "\nFirmware с исходным gate возвращена, новый CID проверен."

    def _error(self, text):
        self.snapshot = None
        self.prepared_images = None
        self.status = "Ошибка: " + text + "\nПеречитайте чип перед следующей операцией."

    def start(self, action):
        if self.screen.operation_running:
            return
        expected = dict(self.snapshot) if self.snapshot else None
        if action != "probe" and expected is None:
            self.status = "Сначала прочитайте подключённый чип."
            return
        cid_text = self.ids.service_cid.text
        try:
            if action == "cid":
                cid_text, cid_source = self._cid_input()
                target = parse_cid(cid_text, bytes.fromhex(expected["cid"]))
                self._prepare_images()
                prepare_firmware(self.firmware_path, snapshot=expected)
                prepare_firmware(self.firmware_path, vendor_patch=False, snapshot=expected)
                cid_text = target.hex()
                prompt = ("Изменить CID установленного чипа?\n\nБыл: " + expected["cid"] +
                          "\nНовый: " + cid_text + "\nИсточник CID: " + cid_source +
                          "\nИсходный BIN: " + self.firmware_path +
                          "\n\n1. Загрузка выбранной firmware с vendor-патчем."
                          "\n2. Запись полного CID и проверка."
                          "\n3. Возврат выбранной firmware с исходным gate и повторная проверка CID."
                          "\nCRC7 рассчитывается автоматически; каждый этап проверяется двумя init."
                          "\nВозвращается код выбранного образа с исходным gate; прежняя firmware МС не сохраняется."
                          "\nБудет изменена firmware eMMC; нужна резервная копия данных."
                          "\nSTOP после начала записи дождётся всей последовательности. Продолжить?")
            elif action == "vmx0_reset":
                from modes.emmc.vmx0_service import prepare
                prepare(expected)
                prompt = ("KMV3W / VMX0: сбросить счётчик и состояние ключа RPMB?"
                          "\nCID: " + expected["cid"] +
                          "\n1. Загрузка SHA-проверенного патча VMX0."
                          "\n2. CMD62 0x51: сброс счётчика и чтение после двух перезапусков."
                          "\n3. CMD62 0x50: сброс состояния ключа."
                          "\n4. Возврат оригинального BIN и проверка result=7 после трёх init."
                          "\nЭксперимент: загрузка и совместимость на реальном KMV3W не подтверждены."
                          "\nПрежняя firmware чипа не считывается. Нужна резервная копия данных."
                          "\nСброс ключа логический: затирание его байтов не подтверждается."
                          "\nПосле удаления ключа ноль счётчика независимо не проверяется."
                          "\nЧтение счётчика без HMAC; прежний ключ вводить не нужно."
                          "\nSTOP дождётся завершения последовательности. Продолжить?")
            elif action == "rpmb":
                self._prepare_images(rpmb=True)
                prompt = ("Экспериментальный сброс RPMB: 44 исследованных образа. Проверьте соответствие BIN микросхеме."
                          "\nCID: " + expected["cid"] + "\nИсходный BIN: " + self.firmware_path +
                          "\nТребуется T48 SCV3+RPM4. Для всех 44 оригиналов перед очисткой ключа выполняется штатный сброс счётчика. Маршруты проверены офлайн; постоянный сброс аппаратно подтверждён на KLMCG4JETD (2 → 0)."
                          "\nБудут загружены патч сброса и затем обычная firmware из выбранного BIN."
                          "\nРезультат проверяется после возврата firmware и трёх init. CID сохраняется."
                          "\nПрежняя firmware МС не считывается. Нужна резервная копия данных."
                          "\nФизическое отключение питания проверяется отдельно."
                          "\nSTOP после начала записи дождётся всей последовательности. Продолжить?")
            elif action == "rstn":
                value = bytes.fromhex(expected["ext_csd"])[162]
                if value not in (1, 2):
                    raise ValueError("RST_N_FUNCTION уже 0 или содержит неизвестное значение.")
                self._prepare_images(rstn=True)
                prompt = ("Сбросить RST_N_FUNCTION: " + str(value) + " → 0?"
                          "\nCID: " + expected["cid"] + "\nИсходный BIN: " + self.firmware_path +
                          "\n1. Загрузка firmware с патчем проверки RST_n."
                          "\n2. CMD6 [162]=0 и проверка после двух init."
                          "\n3. Возврат выбранного BIN с исходными проверками и ещё два init."
                          "\nCID сохраняется. Прежняя firmware чипа не считывается."
                          "\nСброс 1→0 аппаратно проверен только на исследованной МС; 2→0 не проверен."
                          "\nБудет изменена firmware eMMC; нужна резервная копия данных."
                          "\nSTOP дождётся всей последовательности. Продолжить?")
            elif action != "probe":
                raise ValueError("Неизвестная операция")
        except Exception as error:
            self.status = str(error)
            return
        if action != "probe":
            if action != "vmx0_reset":
                prompt += "\n0x0C: " + str(self.prepared_images["header_version"])
                prompt += "\nСовместимость выбранного образа с МС должна быть проверена отдельно."
            dialog = tk.Tk()
            dialog.withdraw()
            dialog.attributes("-topmost", True)
            try:
                confirmed = messagebox.askyesno("Firmware / CID", prompt, parent=dialog, default="no")
            finally:
                dialog.destroy()
            if not confirmed or self.screen.operation_running:
                return
        app = App.get_running_app()
        folder = str(Path(app.user_data_dir) / "service_logs")
        self.status = "Чтение чипа…" if action == "probe" else "Запись и проверка. Не отключайте T48."
        self.snapshot = None
        self.prepared_images = None
        if action != "probe":
            self._invalidate()
        self.screen.operation_running = True
        try:
            started = self.screen.orchestrator.run_emmc_service(
                action, folder,
                lambda result: Clock.schedule_once(lambda dt: self._result(result), 0),
                expected=expected, firmware_path=self.firmware_path, cid_text=cid_text,
                error_cb=lambda text: Clock.schedule_once(lambda dt: self._error(text), 0))
        except Exception as error:
            self.screen.operation_running = False
            self._error(str(error))
            return
        if not started:
            self.screen.operation_running = False
            self.status = "Другая операция уже выполняется."
