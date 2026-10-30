import tkinter as tk
from tkinter import filedialog
from kivy.app import App
from kivy.lang import Builder
from kivy.uix.boxlayout import BoxLayout
from kivy.properties import ListProperty, DictProperty, BooleanProperty
from modes.emmc.write_files import AREA_NAMES, find_write_files
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from t48_usb import XGecuT48
from orchestrator import AppOrchestrator
from modes.emmc import protocol as P

Window.title = "XGecu T48 Universal Flasher PRO"

from kivy.properties import NumericProperty, StringProperty, ObjectProperty
from kivy.uix.widget import Widget


class M3Progress(Widget):
    value = NumericProperty(0)
    max = NumericProperty(100)


class ExtCsdRow(BoxLayout):
    screen = ObjectProperty(None, rebind=True)
    field = StringProperty("")
    offset = NumericProperty(0)


class TableRow(RecycleDataViewBehavior, BoxLayout):
    idx = NumericProperty(0)
    name_txt = StringProperty('')
    fs = StringProperty('')
    lba = StringProperty('')
    size_txt = StringProperty('')
    first_lba = NumericProperty(0)
    last_lba = NumericProperty(0)

    def refresh_view_attrs(self, rv, index, data):
        self.idx = int(data.get('idx', 0))
        self.name_txt = str(data.get('name_txt', ''))
        self.fs = str(data.get('fs', ''))
        self.lba = str(data.get('lba', ''))
        self.size_txt = str(data.get('size_txt', ''))
        self.first_lba = int(data.get('first_lba', 0))
        self.last_lba = int(data.get('last_lba', 0))
        return super().refresh_view_attrs(rv, index, data)

    def on_btn_read(self):
        App.get_running_app().root.action_read_part(self.name_txt, self.first_lba, self.last_lba)

    def on_btn_write(self):
        App.get_running_app().root.action_write_part(self.name_txt, self.first_lba, self.last_lba)

    def on_btn_erase(self):
        App.get_running_app().root.action_erase_part(self.name_txt, self.first_lba, self.last_lba)

