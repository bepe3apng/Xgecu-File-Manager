import tkinter as tk
from tkinter import ttk
import threading
import struct
import sys

# Импорты из вашей библиотеки t48_emmc.py
from t48_emmc import (
    T48Emmc, EMMC_AUTO_4BIT_ISP, build_begin_transaction, 
    pack_cmd_B, LongRecvSubOp
)

# --- БАЗА СИГНАТУР И КОНСТАНТЫ ---
FS_SIGNATURES = (
    (0x438, b'\x53\xEF', 'EXT4'),
    (0x400, b'\x10\x20\xF5\xF2', 'F2FS'), 
    (0x400, b'\xE2\xE1\xF5\xE0', 'EROFS'),
    (0x000, b'hsqs', 'SquashFS'),
    (0x003, b'NTFS    ', 'NTFS'),
    (0x003, b'EXFAT   ', 'exFAT'),
    (0x052, b'FAT32   ', 'FAT32'),
    (0x036, b'FAT', 'FAT16/12'),
)

ADAPTER_HS = [bytes.fromhex(x) for x in (
    "24f0000001000000", "24e0280000000000000000e5", "24f1000000000000")]
OP_3E      = bytes.fromhex("3e01100000080000")

SWITCH_RPMB = bytes.fromhex("274600000003b301")
SWITCH_USER = bytes.fromhex("274600000007b302")
RD_SETUP    = bytes.fromhex("0d010000000000000002000020000000000100002000000020000000010000000100000000000000")
RD_14       = bytes.fromhex("14000000000000000100000200000000")
RD_15       = bytes.fromhex("15000002000000000100000200000000")

BLK = 512
CHUNK_BLOCKS = 32
CHUNK = CHUNK_BLOCKS * BLK

_MID = {
    0x02: "SanDisk", 0x11: "Toshiba/Kioxia", 0x13: "Micron", 0x15: "Samsung",
    0x45: "SanDisk", 0x70: "Kingston", 0x90: "SK Hynix", 0x9B: "YMTC",
    0xD6: "Foresee", 0xFE: "Micron", 0xF4: "Biwin"
}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def _set_u32(buf, off, val):
    b = bytearray(buf)
    struct.pack_into("<I", b, off, val & 0xFFFFFFFF)
    return bytes(b)

def swap_words(raw_bytes):
    if not raw_bytes or len(raw_bytes) != 16:
        return raw_bytes
    swapped = bytearray(16)
    for i in range(0, 16, 4):
        swapped[i:i+4] = raw_bytes[i:i+4][::-1]
    return bytes(swapped)

def decode_cid(reg16):
    c = swap_words(reg16)
    mid = c[0]
    pnm = ''.join(chr(x) if 32 <= x < 127 else '.' for x in c[3:9])
    rev = f"{c[9] >> 4}.{c[9] & 0xF}"
    sn = int.from_bytes(c[10:14], "big")
    mdt = c[14]
    date = f"{2013 + (mdt >> 4)}-{(mdt & 0xF):02d}"
    return c, _MID.get(mid, f"Unknown(0x{mid:02x})"), pnm, rev, sn, date

def fs_magic(sec):
    if not sec or sec.count(0) > len(sec) - 4:
        return "(blank)"
    if sec[0:4] == b"ANDR":
        return "android-boot"
    if sec[0:8] == b"\x88\x16\x88\x58" or sec[0:4] == b"\x3a\xff\x26\xed":
        return "sparse-img"
    for sig_offset, sig_bytes, fs_name in FS_SIGNATURES:
        if len(sec) >= sig_offset + len(sig_bytes):
            if sec[sig_offset : sig_offset + len(sig_bytes)] == sig_bytes:
                return fs_name
    return "?"

def parse_lifetime(val):
    if val == 0x00: return "Не определено"
    if val == 0x0B: return "ПРЕВЫШЕН МАКСИМУМ (>100%)"
    if 0x01 <= val <= 0x0A: return f"{(val-1)*10}% - {val*10}%"
    return f"Неизвестно (0x{val:02X})"