class MainScreen(BoxLayout):
    log_text = StringProperty("Готов к работе. Выберите память и режим.\n")

    light_theme = BooleanProperty(False)

    def on_light_theme(self, instance, value):
        from ui_theme import theme
        theme.light = value

    log_filter = StringProperty("Все")
    log_query = StringProperty("")
    log_follow = BooleanProperty(True)
    from ui_log import console_font
    log_font = StringProperty(console_font())

    def _get_log_markup(self):
        from ui_theme import Theme
        from ui_log import format_log
        return format_log(self.log_text, Theme.palette(self.light_theme),
                          self.log_filter, self.log_query)

    from kivy.properties import AliasProperty
    log_markup = AliasProperty(_get_log_markup, None,
                              bind=("log_text", "light_theme", "log_filter", "log_query"),
                              cache=True)

    def copy_log(self):
        from kivy.core.clipboard import Clipboard
        Clipboard.copy(self.log_text)

    def follow_log(self):
        self.log_follow = True
        if "log_scroll" in self.ids:
            self.ids.log_scroll.scroll_y = 0

    def scroll_log(self, *args):
        if self.log_follow:
            Clock.schedule_once(lambda dt: self.follow_log(), 0)

    def on_log_markup(self, *args):
        self.scroll_log()
    table_data = ListProperty([])
    active_mode_tab = StringProperty("emmc")
    active_sub_tab = StringProperty("sub_read")
    progress_value = NumericProperty(0)
    progress_speed = NumericProperty(0)
    operation_running = BooleanProperty(False)
    boot_size_bytes = NumericProperty(0)
    write_folder = StringProperty("")
    write_files = DictProperty(find_write_files("", {}))
    verify_read = BooleanProperty(False)
    verify_write = BooleanProperty(True)
    erase_before_write = BooleanProperty(True)
    progress_status = StringProperty("Готов к работе")

    def __init__(self, **kwargs):
        self._preset_clocks = {
            "стандартный": "30 МГц",
            "THGA": "30 МГц (эксп.)",
        }
        self._manual_write_files = {}
        super().__init__(**kwargs)
        self.orchestrator = AppOrchestrator(
            log_cb=self.log, table_cb=self.update_table, task_cb=self.task_progress,
            progress_cb=self.update_progress, phase_cb=self.update_phase,
        )
        self.user_total_sectors = 0
        self.set_emmc_preset(self.emmc_preset)

    emmc_preset = StringProperty(P.DEFAULT_PRESET)
    emmc_clock = StringProperty(
        f"{P.DEFAULT_CLOCK_MHZ} МГц" + (
            " (эксп.)" if P.DEFAULT_PRESET == "THGA" and P.DEFAULT_CLOCK_MHZ == 30 and P.DEFAULT_LEGACY_30 else ""
        )
    )

    extcsd_file_tail = b""
    extcsd_data = None
    extcsd_ready = BooleanProperty(False)
    extcsd_drafts = DictProperty({})
    extcsd_values = DictProperty({})
    extcsd_reference = DictProperty({})
    extcsd_comparison = StringProperty("")
    extcsd_source = StringProperty("Снимок EXT_CSD не загружен")
    extcsd_text = StringProperty("Прочитайте EXT_CSD с чипа или откройте файл 512 / 544 байта.")

    def _extcsd_dialog(self, save=False):
        dialog = tk.Tk()
        dialog.withdraw()
        dialog.attributes('-topmost', True)
        try:
            options = dict(title="EXT_CSD", filetypes=[("Binary", "*.bin"), ("Все файлы", "*.*")])
            if save:
                return filedialog.asksaveasfilename(
                    initialfile="extcsd.bin", defaultextension=".bin", **options)
            return filedialog.askopenfilename(**options)
        finally:
            dialog.destroy()

    extcsd_chip_ready = BooleanProperty(False)
    extcsd_chip_cid = None
    extcsd_write_field = StringProperty("179 PARTITION_CONFIG")
    extcsd_write_value = StringProperty("00")
    extcsd_write_hint = StringProperty("")
    _extcsd_settings = None
    _extcsd_import = None

    def _apply_extcsd_import(self):
        if self._extcsd_import is None:
            return
        data, path = self._extcsd_import
        self.extcsd_reference = {i: f"{data[i]:02X}" for i in (177, 178, 179, 162, 160)}
        self.extcsd_drafts = dict(self.extcsd_reference)
        self.extcsd_comparison = "Файл: " + path
        if self.extcsd_chip_ready:
            count = sum(a != b for a, b in zip(self.extcsd_data, data))
            self.extcsd_comparison += f"\nРазличающихся байт: {count} из 512."

    def _show_extcsd(self, data, source, cid=None, file_tail=b""):
        from modes.emmc.extcsd import validate, describe
        data = validate(data)
        self.extcsd_data = data
        self.extcsd_values = {i: f"{data[i]:02X}" for i in (177, 178, 179, 162, 160)}
        self.extcsd_drafts = dict(self.extcsd_values)
        self.extcsd_reference = {}
        self.extcsd_comparison = ""
        self.extcsd_file_tail = bytes(file_tail)
        self.extcsd_source = source
        self.extcsd_text = describe(data)
        if file_tail:
            self.extcsd_text += "\n\nCID: " + file_tail[:16].hex(" ") + "\nCSD: " + file_tail[16:].hex(" ")
        self.extcsd_ready = True
        self.extcsd_chip_cid = cid
        self.extcsd_chip_ready = cid is not None
        self.select_extcsd_field(self.extcsd_write_field)

    def select_extcsd_field(self, label):
        self.extcsd_write_field = label
        index = int(label.split()[0])
        hints = {
            179: "Бит 6: BOOT_ACK; биты 5:3: BOOT_ENABLE (0/1/2/7). Биты 2:0 оставить прежними.",
            177: "Биты 1:0: ширина BOOT (0=1-bit, 1=4-bit, 2=8-bit); бит 2: RESET; биты 4:3: режим BOOT.",
            178: "01 — защита BOOT до отключения питания; 10 — постоянная НЕОБРАТИМАЯ защита.",
            160: "PARTITIONING_SUPPORT: бит 0 — разделы; бит 1 — Enhanced; бит 2 — Extended. Только чтение.",
            162: "НЕОБРАТИМО: 01 — навсегда включить RST_n; 02 — навсегда отключить.",
        }
        self.extcsd_write_hint = hints[index]
        if self.extcsd_ready:
            self.extcsd_write_value = f"{self.extcsd_data[index]:02X}"

    def extcsd_value_from_file(self):
        self.open_extcsd()

    def edit_extcsd_value(self, index, text):
        if not self.operation_running and index != 160 and self.extcsd_ready:
            if self.extcsd_drafts.get(index) != text:
                self.extcsd_drafts[index] = text

    def write_extcsd(self, index=None):
        if self.operation_running or not self.extcsd_chip_ready:
            return
        if index is not None:
            from modes.emmc.extcsd import WRITE_FIELDS
            if index not in WRITE_FIELDS:
                return
            self.select_extcsd_field(f"{index} {WRITE_FIELDS[index]}")
            self.extcsd_write_value = self.extcsd_drafts[index]
        try:
            from modes.emmc.extcsd import validate_write, WRITE_FIELDS
            index = int(self.extcsd_write_field.split()[0])
            value = int(self.extcsd_write_value.strip(), 16)
            data, cid = self.extcsd_data, self.extcsd_chip_cid
            settings = (self.ids.chk_voltage.active, self.ids.spin_bus.text,
                        self.emmc_preset, self.emmc_clock)
            if settings != self._extcsd_settings:
                raise ValueError("Настройки подключения изменились; прочитайте EXT_CSD заново")
            old = validate_write(data, index, value, allow_otp=True)
        except ValueError as error:
            self.log(f"[-] Ошибка параметров записи EXT_CSD: {error}")
            return
        if old == value:
            self.log("[*] EXT_CSD: значение уже установлено.")
            return
        from tkinter import messagebox
        dialog = tk.Tk()
        dialog.withdraw()
        dialog.attributes('-topmost', True)
        try:
            prompt = (f"Чип CID: {cid.hex()}\n"
                      f"EXT_CSD [{index}] {WRITE_FIELDS[index]}: {old:02X} → {value:02X}\n\n")
            if index == 178 and (value & 0x10) and not (old & 0x10):
                prompt += "Постоянная НЕОБРАТИМАЯ защита настроек загрузки. Снять её невозможно.\n\n"
            if index == 162:
                prompt += ("RST_n — однократная настройка: 01/02 могут фиксироваться НЕОБРАТИМО.\n"
                           "Если RST_n уже установлен, чип может отклонить изменение или возврат к 00.\n"
                           "После попытки значение будет проверено чтением.\n\n")
            prompt += "Записать выбранное поле и проверить чтением?"
            confirmed = messagebox.askyesno("Запись EXT_CSD", prompt, parent=dialog, default="no")
        finally:
            dialog.destroy()
        if not confirmed or self.operation_running:
            return
        source = self.extcsd_source
        def started():
            def apply(dt):
                self.extcsd_chip_ready = False
                self.extcsd_chip_cid = None
                self.extcsd_ready = False
                self.extcsd_data = None
                self.extcsd_source = "Запись EXT_CSD; при ошибке прочитайте регистр заново"
                self.extcsd_text = "Ожидание проверки записи."
            Clock.schedule_once(apply, 0)
        pending = dict(self.extcsd_drafts)
        reference = dict(self.extcsd_reference)
        comparison = self.extcsd_comparison
        def result(actual, actual_cid):
            def apply(dt):
                self._show_extcsd(actual, source, actual_cid)
                self.extcsd_drafts.update({i: v for i, v in pending.items() if i != index})
                self.extcsd_reference = reference
                self.extcsd_comparison = comparison.split("\n")[0] if comparison else ""
            Clock.schedule_once(apply, 0)
        self.orchestrator.run_write_extcsd(
            settings[0], settings[1], index, value, data, cid, result,
            started_cb=started, allow_otp=(index in (162, 178)))

    def read_extcsd(self):
        if self.operation_running:
            return
        preset, clock = self.emmc_preset, self.emmc_clock
        bus, voltage = self.ids.spin_bus.text, self.ids.chk_voltage.active

        def started():
            def apply(dt):
                self.extcsd_data = None
                self.extcsd_ready = False
                self.extcsd_chip_ready = False
                self.extcsd_chip_cid = None
                self.extcsd_source = "Чтение EXT_CSD..."
                self.extcsd_text = "Ожидание результата. При ошибке повторите чтение."
            Clock.schedule_once(apply, 0)

        def result(data, cid):
            source = f"Чип CID: {cid.hex()} | {preset}, {clock}, {bus}, VCCQ {1.8 if voltage else 3.3} В"
            def apply(dt):
                self._extcsd_settings = (voltage, bus, preset, clock)
                self._show_extcsd(data, source, cid)
                self._apply_extcsd_import()
            Clock.schedule_once(apply, 0)

        self.orchestrator.run_read_extcsd(voltage, bus, result, started)

    def open_extcsd(self, compare=False, path=None):
        if self.operation_running or (compare and not self.extcsd_ready):
            return
        path = path or self._extcsd_dialog()
        if not path:
            return
        try:
            from pathlib import Path
            from modes.emmc.extcsd import load_file, describe, compare as compare_data
            data, tail = load_file(path)
            if compare:
                self.extcsd_text = (describe(self.extcsd_data) + "\n\nСравнение с: " +
                                    path + "\n" + compare_data(self.extcsd_data, data))
                self.extcsd_reference = {i: f"{data[i]:02X}" for i in (177, 178, 179, 162, 160)}
                count = sum(a != b for a, b in zip(self.extcsd_data, data))
                self.extcsd_comparison = f"Сравнение: {path}\nРазличающихся байт: {count} из 512."
            else:
                if not self.extcsd_chip_ready:
                    self._show_extcsd(data, "Файл загружен. Прочитайте EXT_CSD с чипа для записи.", file_tail=tail)
                    self.extcsd_values = {}
                self._extcsd_import = (data, path)
                self._apply_extcsd_import()
                self._manual_write_files["EXTCSD"] = path
                self.refresh_write_files()
        except (OSError, ValueError) as error:
            self.log(f"[-] Ошибка открытия EXT_CSD: {error}")

    def save_extcsd(self):
        if self.operation_running or not self.extcsd_ready:
            return
        path = self._extcsd_dialog(save=True)
        if path:
            try:
                from pathlib import Path
                raw = self.extcsd_data + self.extcsd_file_tail
                Path(path).write_bytes(raw)
                self.log(f"[+] EXT_CSD сохранён ({len(raw)} байт): {path}")
            except OSError as error:
                self.log(f"[-] Ошибка сохранения EXT_CSD: {error}")

    def set_emmc_preset(self, label):
        if label not in (*P.PRESET_SETUP, "Авто"):
            raise ValueError(f"Неизвестный пресет eMMC: {label}")
        previous = self.emmc_preset
        self._preset_clocks[previous] = self.emmc_clock
        values = ["8 МГц", "16 МГц", "20 МГц", "30 МГц"] + (
            ["30 МГц (эксп.)"] if label == "THGA" else []
        )
        clock = self._preset_clocks.get(label, self.emmc_clock)
        if clock not in values:
            clock = "30 МГц"
        P.DEFAULT_PRESET = label
        self.emmc_preset = label
        self.set_emmc_clock(clock)
        if previous != label:
            self.log("[*] Авто: пресет и частота будут подобраны перед операцией." if label == "Авто"
                     else f"[*] Выбран пресет eMMC: {label}, {clock}. Применится со следующей операции.")

    def set_emmc_clock(self, label):
        # Existing sessions retain their selected frequency until power-off.
        if self.emmc_preset == "Авто":
            self.emmc_clock = "Авто"
            return
        clock_mhz = int(label.split()[0])
        if clock_mhz not in P.CLOCK_SELECTORS:
            raise ValueError(f"Неподдерживаемая частота eMMC: {clock_mhz} МГц")
        if "(эксп.)" in label and (self.emmc_preset != "THGA" or clock_mhz != 30):
            raise ValueError("Экспериментальные 30 МГц доступны только для THGA")
        P.DEFAULT_CLOCK_MHZ = clock_mhz
        P.DEFAULT_LEGACY_30 = "(эксп.)" in label
        self.emmc_clock = label

    def log(self, msg):
        Clock.schedule_once(lambda dt: self._append_log(msg), 0)

    def task_progress(self, running):
        def _apply(dt):
            self.operation_running = running
            if running:
                self.progress_value = 0
                self.progress_speed = 0
                self.progress_status = "Выполняется"
            elif self.progress_status.startswith("Выполняется") or self.progress_status == "Завершение":
                self.progress_value = 100
                self.progress_status = "Завершено"
            elif self.progress_status == "Остановка...":
                self.progress_status = "Остановлено"
        Clock.schedule_once(_apply, 0)

    def update_phase(self, action):
        labels = {
            "чтения": "Выполняется чтение",
            "записи": "Выполняется запись",
            "стирания": "Выполняется стирание",
            "верификации": "Выполняется верификация",
            "контроля стирания": "Выполняется контроль стирания",
            "анализа образа": "Выполняется анализ образа",
        }
        def _apply(dt):
            if action in labels and self.progress_status.startswith("Выполняется"):
                self.progress_status = labels[action]
                self.progress_value = 0
                self.progress_speed = 0
        Clock.schedule_once(_apply, 0)

    def update_progress(self, percent, speed):
        def _apply(dt):
            self.progress_value = max(0.0, min(100.0, percent))
            self.progress_speed = max(0.0, speed)
        Clock.schedule_once(_apply, 0)

    def _append_log(self, msg):
        message = msg.strip()
        if message.startswith(("... Прогресс:", "... Стерто:", "... Записано:", "... Записан сегмент [")):
            return
        from datetime import datetime
        from ui_log import TIME
        now = datetime.now()
        timestamp = now.strftime("[%H:%M:%S:") + f"{now.microsecond // 1000:03d}]"
        lines = str(msg).splitlines() or [""]
        self.log_text += "\n".join(
            line if TIME.match(line) else timestamp + " " + line
            for line in lines
        ) + "\n"
        lower = message.lower()
        if message.startswith(("[!]", "[-]")):
            if "команда стоп" in lower:
                self.progress_status = "Остановка..."
            elif "прерван" in lower or "отменен" in lower or "отменён" in lower:
                self.progress_status = "Остановлено"
            elif any(word in lower for word in ("ошиб", "тайм-аут", "не найден", "не отвечает", "не удалось")):
                self.progress_status = "Ошибка — см. журнал"
            elif message.startswith("[!]") or "файл пуст" in lower or "нет полезных данных" in lower:
                self.progress_status = "Проверьте журнал"

    def update_table(self, parts, boot_bytes=0, user_mb=0, user_sectors=None):
        def _apply(dt):
            self.boot_size_bytes = boot_bytes
            self.user_total_sectors = int(user_sectors if user_sectors is not None else (user_mb * 1024 * 1024) // 512)
            self.refresh_write_files()

            if hasattr(self.ids, 'txt_erase_end') and self.user_total_sectors > 0:
                if hasattr(self.ids, 'spin_erase_target') and self.ids.spin_erase_target.text == "User Area (Весь)":
                    self.ids.txt_erase_end.text = str(self.user_total_sectors - 1)

            rows = []
            # Если первый элемент — таблица, начинаем нумерацию с 0
            start_idx = 0 if (parts and parts[0].get("is_table")) else 1
            for idx, p in enumerate(parts, start_idx):
                size_mb = p.get('size', 0.0)
                # Красивый вывод малых размеров (KB / B) вместо 0.0 MB
                if size_mb < 0.1:
                    size_kb = size_mb * 1024
                    size_txt = f"{size_kb:.1f} KB" if size_kb >= 1.0 else f"{int(size_mb * 1024 * 1024)} B"
                else:
                    size_txt = f"{size_mb:.1f} MB"

                rows.append({
                    'idx': idx,
                    'name_txt': p.get("name", "(unnamed)"),
                    'fs': p.get("fs", "?"),
                    'lba': f"{p.get('first', 0)}..{p.get('last', 0)}",
                    'size_txt': size_txt,
                    'first_lba': p.get('first', 0),
                    'last_lba': p.get('last', 0)
                })
            self.table_data = rows
            if 'rv' in self.ids:
                self.ids.rv.refresh_from_data()

        Clock.schedule_once(_apply, 0)

    # Дамп раздела или таблицы разметки
    def action_read_part(self, name, first_lba, last_lba):
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        # Автоматическое имя файла по умолчанию
        if "GPT" in name:
            default_file = "gpt_table.bin"
        elif "MBR" in name:
            default_file = "mbr_table.bin"
        else:
            clean_name = "".join(c for c in name if c.isalnum() or c in ('_', '-')) or "partition"
            default_file = f"{clean_name}.img"

        path = filedialog.asksaveasfilename(
            defaultextension=".bin" if "Таблица" in name else ".img",
            initialfile=default_file,
            title=f"Сохранить {name}",
            filetypes=[("Binary / Image", "*.bin;*.img;*.raw"), ("All files", "*.*")]
        )
        root.destroy()

        if path:
            is_18v = self.ids.chk_voltage.active
            bus = self.ids.spin_bus.text
            self.orchestrator.run_dump_part(is_18v, bus, name, first_lba, last_lba, path, verify=self.verify_read)

    # Запись файла в выбранный раздел
    def action_write_part(self, name, first_lba, last_lba):
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askopenfilename(
            title=f"Выберите файл для записи в раздел {name}",
            filetypes=[("Image files", "*.img;*.bin;*.raw"), ("All files", "*.*")]
        )
        root.destroy()

        if path:
            is_18v = self.ids.chk_voltage.active
            bus = self.ids.spin_bus.text
            self.orchestrator.run_write_part(is_18v, bus, name, first_lba, last_lba, path, verify=self.verify_write, erase_before=self.erase_before_write)

    # Аппаратное стирание выбранного раздела
    def action_erase_part(self, name, first_lba, last_lba):
        is_18v = self.ids.chk_voltage.active
        bus = self.ids.spin_bus.text
        self.log(f"[*] Стирание раздела {name} [LBA: {first_lba} .. {last_lba}]...")
        self.orchestrator.run_erase_task(is_18v, bus, "USER", first_lba, last_lba)

    # Верхние вкладки режимов
    def select_mode(self, mode_name):
        sm = self.ids.mode_manager
        target_screen = mode_name.lower()
        tab_order = ['emmc', 'nand', 'spi']
        
        if sm.current != target_screen:
            current_idx = tab_order.index(sm.current)
            target_idx = tab_order.index(target_screen)
            
            sm.transition.direction = 'left' if target_idx > current_idx else 'right'
            self.orchestrator.set_mode(mode_name)
            sm.current = target_screen
            self.active_mode_tab = target_screen  # Обновляем подсветку верхних вкладок

    def switch_emmc_tab(self, target_screen):
        sm = self.ids.emmc_sub_tabs
        tab_order = ['sub_read', 'sub_write', 'sub_erase']
        
        if sm.current != target_screen:
            current_idx = tab_order.index(sm.current)
            target_idx = tab_order.index(target_screen)
            
            sm.transition.direction = 'left' if target_idx > current_idx else 'right'
            sm.current = target_screen
            self.active_sub_tab = target_screen  # Обновляем подсветку подвкладок
        if target_screen == "sub_write":
            self.refresh_write_files()

    # Анализ чипа и разметки
    def start_analyze(self):
        if self.operation_running:
            return
        from modes.emmc.autoconfig import forget
        forget()
        self.log_text = ""
        self.boot_size_bytes = 0
        self.user_total_sectors = 0
        self.refresh_write_files()
        self.table_data = []  # Очищаем таблицу перед новым анализом
        is_18v = self.ids.chk_voltage.active
        bus = self.ids.spin_bus.text
        preset, clock = self.emmc_preset, self.emmc_clock
        self.extcsd_chip_ready = False
        self.extcsd_chip_cid = None
        self.extcsd_ready = False
        def result(info):
            def apply(dt):
                self._extcsd_settings = (is_18v, bus, preset, clock)
                self._show_extcsd(info["extcsd"], "Анализ: CID " + info["cid"].hex(), info["cid"])
                self._apply_extcsd_import()
            Clock.schedule_once(apply, 0)
        self.orchestrator.run_read_info(is_18v, bus, result_cb=result)

    def dump_all(self):
        if self.operation_running:
            return
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            folder = filedialog.askdirectory(title="Папка для Boot1.bin, Boot2.bin и userdata.img")
        finally:
            root.destroy()
        if folder:
            self.operation_running = True
            try:
                started = self.orchestrator.run_dump_all(
                    self.ids.chk_voltage.active, self.ids.spin_bus.text,
                    folder, verify=self.verify_read)
                if started is False:
                    self.operation_running = False
            except Exception:
                self.operation_running = False
                raise

    # Дамп всей пользовательской области
    def dump_userdata(self):
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.asksaveasfilename(
            defaultextension=".img",
            initialfile="userdata_dump.img",
            title="Сохранить образ User Area",
            filetypes=[("Image files", "*.img;*.bin;*.raw"), ("All files", "*.*")]
        )
        root.destroy()

        if path:
            is_18v = self.ids.chk_voltage.active
            bus = self.ids.spin_bus.text
            self.orchestrator.run_dump_userdata(is_18v, bus, path, verify=self.verify_read)

    # Дамп BOOT
    def dump_boot(self, name):
        root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
        path = filedialog.asksaveasfilename(defaultextension=".bin", initialfile=f"{name}.bin")
        root.destroy()
        if path:
            is_18v = self.ids.chk_voltage.active
            bus = self.ids.spin_bus.text
            self.orchestrator.run_dump_boot(is_18v, bus, name, None, path, verify=self.verify_read)

    # Запись полных образов из рабочей папки
    def select_write_folder(self):
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askdirectory(
                title="Рабочая папка с Boot1, Boot2 и userdata",
                initialdir=self.write_folder or None,
            )
        finally:
            root.destroy()
        if path:
            self.write_folder = path
            self._manual_write_files.clear()
            self.refresh_write_files()

    def select_write_file(self, part):
        if self.operation_running or part not in AREA_NAMES:
            return
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askopenfilename(
                title=f"Выберите образ {AREA_NAMES[part]}",
                initialdir=self.write_folder or None,
                filetypes=[("Образы", "*.bin;*.img;*.raw"), ("Все файлы", "*.*")],
            )
        finally:
            root.destroy()
        if path:
            self._manual_write_files[part] = path
            self.refresh_write_files()
            if part == "EXTCSD":
                self.open_extcsd(path=path)

    def refresh_write_files(self):
        self.write_files = find_write_files(self.write_folder, {
            "BOOT1": self.boot_size_bytes,
            "BOOT2": self.boot_size_bytes,
            "USER": self.user_total_sectors * P.SECTOR_SIZE,
        }, selected=self._manual_write_files)

    def _prepare_write_extcsd_file(self):
        from modes.emmc.extcsd import load_file, write_plan, WRITE_FIELDS
        row = self.write_files["EXTCSD"]
        if not row["ready"]:
            self.log("[!] EXT_CSD: " + row["status"])
            return None
        settings = (self.ids.chk_voltage.active, self.ids.spin_bus.text,
                    self.emmc_preset, self.emmc_clock)
        if not self.extcsd_chip_ready or settings != self._extcsd_settings:
            self.log("[!] Для записи EXT_CSD выполните анализ eMMC или прочитайте EXT_CSD с чипа.")
            return None
        try:
            target, _ = load_file(row["path"])
            snapshot, cid = bytes(self.extcsd_data), bytes(self.extcsd_chip_cid)
            plan = write_plan(snapshot, target)
        except (OSError, ValueError) as error:
            self.log(f"[-] EXT_CSD: {error}")
            return None
        if plan:
            from tkinter import messagebox
            dialog = tk.Tk()
            dialog.withdraw()
            dialog.attributes("-topmost", True)
            try:
                lines = [f"CID: {cid.hex()}", f"Файл: {row['path']}", ""]
                lines += [f"[{i}] {WRITE_FIELDS[i]}: {old:02X} → {value:02X}"
                          for i, old, value in plan]
                if any(i == 162 for i, _, _ in plan):
                    lines.append("RST_N_FUNCTION: 01/02 могут фиксироваться НЕОБРАТИМО. "
                                 "Чип может отклонить смену установленного значения или возврат к 00; "
                                 "результат проверяется чтением.")
                if any(i == 178 and value & 0x10 for i, _, value in plan):
                    lines.append("Постоянная защита BOOT — НЕОБРАТИМА.")
                lines += ["", "Поле 160 не записывается; PARTITION_ACCESS сохраняется.",
                          "Применить эти настройки EXT_CSD с проверкой чтением?"]
                approved = messagebox.askyesno(
                    "Запись EXT_CSD из файла", "\n".join(lines), parent=dialog, default="no")
            finally:
                dialog.destroy()
            if not approved:
                return None
        self._extcsd_import = (target, row["path"])
        self._apply_extcsd_import()
        return snapshot, cid, target

    def _write_extcsd_file_result(self, data, cid):
        def apply(dt):
            self._show_extcsd(data, "Чип CID: " + cid.hex(), cid)
            self._apply_extcsd_import()
        Clock.schedule_once(apply, 0)

    def start_write_extcsd_file(self):
        if self.operation_running:
            return
        self.refresh_write_files()
        payload = self._prepare_write_extcsd_file()
        if payload is None:
            return
        self.operation_running = True
        try:
            started = self.orchestrator.run_write_extcsd_file(
                self.ids.chk_voltage.active, self.ids.spin_bus.text, payload,
                result_cb=self._write_extcsd_file_result)
            if started is False:
                self.operation_running = False
        except Exception:
            self.operation_running = False
            raise

    def start_write_all(self):
        if self.operation_running:
            return
        self.refresh_write_files()
        for area in ("BOOT1", "BOOT2", "USER"):
            if not self.write_files[area]["ready"]:
                self.log(f"[!] {area}: {self.write_files[area]['status']}")
                return
        files = {area: self.write_files[area]["path"] for area in ("BOOT1", "BOOT2", "USER")}
        row = self.write_files["EXTCSD"]
        payload = None
        if row["path"]:
            payload = self._prepare_write_extcsd_file()
            if payload is None:
                return
            files["EXTCSD"] = row["path"]
        elif row["status"].startswith(("Найдено файлов:", "Не удалось")):
            self.log("[!] EXT_CSD: " + row["status"])
            return
        else:
            self.log("[*] Файл EXT_CSD не выбран; записываются BOOT1, BOOT2 и Userdata.")
        self.operation_running = True
        try:
            started = self.orchestrator.run_write_all(
                self.ids.chk_voltage.active, self.ids.spin_bus.text,
                files, verify=self.verify_write, erase_before=self.erase_before_write, extcsd=payload,
                result_cb=self._write_extcsd_file_result)
            if started is False:
                self.operation_running = False
        except Exception:
            self.operation_running = False
            raise

    def start_write(self, part):
        if part == "EXTCSD":
            return self.start_write_extcsd_file()
        if self.operation_running or part not in AREA_NAMES:
            return
        # Recheck the folder immediately before starting, including file sizes.
        self.refresh_write_files()
        row = self.write_files[part]
        if not row["ready"]:
            self.log(f"[!] {part}: {row['status']}")
            return
        self.operation_running = True
        try:
            self.orchestrator.run_write_task(
                self.ids.chk_voltage.active, self.ids.spin_bus.text, part, row["path"], verify=self.verify_write, erase_before=self.erase_before_write,
            )
        except Exception:
            self.operation_running = False
            raise

    def on_erase_target_change(self, target):
            if target == "BOOT1" or target == "BOOT2":
                sec_count = (self.boot_size_bytes // 512) if self.boot_size_bytes > 0 else 8192
                self.ids.txt_erase_start.text = "0"
                self.ids.txt_erase_end.text = str(sec_count - 1)
            elif target == "User Area (Весь)":
                self.ids.txt_erase_start.text = "0"
                self.ids.txt_erase_end.text = str(self.user_total_sectors - 1) if self.user_total_sectors > 0 else "0 (Все)"
            elif target == "Пользовательский LBA":
                pass

    def start_erase(self):
        target = self.ids.spin_erase_target.text
        raw_start = self.ids.txt_erase_start.text.strip()
        raw_end = self.ids.txt_erase_end.text.strip()

        # Разбор начального сектора
        try:
            start_lba = int(raw_start)
        except ValueError:
            start_lba = 0

        # Разбор конечного сектора (если указано '0 (Все)', передаем 0 для автоопределения из ECSD)
        if "Все" in raw_end or "Авто" in raw_end or (raw_end == "0" and target == "User Area (Весь)"):
            end_lba = self.user_total_sectors - 1 if self.user_total_sectors > 0 else 0
        else:
            try:
                end_lba = int(raw_end)
            except ValueError:
                end_lba = 0

        if end_lba > 0 and end_lba < start_lba:
            self.log("[!] Конечный LBA не может быть меньше начального!")
            return

        is_18v = self.ids.chk_voltage.active
        bus = self.ids.spin_bus.text
        part_type = "BOOT1" if target == "BOOT1" else ("BOOT2" if target == "BOOT2" else "USER")

        self.orchestrator.run_erase_task(is_18v, bus, part_type, start_lba, end_lba)

    def create_gpt_from_scatter(self):
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        scatter_path = filedialog.askopenfilename(
            title="Выберите MTK Scatter файл (XML или TXT)",
            filetypes=[
                ("Scatter files", "*.xml;*.txt"),
                ("XML files", "*.xml"),
                ("Text files", "*.txt"),
                ("All files", "*.*")
            ]
        )
        if not scatter_path:
            root.destroy()
            return

        out_gpt_path = filedialog.asksaveasfilename(
            defaultextension=".bin",
            initialfile="gpt.bin",
            title="Сохранить сгенерированный образ GPT (LBA 0..33)",
            filetypes=[("Binary Image", "*.bin;*.img"), ("All files", "*.*")]
        )
        root.destroy()

        if out_gpt_path:
            # Если eMMC уже анализировалась, передаем реальный объем User Area в МБ
            user_mb = (self.user_total_sectors * 512) // (1024 * 1024) if self.user_total_sectors > 0 else 0
            self.orchestrator.run_scatter_to_gpt(scatter_path, out_gpt_path, user_mb)

    def stop_operation(self):
        self.orchestrator.stop_operation()

class FlasherApp(App):
    title = 'Xgecu Prog'
    from pathlib import Path as _Path
    icon = str(_Path(__file__).resolve().parent / 'assets' / 'xgecu_prog.png')

    def on_start(self):
        Window.maximize()

    def build(self):
        from pathlib import Path
        Builder.load_file(str(Path(__file__).resolve().with_name('gui.kv')))
        return MainScreen()

if __name__ == "__main__":
    FlasherApp().run()