def parse_pre_eol(val):
    if val == 0x01: return "Норма (Normal)"
    if val == 0x02: return "ВНИМАНИЕ (Израсходовано 80% резерва)"
    if val == 0x03: return "КРИТИЧНО (Резервные блоки исчерпаны)"
    return f"Неизвестно (0x{val:02X})"

# --- GUI ПРИЛОЖЕНИЕ ---
class EMMCReaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("eMMC ISP Tool - PRO (Direct USB)")
        self.root.geometry("1200x800")
        
        self.var_voltage_18 = tk.BooleanVar(value=False)
        self.var_bus_4bit = tk.BooleanVar(value=True)
        self.var_clock = tk.StringVar(value="40") 
        
        self.setup_ui()

    def setup_ui(self):
        toolbar = ttk.Frame(self.root, padding=5)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        
        ttk.Button(toolbar, text="Connect & Read", command=self.start_task).pack(side=tk.LEFT, padx=5)
        
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Checkbutton(toolbar, text="T48 VCCQ 1.8V", variable=self.var_voltage_18).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(toolbar, text="4-bit Bus", variable=self.var_bus_4bit).pack(side=tk.LEFT, padx=5)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        
        ttk.Label(toolbar, text="Clock:").pack(side=tk.LEFT, padx=2)
        clock_cb = ttk.Combobox(
            toolbar, textvariable=self.var_clock, 
            values=["8", "12", "16", "20", "30", "40"], 
            width=3, state="readonly"
        )
        clock_cb.pack(side=tk.LEFT, padx=2)
        ttk.Label(toolbar, text="MHz").pack(side=tk.LEFT, padx=(0, 10))

        paned_window = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        left_frame = ttk.LabelFrame(paned_window, text="Partitions")
        paned_window.add(left_frame, weight=1)
        
        columns = ("#", "Name", "FS", "LBA", "Size")
        self.tree = ttk.Treeview(left_frame, columns=columns, show="headings")
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=80, anchor=tk.CENTER)
        self.tree.column("Name", width=150)
        self.tree.column("LBA", width=150)
        self.tree.pack(fill=tk.BOTH, expand=True)
        
        right_frame = ttk.LabelFrame(paned_window, text="Console Log")
        paned_window.add(right_frame, weight=2)
        
        self.hex_text = tk.Text(right_frame, font=("Consolas", 10), wrap=tk.NONE)
        v_scroll = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=self.hex_text.yview)
        h_scroll = ttk.Scrollbar(right_frame, orient=tk.HORIZONTAL, command=self.hex_text.xview)
        self.hex_text.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
        
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.hex_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        self.hex_text.insert(tk.END, message + "\n")
        self.hex_text.see(tk.END)

    def add_to_tree(self, values):
        self.tree.insert("", tk.END, values=values)

    def start_task(self):
        self.hex_text.delete(1.0, tk.END)
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        threading.Thread(target=self._run_emmc_operations, daemon=True).start()

    # --- ЛОГИКА ВЗАИМОДЕЙСТВИЯ С EMMC (РАБОТАЕТ В ПОТОКЕ) ---
    def _arm_bulk(self, d):
        d.ep1_send(SWITCH_RPMB); d.ep1_recv(64)
        d.ep1_send(RD_14)
        d.ep2_send(b"\x00" * 512)
        d.ep1_send(RD_15)
        d.ep2_recv(512)
        d.ep2_recv(16)
        d.ep1_send(SWITCH_USER); d.ep1_recv(64)

    def _read_region(self, d, start_block, n_blocks):
        base = start_block - (start_block % CHUNK_BLOCKS)
        off = (start_block - base) * BLK
        n_chunks = (off + n_blocks * BLK + CHUNK - 1) // CHUNK
        
        setup = _set_u32(_set_u32(RD_SETUP, 4, base), 16, n_chunks)
        d.ep1_send(setup)
        
        data = bytearray()
        for _ in range(n_chunks):
            big = d.ep2_recv(CHUNK)
            tail = d.ep2_recv(16)
            data += big[16:] + tail 
            
        return bytes(data)[off:off + n_blocks * BLK]

    def _parse_partitions(self, d):
        self.root.after(0, self.log, "\n[*] Чтение таблицы разделов (GPT/MBR)...")
        buf = self._read_region(d, 0, 64) 
        
        mbr, gpt = buf[0:512], buf[512:1024]
        scheme = "none"
        parts = []
        
        if gpt[:8] == b"EFI PART":
            scheme = "GPT"
            entry_lba = struct.unpack_from("<Q", gpt, 72)[0]
            num = struct.unpack_from("<I", gpt, 80)[0]
            esize = struct.unpack_from("<I", gpt, 84)[0]
            
            for i in range(min(num, 256)):
                o = entry_lba * BLK + i * esize
                if o + esize > len(buf):
                    break
                e = buf[o:o + esize]
                if e[:16] == b"\x00" * 16:
                    continue
                first = struct.unpack_from("<Q", e, 32)[0]
                last = struct.unpack_from("<Q", e, 40)[0]
                name = e[56:128].decode("utf-16-le", "replace").rstrip("\x00")
                parts.append((name or "(unnamed)", first, last))
                
        elif mbr[510:512] == b"\x55\xaa":
            scheme = "MBR"
            for i in range(4):
                p = mbr[446 + i * 16: 446 + (i + 1) * 16]
                if p[4] == 0:
                    continue
                start = struct.unpack_from("<I", p, 8)[0]
                cnt = struct.unpack_from("<I", p, 12)[0]
                parts.append(("type=0x%02x" % p[4], start, start + cnt - 1))
                
        self.root.after(0, self.log, f"== USER partition table ==\n  scheme: {scheme}, {len(parts)} partition(s)")
        
        for idx, (name, first, last) in enumerate(parts, 1):
            mb = (last - first + 1) * BLK / (1024 * 1024)
            fs = "?"
            try:
                fs = fs_magic(self._read_region(d, first, 4)) 
            except Exception:
                fs = "(error)"
                
            self.root.after(0, self.add_to_tree, (str(idx), name, fs, f"{first}..{last}", f"{mb:.2f} MB"))
            self.root.after(0, self.log, f"  {name:<20} LBA {first:>10}..{last:<10} {mb:8.1f} MiB  fs={fs}")

    def _run_emmc_operations(self):
        self.root.after(0, self.log, "[*] Подключение к программатору XGecu T48...")
        d = T48Emmc()
        try:
            d.connect()
            for ep in (0x01, 0x81, 0x02, 0x82):
                try: d.dev.clear_halt(ep)
                except Exception: pass

            # --- 1. РУКОПОЖАТИЕ АДАПТЕРА ---
            d.ep1_send(ADAPTER_HS[0])
            d.ep1_send(ADAPTER_HS[1])
            try: d.ep1_recv(512)
            except Exception: pass
            d.ep1_send(ADAPTER_HS[2])
            
            d.ep1_send(OP_3E)
            try: d.ep1_recv(512)
            except Exception: pass

            # --- 2. СТАРТ СЕССИИ С УЧЕТОМ GUI НАСТРОЕК ---
            # Модифицируем пакет инициализации на лету под 1.8V если нужно
            raw_begin = bytearray(build_begin_transaction(EMMC_AUTO_4BIT_ISP))
            if self.var_voltage_18.get():
                raw_begin[0x15] = 0x01
                self.root.after(0, self.log, "[*] Применено напряжение VCCQ = 1.8V")
            else:
                raw_begin[0x15] = 0x00
                self.root.after(0, self.log, "[*] Применено напряжение VCCQ = 3.3V")
            
            session = d.begin_session_with_ovc_check(bytes(raw_begin))
            if not session.get('success', False):
                raise RuntimeError("Сработала защита OVC! Проверьте замыкание.")

            # --- 3. ИНИЦИАЛИЗАЦИЯ ЧИПА ---
            self.root.after(0, self.log, "[*] Инициализация eMMC (CMD0/1/2/3)...")
            info = d.init_emmc(algo_param=0x00)
            if info.get('cid') is None:
                raise RuntimeError("eMMC не ответил на инициализацию. Проверьте колодку/пайку.")

            # --- 4. РАЗБОР CID ---
            raw_cid = info.get('cid')
            cid_bytes = raw_cid[:16] if len(raw_cid) >= 16 else raw_cid
            _, manufacturer, pnm, rev, sn, date = decode_cid(cid_bytes)
            
            cid_info = (
                f"\n--- Информация о чипе (CID) ---\n"
                f"Производитель : {manufacturer}\n"
                f"Модель (PNM)  : {pnm.strip()}\n"
                f"Ревизия       : {rev}\n"
                f"Серийный номер: 0x{sn:08x}\n"
                f"Дата выпуска  : {date}"
            )
            self.root.after(0, self.log, cid_info)

            # --- 5. ЧТЕНИЕ EXT_CSD И ОЦЕНКА ИЗНОСА ---
            self.root.after(0, self.log, "\n[*] Чтение регистра EXT_CSD...")
            d.ep1_send(pack_cmd_B(LongRecvSubOp.READ_BLOCK_512, 0x200, 0))
            ep2_data = d.ep2_recv(520)
            
            if ep2_data and len(ep2_data) >= 520:
                ext_csd = ep2_data[8:520]
                sec_count = int.from_bytes(ext_csd[212:216], byteorder='little')
                capacity_gb = (sec_count * 512) / (1024 * 1024 * 1024)
                
                boot_size_kb = ext_csd[226] * 128
                rpmb_size_kb = ext_csd[168] * 128
                ext_csd_rev = ext_csd[192]
                
                rev_map = {1: "4.1", 5: "4.41", 6: "4.5", 7: "5.0", 8: "5.1"}
                emmc_ver = rev_map.get(ext_csd_rev, f"Unknown ({ext_csd_rev})")
                
                pre_eol = ext_csd[267]
                life_a = ext_csd[268] 
                life_b = ext_csd[269] 
                
                ext_info = (
                    f"--- Состояние eMMC (EXT_CSD) ---\n"
                    f"Версия eMMC      : {emmc_ver}\n"
                    f"Объем USER       : {capacity_gb:.2f} ГБ\n"
                    f"Объем BOOT1/2    : {boot_size_kb} КБ\n"
                    f"Объем RPMB       : {rpmb_size_kb} КБ\n"
                    f"Общее здоровье   : {parse_pre_eol(pre_eol)}\n"
                    f"Износ SLC (Typ A): {parse_lifetime(life_a)}\n"
                    f"Износ TLC (Typ B): {parse_lifetime(life_b)}"
                )
                self.root.after(0, self.log, ext_info)

            # --- 6. ЧТЕНИЕ GPT И ФАЙЛОВЫХ СИСТЕМ ---
            self._arm_bulk(d)           
            self._parse_partitions(d)    

            self.root.after(0, self.log, "\n[+] Все операции успешно завершены.")

        except Exception as e:
            self.root.after(0, self.log, f"\n[!] Ошибка выполнения: {e}")

        finally:
            self.root.after(0, self.log, "[*] Закрытие сессии T48...")
            for ep in (0x82, 0x81):
                for _ in range(6):
                    try:
                        if not d.dev.read(ep, 16384, timeout=400): break
                    except Exception: break
            try: d.ep1_send(bytes.fromhex("0400000000000000"))
            except Exception: pass
            d.close()

if __name__ == "__main__":
    root = tk.Tk()
    app = EMMCReaderApp(root)
    root.mainloop()