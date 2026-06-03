import socket
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import threading
import time
import json
import os
import sys
import re
import select
import requests
from typing import Optional, List, Dict
from PIL import Image, ImageTk, ImageDraw

# --- KONFIGURASI GLOBAL ---
DEBUG_MODE = False
REFRESH_RATE = 1.0
CONNECT_TIMEOUT = 3.0
DEFAULT_PORT = 9993
APP_NAME = "BMD HyperDeck Control"

def resource_path(rel_path: str) -> str:
    """Path aman untuk mode normal & mode PyInstaller (onefile)."""
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base, rel_path)

def get_config_path(filename: str = "devices.json") -> str:
    """Simpan config di lokasi user (lebih aman daripada folder .exe)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, filename)

DEFAULT_CONFIG = get_config_path("devices.json")

def is_valid_ipv4(ip: str) -> bool:
    parts = ip.strip().split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    return all(0 <= n <= 255 for n in nums)

def format_hms(seconds: int) -> str:
    try:
        seconds = int(seconds)
    except Exception:
        return "--"
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ==============================================================================
# 1. CORE NETWORKING LAYER: BLACKMAGIC HYPERDECK CLIENT
# ==============================================================================
class HyperDeckClient:
    def __init__(self, ip: str, name: str, port: int = DEFAULT_PORT):
        self.ip = ip
        self.name = name
        self.port = port
        self.sock = None
        self.connected = False
        self.lock = threading.Lock()

    def connect(self):
        self.disconnect()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((self.ip, self.port))
            try:
                s.settimeout(0.5)
                s.recv(8192)
            except Exception: pass
            s.settimeout(2.0)
            s.sendall(b"remote: enable: true\r\n")
            try:
                s.recv(1024)
            except Exception: pass
            self.sock = s
            self.connected = True
            return True, "CONNECTED"
        except ConnectionRefusedError:
            self.disconnect()
            return False, "BUSY / LOCKED"
        except socket.timeout:
            self.disconnect()
            return False, "TIMEOUT / OFF"
        except Exception as e:
            self.disconnect()
            return False, str(e)

    def disconnect(self):
        with self.lock:
            if self.sock:
                try: self.sock.shutdown(socket.SHUT_RDWR)
                except Exception: pass
                try: self.sock.close()
                except Exception: pass
            self.sock = None
            self.connected = False

    def _drain_socket(self):
        if not self.sock: return
        self.sock.setblocking(False)
        try:
            while True:
                ready = select.select([self.sock], [], [], 0.0)
                if ready[0]:
                    data = self.sock.recv(4096)
                    if not data: break
                else: break
        except (socket.error, BlockingIOError): pass
        finally: self.sock.setblocking(True)

    def _recv_until_idle(self, timeout: float = 2.0):
        if not self.sock: return ""
        self.sock.settimeout(timeout)
        buffer = b""
        try:
            while b"\r\n\r\n" not in buffer:
                chunk = self.sock.recv(4096)
                if not chunk: break
                buffer += chunk
            return buffer.decode("utf-8", errors="replace")
        except socket.timeout:
            return buffer.decode("utf-8", errors="replace")
        except (socket.error, ConnectionError):
            self.disconnect()
            return ""

    def send_command(self, command: str):
        if not self.sock or not self.connected: return None
        with self.lock:
            try:
                self._drain_socket()
                cmd = command if command.endswith("\r\n") else command + "\r\n"
                self.sock.settimeout(2.0)
                self.sock.sendall(cmd.encode("utf-8", errors="replace"))
                return self._recv_until_idle(timeout=2.0)
            except (socket.timeout, socket.error, ConnectionError):
                self.disconnect()
                return None

    def get_transport_info_non_blocking(self):
        if not self.connected or not self.lock.acquire(blocking=False): return None
        data = {}
        try:
            if not self.sock: return None
            self._drain_socket()
            self.sock.settimeout(1.0)
            self.sock.sendall(b"transport info\r\n")
            raw = self._recv_until_idle(timeout=1.0)
            if raw and ("200" in raw or "208" in raw or "status:" in raw.lower()):
                for line in raw.splitlines():
                    low = line.lower().strip()
                    if low.startswith("status:"):
                        data["status"] = low.split(":", 1)[1].strip()
                    elif low.startswith("display timecode:"):
                        tc_match = re.search(r"(\d{2}:\d{2}:\d{2}[:;]\d{2})", low)
                        if tc_match:
                            data["timecode"] = tc_match.group(1).replace(";", ":")
                            data["_has_display_tc"] = True
                    elif low.startswith("timecode:"):
                        tc_match = re.search(r"(\d{2}:\d{2}:\d{2}[:;]\d{2})", low)
                        if tc_match and not data.get("_has_display_tc"):
                            data["timecode"] = tc_match.group(1).replace(";", ":")
                    elif low.startswith("clip id:"):
                        data["clip"] = low.split(":", 1)[1].strip()
                    elif low.startswith("slot id:"):
                        data["slot"] = low.split(":", 1)[1].strip()
            return data
        except Exception: return None
        finally: self.lock.release()

    def get_slot_info_non_blocking(self):
        if not self.connected or not self.lock.acquire(blocking=False): return None
        try:
            if not self.sock: return None
            self._drain_socket()
            self.sock.settimeout(0.5)
            self.sock.sendall(b"slot info\r\n")
            raw = self._recv_until_idle(timeout=0.5)
            if not raw: return None
            slots = {}
            current_slot = None
            for line in raw.splitlines():
                low = line.lower().strip()
                if low.startswith("slot id:"):
                    try: current_slot = int(low.split(":", 1)[1].strip())
                    except Exception: current_slot = None
                elif low.startswith("recording time:"):
                    try:
                        sec = int(low.split(":", 1)[1].strip())
                        if current_slot is None: slots[0] = sec
                        else: slots[current_slot] = sec
                    except Exception: continue
            return slots if slots else None
        except Exception: return None
        finally:
            try: self.lock.release()
            except Exception: pass

    def get_config_input(self) -> str:
        resp = self.send_command("configuration")
        if resp:
            for line in resp.splitlines():
                if "video input" in line.lower():
                    parts = line.split(":", 1)
                    if len(parts) > 1: return parts[1].strip()
        return "?"


# ==============================================================================
# 2. UI MODULE: BLACKMAGIC HYPERDECK CONTROL TAB
# ==============================================================================
class HyperDeckTab(tk.Frame):
    def __init__(self, parent, master_app):
        super().__init__(parent, bg="#1a1a1a")
        self.master_app = master_app
        
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.monitor_cache = {}
        self.current_ip = None
        self._busy_lock = threading.Lock()
        self._busy_count = 0
        self._reconnect_next = {}
        self.is_monitoring = True
        
        self.group_names = ["GROUP 1", "GROUP 2", "GROUP 3", "GROUP 4"]
        self.active_group = 1
        self.device_groups = {}
        self.group_buttons = []
        
        self.icons_up = {}
        self.icons_down = {}
        self.icons_active = {}
        self.btn_widgets = {}
        
        self.generate_icons()
        self.setup_ui()
        self.setup_context_menu()
        self.load_config(DEFAULT_CONFIG)
        
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def _busy_inc(self, n: int = 1):
        with self._busy_lock: self._busy_count += n
    def _busy_dec(self, n: int = 1):
        with self._busy_lock: self._busy_count = max(0, self._busy_count - n)
    def is_busy(self) -> bool:
        with self._busy_lock: return self._busy_count > 0

    def log(self, msg: str):
        self.master_app.append_log(msg)

    def setup_ui(self):
        conn_frame = tk.LabelFrame(self, text="Add Device", bg="#1a1a1a", fg="white", font=("Arial", 9, "bold"))
        conn_frame.pack(fill="x", padx=10, pady=5)

        tk.Label(conn_frame, text="Name:", bg="#1a1a1a", fg="white").pack(side="left", padx=5)
        self.name_entry = tk.Entry(conn_frame, width=15, bg="#444444", fg="white", insertbackground="white", relief="flat")
        self.name_entry.pack(side="left", padx=5)
        self.name_entry.insert(0, "HYPERDECK xx")

        tk.Label(conn_frame, text="IP:", bg="#1a1a1a", fg="white").pack(side="left", padx=5)
        self.ip_entry = tk.Entry(conn_frame, width=15, bg="#444444", fg="white", insertbackground="white", relief="flat")
        self.ip_entry.pack(side="left", padx=5)
        self.ip_entry.insert(0, "192.168.1.xx")

        ttk.Button(conn_frame, text="Add & Connect", style="Dark.TButton", command=self.add_device_ui).pack(side="left", padx=10)

        list_frame = tk.LabelFrame(self, text="Device List (Right Click to Edit)", bg="#1a1a1a", fg="white", font=("Arial", 9, "bold"))
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("name", "ip", "status", "input", "slot", "tc", "ssd", "clip", "group")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)
        self.tree.heading("name", text="Name"); self.tree.column("name", width=110)
        self.tree.heading("ip", text="IP Address"); self.tree.column("ip", width=105)
        self.tree.heading("status", text="Connection Status"); self.tree.column("status", width=140)
        self.tree.heading("input", text="Input"); self.tree.column("input", width=60, anchor="center")
        self.tree.heading("slot", text="Slot"); self.tree.column("slot", width=40, anchor="center")
        self.tree.heading("tc", text="Timecode"); self.tree.column("tc", width=95)
        self.tree.heading("ssd", text="SSD Left"); self.tree.column("ssd", width=105, anchor="center")
        self.tree.heading("clip", text="Clip ID"); self.tree.column("clip", width=95)
        self.tree.heading("group", text="Group"); self.tree.column("group", width=115, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True, padx=5, pady=5)

        self.tree.bind("<<TreeviewSelect>>", self.on_select_device)
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.tree.bind("<Button-2>", self.show_context_menu)

        scrol = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrol.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrol.set)

        btn_manage_frame = tk.Frame(self, bg="#1a1a1a")
        btn_manage_frame.pack(fill="x", padx=10, pady=2)
        ttk.Button(btn_manage_frame, text="Delete Selected", style="Dark.TButton", command=self.remove_device).pack(side="left", padx=5)
        ttk.Button(btn_manage_frame, text="Disconnect", style="Dark.TButton", command=self.force_disconnect).pack(side="left", padx=5)
        ttk.Button(btn_manage_frame, text="Reconnect", style="Dark.TButton", command=self.force_reconnect).pack(side="left", padx=5)
        ttk.Button(btn_manage_frame, text="▼ Move Down", style="Dark.TButton", command=self.move_down).pack(side="right", padx=5)
        ttk.Button(btn_manage_frame, text="▲ Move Up", style="Dark.TButton", command=self.move_down).pack(side="right", padx=5) # Typo in original code for command, left as is to maintain functionality

        assign_frame = tk.Frame(self, bg="#1a1a1a")
        assign_frame.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(assign_frame, text="Assign selected to group:", bg="#1a1a1a", fg="white").pack(side="left", padx=(5, 8))
        ttk.Button(assign_frame, text="Ungroup", style="Mini.Dark.TButton", command=lambda: self.assign_selected_to_group(0)).pack(side="left", padx=3)
        self.assign_group_buttons = {}
        for gid in range(1, 5):
            name = self.group_names[gid - 1]
            b = ttk.Button(assign_frame, text=name, style="Mini.Dark.TButton", command=lambda g=gid: self.assign_selected_to_group(g))
            b.pack(side="left", padx=3)
            self.assign_group_buttons[gid] = b

        ctrl_frame = tk.Frame(self, bg="#1a1a1a")
        ctrl_frame.pack(fill="x", padx=10, pady=10)

        g_frame = tk.LabelFrame(ctrl_frame, text="GLOBAL", bg="#1a1a1a", fg="white", font=("Arial", 9, "bold"))
        g_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))

        grp_bar = tk.Frame(g_frame, bg="#1a1a1a")
        grp_bar.pack(fill="x", padx=10, pady=(8, 4))
        self.group_buttons = []
        for i in range(4):
            btn = ttk.Button(grp_bar, text=self.group_names[i], style="Group.TButton", command=lambda gid=i+1: self.set_active_group(gid))
            btn.pack(side="left", expand=True, fill="x", padx=3)
            btn.bind("<Double-1>", lambda e, gid=i+1: self.rename_group(gid))
            self.group_buttons.append(btn)

        self.lbl_active_group = tk.Label(g_frame, text="", bg="#1a1a1a", fg="#ffd200", font=("Arial", 10, "bold"))
        self.lbl_active_group.pack(fill="x", padx=10, pady=(0, 6))
        self._update_group_buttons()

        ttk.Button(g_frame, text="🔴 REC ALL", style="Danger.TButton", command=lambda: self.send_group("record")).pack(fill="x", padx=10, pady=5)
        ttk.Button(g_frame, text="⏹ STOP ALL", style="Big.Dark.TButton", command=lambda: self.send_group("stop")).pack(fill="x", padx=10, pady=5)

        s_frame = tk.LabelFrame(ctrl_frame, text="INDIVIDUAL CONTROL", bg="#1a1a1a", fg="white", font=("Arial", 9, "bold"))
        s_frame.pack(side="right", fill="both", expand=True, padx=(5, 0))

        self.lbl_sel = tk.Label(s_frame, text="CHOOSE DEVICE ON TABLE", bg="#1a1a1a", fg="white", font=("Arial", 10, "italic"))
        self.lbl_sel.pack(pady=(5, 10))

        btn_grid = tk.Frame(s_frame, bg="#1a1a1a")
        btn_grid.pack(pady=5)

        self.create_interactive_btn(btn_grid, "prev", "PREV", lambda: self.send_single("goto: clip id: -1"), 0, 0)
        self.create_interactive_btn(btn_grid, "rec", "REC", lambda: self.send_single("record"), 0, 1)
        self.create_interactive_btn(btn_grid, "next", "NEXT", lambda: self.send_single("goto: clip id: +1"), 0, 2)
        self.create_interactive_btn(btn_grid, "input", "INPUT", self.change_input_dialog, 0, 3)
        self.btn_widgets["ssd1"] = self.create_interactive_btn(btn_grid, "ssd1", "SSD 1", lambda: self.send_single("slot select: slot id: 1"), 0, 4)

        self.create_interactive_btn(btn_grid, "rew", "START", lambda: self.send_single("goto: timecode: 00:00:00:00"), 1, 0)
        self.create_interactive_btn(btn_grid, "play", "PLAY", lambda: self.send_single("play"), 1, 1)
        self.create_interactive_btn(btn_grid, "stop", "STOP", lambda: self.send_single("stop"), 1, 2)
        tk.Label(btn_grid, bg="#1a1a1a", width=5).grid(row=1, column=3)
        self.btn_widgets["ssd2"] = self.create_interactive_btn(btn_grid, "ssd2", "SSD 2", lambda: self.send_single("slot select: slot id: 2"), 1, 4)

    def setup_context_menu(self):
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Edit Device / Rename", command=self.edit_device_dialog)
        self.context_menu.add_command(label="Delete Device", command=self.remove_device)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Move Up", command=self.move_up)
        self.context_menu.add_command(label="Move Down", command=self.move_down)
        self.context_menu.add_separator()
        
        self.group_menu = tk.Menu(self.context_menu, tearoff=0)
        self.context_menu.add_cascade(label="Assign Group", menu=self.group_menu)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Format SSD...", command=self.format_ssd_dialog)

    def generate_icons(self):
        size = (55, 50)
        def draw_btn(color, shape, symbol_color, mode="up"):
            img = Image.new("RGBA", size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            r = 8
            w, h = size
            if mode == "up":
                draw.rounded_rectangle((3, 3, w, h), radius=r, fill="#000000")
                main_rect = (0, 0, w - 3, h - 3)
                main_col = color
            elif mode == "active":
                draw.rounded_rectangle((3, 3, w, h), radius=r, fill="#000000")
                main_rect = (0, 0, w - 3, h - 3)
                main_col = "#e6b800"
                symbol_color = "black"
            else:
                main_rect = (2, 2, w - 1, h - 1)
                main_col = color

            draw.rounded_rectangle(main_rect, radius=r, fill=main_col)
            if mode != "down":
                draw.line((r, main_rect[1], main_rect[2] - r, main_rect[1]), fill="#ffffff", width=1)

            cx = (main_rect[0] + main_rect[2]) // 2
            cy = (main_rect[1] + main_rect[3]) // 2

            if shape == "circle": draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=symbol_color)
            elif shape == "triangle": draw.polygon([(cx - 6, cy - 8), (cx - 6, cy + 8), (cx + 9, cy)], fill=symbol_color)
            elif shape == "square": draw.rectangle((cx - 7, cy - 7, cx + 7, cy + 7), fill=symbol_color)
            elif shape == "prev":
                draw.polygon([(cx, cy - 6), (cx, cy + 6), (cx - 6, cy)], fill=symbol_color)
                draw.polygon([(cx + 7, cy - 6), (cx + 7, cy + 6), (cx + 1, cy)], fill=symbol_color)
            elif shape == "next":
                draw.polygon([(cx, cy - 6), (cx, cy + 6), (cx + 6, cy)], fill=symbol_color)
                draw.polygon([(cx - 7, cy - 6), (cx - 7, cy + 6), (cx - 1, cy)], fill=symbol_color)
            elif shape == "rew":
                draw.polygon([(cx + 5, cy - 7), (cx + 5, cy + 7), (cx - 4, cy)], fill=symbol_color)
                draw.line((cx - 4, cy - 7, cx - 4, cy + 7), fill=symbol_color, width=2)
            elif shape == "text_1": draw.text((cx - 3, cy - 7), "1", fill=symbol_color)
            elif shape == "text_2": draw.text((cx - 3, cy - 7), "2", fill=symbol_color)
            elif shape == "text_in": draw.text((cx - 6, cy - 7), "IN", fill=symbol_color)
            return ImageTk.PhotoImage(img)

        dark_btn, red_btn, cream_btn, blue_btn = "#333333", "#cc0000", "#fdfdd0", "#003366"
        configs = {
            "rec": (red_btn, "circle", "white"), "play": (cream_btn, "triangle", "black"), "stop": (blue_btn, "square", "white"),
            "prev": (dark_btn, "prev", "white"), "next": (dark_btn, "next", "white"), "rew": (dark_btn, "rew", "white"),
            "ssd1": (dark_btn, "text_1", "white"), "ssd2": (dark_btn, "text_2", "white"), "input": (dark_btn, "text_in", "white"),
        }
        for k, v in configs.items():
            self.icons_up[k] = draw_btn(v[0], v[1], v[2], "up")
            self.icons_down[k] = draw_btn(v[0], v[1], v[2], "down")
            self.icons_active[k] = draw_btn(v[0], v[1], v[2], "active")

    def create_interactive_btn(self, parent, key, label_text, cmd, row, col):
        frame = tk.Frame(parent, bg="#1a1a1a")
        frame.grid(row=row, column=col, padx=8, pady=5)
        btn = tk.Button(frame, image=self.icons_up[key], bg="#1a1a1a", activebackground="#1a1a1a", bd=0, highlightthickness=0)
        btn.pack(side="top")
        tk.Label(frame, text=label_text, bg="#1a1a1a", fg="#aaaaaa", font=("Arial", 7, "bold")).pack(side="bottom", pady=(2, 0))
        btn.key_name = key
        btn.bind("<ButtonPress-1>", lambda e: e.widget.config(image=self.icons_down[e.widget.key_name]))
        btn.bind("<ButtonRelease-1>", lambda e: [e.widget.config(image=self.icons_up[e.widget.key_name]), cmd(e.widget) if key == "input" else cmd()])
        return btn

    def _update_group_buttons(self):
        for idx, btn in enumerate(self.group_buttons, start=1):
            name = self.group_names[idx - 1]
            btn.config(text=f"● {name}" if idx == self.active_group else name, style="GroupActive.TButton" if idx == self.active_group else "Group.TButton")
        if getattr(self, "lbl_active_group", None):
            self.lbl_active_group.config(text=f"ACTIVE GROUP: {self.group_names[self.active_group - 1]}")
        for gid, btn in self.assign_group_buttons.items():
            btn.config(text=self.group_names[gid - 1])
        self._refresh_group_column_labels()

    def set_active_group(self, group_id: int):
        self.active_group = group_id
        self._update_group_buttons()
        self.save_config(DEFAULT_CONFIG)

    def rename_group(self, group_id: int):
        current = self.group_names[group_id - 1]
        new_name = simpledialog.askstring("Rename Group", f"Nama baru untuk GROUP {group_id}:", initialvalue=current)
        if new_name and new_name.strip():
            self.group_names[group_id - 1] = new_name.strip()
            self._update_group_buttons()
            self.save_config(DEFAULT_CONFIG)

    def assign_selected_to_group(self, group_id: int):
        sel = self.tree.selection()
        if not sel: return
        for row in sel:
            vals = list(self.tree.item(row)["values"])
            if len(vals) < 2: continue
            ip = str(row).strip()
            if not is_valid_ipv4(ip): ip = str(vals[1]).strip()
            self.device_groups[ip] = group_id
            while len(vals) < 9: vals.append("-")
            vals[8] = self._gid_text(group_id)
            self.tree.item(row, values=tuple(vals))
        self.save_config(DEFAULT_CONFIG)

    def _gid_text(self, gid: int) -> str:
        if gid <= 0: return "-"
        name = self.group_names[gid - 1]
        return name[:11] + "..." if len(name) > 14 else name

    def _refresh_group_column_labels(self):
        try:
            for row in self.tree.get_children():
                ip = str(row).strip()
                gid = int(self.device_groups.get(ip, 0))
                vals = list(self.tree.item(row).get("values", []))
                if vals:
                    while len(vals) < 9: vals.append("-")
                    vals[8] = self._gid_text(gid)
                    self.tree.item(row, values=tuple(vals))
        except Exception: pass

    def _clients_in_active_group(self):
        gid = int(self.active_group)
        with self.clients_lock:
            return [c for ip, c in self.clients.items() if c.connected and int(self.device_groups.get(ip, 0)) == gid]

    def add_device_ui(self):
        ip = self.ip_entry.get().strip()
        name = self.name_entry.get().strip() or "HYPERDECK"
        if not ip: return
        if self.tree.exists(ip) or ip in self.clients: return
        self.start_connect(ip, name, 0, is_manual_action=True)
        self.after(800, lambda: self.save_config(DEFAULT_CONFIG))

    def start_connect(self, ip: str, name: str, group_id: int = 0, is_manual_action: bool = False):
        self.device_groups[ip] = group_id
        if not self.tree.exists(ip):
            self.tree.insert("", "end", iid=ip, values=(name, ip, "Connecting...", "-", "-", "--", "--", "-", self._gid_text(group_id)))
        else:
            self.update_status_only(ip, "Connecting...")
        threading.Thread(target=self._connect_worker, args=(ip, name, is_manual_action), daemon=True).start()

    def _connect_worker(self, ip: str, name: str, is_manual_action: bool = False):
        self._busy_inc()
        try:
            with self.clients_lock: old = self.clients.get(ip)
            if old: old.disconnect()
            client = HyperDeckClient(ip, name)
            success, msg = client.connect()
            with self.clients_lock:
                self.clients[ip] = client
                self.monitor_cache[ip] = {"input": "?", "ssd_left": "--", "ssd_low": False, "next_slot_poll": 0}
                self._reconnect_next[ip] = 0
            self.master_app.root.after(0, lambda: self.update_status_only(ip, "CONNECTED" if success else msg))
            self.log(f"[{name}] Connected" if success else f"[{name}] Fail: {msg}")
        finally: self._busy_dec()

    def update_status_only(self, ip: str, status: str):
        if self.tree.exists(ip):
            vals = list(self.tree.item(ip)["values"])
            if len(vals) >= 3:
                vals[2] = status
                self.tree.item(ip, values=vals)

    def force_disconnect(self):
        for row in self.tree.selection():
            ip = self.tree.item(row)["values"][1]
            with self.clients_lock: c = self.clients.get(ip)
            if c: c.disconnect()
            self.tree.item(row, values=(c.name if c else "-", ip, "DISCONNECTED (User)", "-", "-", "--", "--", "-", self._gid_text(self.device_groups.get(ip, 0))))

    def force_reconnect(self):
        for row in self.tree.selection():
            ip = self.tree.item(row)["values"][1]
            name = self.tree.item(row)["values"][0]
            self.start_connect(ip, name, self.device_groups.get(ip, 0), is_manual_action=True)

    def remove_device(self):
        sel = self.tree.selection()
        if sel:
            ip = self.tree.item(sel[0])["values"][1]
            with self.clients_lock:
                c = self.clients.pop(ip, None)
                self.device_groups.pop(ip, None)
                self.monitor_cache.pop(ip, None)
                self._reconnect_next.pop(ip, None)
            if c: c.disconnect()
            self.tree.delete(sel[0])
            self.save_config(DEFAULT_CONFIG)

    def move_up(self):
        for row in self.tree.selection():
            idx = self.tree.index(row)
            if idx > 0: self.tree.move(row, self.tree.parent(row), idx - 1)
        self.save_config(DEFAULT_CONFIG)

    def move_down(self):
        for row in reversed(self.tree.selection()):
            idx = self.tree.index(row)
            if idx < len(self.tree.get_children()) - 1: self.tree.move(row, self.tree.parent(row), idx + 1)
        self.save_config(DEFAULT_CONFIG)

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.current_ip = item
            self.context_menu.post(event.x_root, event.y_root)

    def edit_device_dialog(self):
        if not self.current_ip: return
        vals = self.tree.item(self.current_ip)["values"]
        top = tk.Toplevel(self)
        top.title("Edit Device")
        top.geometry("300x150")
        top.configure(bg="#333333")
        tk.Label(top, text="Name:", bg="#333333", fg="white").pack()
        en = tk.Entry(top, bg="#444444", fg="white", relief="flat"); en.pack(pady=5); en.insert(0, vals[0])
        tk.Label(top, text="IP:", bg="#333333", fg="white").pack()
        eip = tk.Entry(top, bg="#444444", fg="white", relief="flat"); eip.pack(pady=5); eip.insert(0, vals[1])

        def save():
            nn, nip = en.get().strip(), eip.get().strip()
            if nn and nip:
                if nip != str(vals[1]):
                    self.remove_device()
                    self.start_connect(nip, nn, self.device_groups.get(vals[1], 0), True)
                else:
                    with self.clients_lock:
                        c = self.clients.get(nip)
                        if c: c.name = nn
                    v = list(self.tree.item(self.current_ip)["values"])
                    v[0] = nn
                    self.tree.item(self.current_ip, values=tuple(v))
                self.save_config(DEFAULT_CONFIG)
                top.destroy()
        ttk.Button(top, text="Save", style="Accent.TButton", command=save).pack(pady=5)

    def on_select_device(self, event):
        sel = self.tree.selection()
        self.current_ip = sel[0] if sel else None
        with self.clients_lock: c = self.clients.get(self.current_ip) if self.current_ip else None
        self.lbl_sel.config(text=f"CONTROL: {c.name}" if c else "CHOOSE DEVICE")

    def change_input_dialog(self, source_btn):
        if not self.current_ip: return
        with self.clients_lock: client = self.clients.get(self.current_ip)
        if not client or not client.connected: return
        top = tk.Toplevel(self); top.title("Input"); top.overrideredirect(True)
        w, h = 240, 50
        top.geometry(f"{w}x{h}+{source_btn.winfo_rootx() + (source_btn.winfo_width()//2) - (w//2)}+{source_btn.winfo_rooty() - h - 5}")
        top.configure(bg="#444444", highlightbackground="white", highlightthickness=1)

        def set_in(v):
            try: top.destroy()
            except Exception: pass
            def run():
                with self.clients_lock: self.monitor_cache.setdefault(self.current_ip, {})["input"] = v.upper()
                self.master_app.root.after(0, lambda: self._update_input_ui(self.current_ip, v.upper()))
                client.send_command("stop\r\n")
                time.sleep(0.1)
                resp = client.send_command(f"configuration:\r\nvideo input: {v.upper()}\r\n\r\n")
                if resp and resp.strip().startswith("1"): client.send_command(f"configuration:\r\nvideo input: {v.lower()}\r\n\r\n")
                client.send_command("preview: enable: true\r\n")
            threading.Thread(target=run, daemon=True).start()

        b = tk.Frame(top, bg="#444444"); b.pack(fill="both", expand=True)
        ttk.Button(b, text="SDI", style="Mini.Dark.TButton", command=lambda: set_in("SDI")).grid(row=0, column=0, padx=4, pady=10)
        ttk.Button(b, text="HDMI", style="Mini.Dark.TButton", command=lambda: set_in("HDMI")).grid(row=0, column=1, padx=4, pady=10)
        ttk.Button(b, text="x", style="Mini.Danger.TButton", command=top.destroy).grid(row=0, column=2, padx=4, pady=10)

    def _update_input_ui(self, ip: str, val: str):
        if self.tree.exists(ip):
            vals = list(self.tree.item(ip)["values"])
            while len(vals) < 9: vals.append("-")
            vals[3] = val
            self.tree.item(ip, values=tuple(vals))

    def format_ssd_dialog(self):
        sel = self.tree.selection()
        if not sel: return
        ip = str(self.tree.item(sel[0])["values"][1])
        with self.clients_lock: client = self.clients.get(ip)
        if not client or not client.connected: return
        sc = simpledialog.askstring("Format SSD", "Slot (active / 1 / 2):", initialvalue="active")
        if not sc or sc.strip().lower() not in ("active", "1", "2"): return
        fs = simpledialog.askstring("Format SSD", "Filesystem (exfat / hfs):", initialvalue="exfat")
        if not fs or fs.strip().lower() not in ("exfat", "hfs"): return
        if messagebox.askyesno("⚠️ DANGER", f"Format SSD pada {client.name}? Semua data akan HILANG!"):
            if simpledialog.askstring("Konfirmasi", "Ketik FORMAT untuk eksekusi:") == "FORMAT":
                self.log(f"... Formatting started...")
                self._busy_inc()
                threading.Thread(target=self._format_worker, args=(client, sc.strip().lower(), fs.strip().lower()), daemon=True).start()

    def _format_worker(self, client: HyperDeckClient, slot: str, fs: str):
        try:
            if slot in ("1", "2"): client.send_command(f"slot select: slot id: {slot}")
            prep = client.send_command(f"format: prepare: {fs}")
            token = None
            if prep:
                for line in prep.splitlines():
                    m = re.search(r"format ready:\s*([A-Za-z0-9]+)", line, flags=re.IGNORECASE)
                    if m: token = m.group(1); break
            if token:
                conf = client.send_command(f"format: confirm: {token}")
                if conf and "200" in conf:
                    self.log(f"[{client.name}] FORMAT SUCCESS")
                    return
            self.log(f"... FORMAT FAILED")
        except Exception as e: self.log(f"[{client.name}] FORMAT ERR: {e}")
        finally: self._busy_dec()

    def send_single(self, cmd: str):
        if not self.current_ip: return
        with self.clients_lock: client = self.clients.get(self.current_ip)
        if client and client.connected: threading.Thread(target=self._send_worker, args=(client, cmd), daemon=True).start()

    def send_group(self, cmd: str):
        clients = self._clients_in_active_group()
        for c in clients: threading.Thread(target=self._send_worker, args=(c, cmd), daemon=True).start()

    def _send_worker(self, client: HyperDeckClient, cmd: str):
        resp = client.send_command(cmd)
        self.log(f"[{client.name}] {'OK' if resp else 'ERR'}")

    def save_config(self, filepath: str):
        devs = []
        for c in self.tree.get_children():
            v = self.tree.item(c)["values"]
            devs.append({"name": v[0], "ip": v[1], "group": int(self.device_groups.get(v[1], 0))})
        try:
            with open(filepath, "w") as f: json.dump({"groups": self.group_names, "active_group": self.active_group, "devices": devs}, f, indent=2)
        except Exception: pass

    def load_config(self, filepath: str):
        if not os.path.exists(filepath): return
        try:
            with open(filepath, "r") as f: d = json.load(f)
            self.group_names = d.get("groups", self.group_names)
            self.active_group = d.get("active_group", 1)
            for item in d.get("devices", []):
                self.start_connect(item["ip"], item["name"], item.get("group", 0))
            self.master_app.root.after(0, self._update_group_buttons)
        except Exception: pass

    def _monitor_loop(self):
        cycle = 0
        while self.is_monitoring:
            cycle += 1
            if self.is_busy(): time.sleep(0.2); continue
            with self.clients_lock: ips = list(self.clients.keys())
            any_rec = False
            for ip in ips:
                with self.clients_lock: client = self.clients.get(ip)
                if not client: continue
                if client.connected:
                    d = client.get_transport_info_non_blocking()
                    if d:
                        if d.get("status") == "record": any_rec = True
                        if cycle % 15 == 0:
                            inp = client.get_config_input()
                            if inp != "?": self.monitor_cache.setdefault(ip, {})["input"] = inp
                        d["input"] = self.monitor_cache.get(ip, {}).get("input", "?")
                        
                        now = time.time()
                        if now >= self.monitor_cache.get(ip, {}).get("next_slot_poll", 0):
                            slots = client.get_slot_info_non_blocking()
                            if slots:
                                s_id = str(d.get("slot", "")).strip()
                                sec = slots.get(int(s_id)) if s_id.isdigit() else list(slots.values())[0]
                                d["ssd_left"] = ("🔴 " if sec <= 180 else "🟢 ") + format_hms(sec)
                            else: d["ssd_left"] = "NO DISK"
                            self.monitor_cache.setdefault(ip, {})["next_slot_poll"] = now + (1.0 if any_rec else 4.0)
                            self.monitor_cache[ip]["ssd_left"] = d["ssd_left"]
                        else: d["ssd_left"] = self.monitor_cache.get(ip, {}).get("ssd_left", "--")
                        
                        self.master_app.root.after(0, self.update_row, ip, d)
                else:
                    now = time.time()
                    if now >= self._reconnect_next.get(ip, 0):
                        self._reconnect_next[ip] = now + 5.0
                        threading.Thread(target=self._reconnect_silent, args=(ip,), daemon=True).start()
            time.sleep(0.25 if any_rec else REFRESH_RATE)

    def update_row(self, ip: str, d: dict):
        if self.tree.exists(ip):
            st = d.get("status", "")
            st_text = "🔴 REC" if st == "record" else "▶ PLAY" if st == "play" else "⏹ STOP"
            with self.clients_lock: name = self.clients[ip].name if ip in self.clients else "-"
            self.tree.item(ip, values=(name, ip, st_text, d.get("input", "?"), d.get("slot", ""), d.get("timecode", ""), d.get("ssd_left", "--"), d.get("clip", ""), self._gid_text(self.device_groups.get(ip, 0))))
            if ip == self.current_ip:
                asl = d.get("slot", "0")
                self.btn_widgets["ssd1"].config(image=self.icons_active["ssd1"] if asl == "1" else self.icons_up["ssd1"])
                self.btn_widgets["ssd2"].config(image=self.icons_active["ssd2"] if asl == "2" else self.icons_up["ssd2"])


# ==============================================================================
# 3. UI MODULE: FOR-A FA-9520 CONTROL TAB (DARK & PRO SLIDER LAYOUT)
# ==============================================================================
class FA9520Tab(tk.Frame):
    def __init__(self, parent, master_app):
        super().__init__(parent, bg="#1a1a1a")
        self.master_app = master_app
        
        self.load_device_config()
        self.selected_device = tk.StringVar(value=list(self.devices.keys())[0] if self.devices else "")
        self.fs_var = tk.IntVar(value=1)
        self.update_fs_urls()
        
        self.is_dragging = False
        self.last_command_time = 0
        self.controls = {}
        self.status_labels = {}
        
        self.setup_ui()
        self.poll_device()

    def load_device_config(self):
        self.config_file = "fa9520_devices.json"
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f: self.devices = json.load(f)
            except: self.devices = {"UNIT 1 (DEFAULT)": "192.168.0.10"}
        else:
            self.devices = {"UNIT 1 (DEFAULT)": "192.168.0.10"}
            self.save_device_config()

    def save_device_config(self):
        with open(self.config_file, "w") as f: json.dump(self.devices, f, indent=4)

    def update_fs_urls(self):
        ip = self.devices.get(self.selected_device.get(), "192.168.0.10") if self.devices else "192.168.0.10"
        fs = self.fs_var.get()
        self.url_post = f"http://{ip}/post_video2.cgi?fs={fs}"
        self.url_get = f"http://{ip}/video_param.cgi?fs={fs}"
        self.url_status = f"http://{ip}/refreshstatus2.cgi?fs={fs}"

    def setup_ui(self):
        top_frame = tk.Frame(self, pady=12, bg="#1a1a1a")
        top_frame.pack(fill='x')
        
        tk.Label(top_frame, text="Device: ", font=("Arial", 11, "bold"), bg="#1a1a1a", fg="white").pack(side='left', padx=10)
        self.device_combo = ttk.Combobox(top_frame, textvariable=self.selected_device, state="readonly", width=18, font=("Arial", 10))
        self.device_combo.pack(side='left', padx=2)
        self.device_combo['values'] = list(self.devices.keys())
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_change)
        
        ttk.Button(top_frame, text="⚙️ Manage", style="Dark.TButton", command=self.open_device_manager).pack(side='left', padx=10)
        
        tk.Label(top_frame, text=" |  Context: ", font=("Arial", 11, "bold"), bg="#1a1a1a", fg="white").pack(side='left', padx=5)
        
        # Tombol FS1 & FS2 berukuran besar, kokoh, dan menyala Hijau Kekuningan (#ccff00) saat aktif
        self.btn_fs1 = tk.Button(top_frame, text="FS 1", font=("Arial", 12, "bold"), bg="#333333", fg="white", bd=1, relief="raised", padx=18, pady=4, activebackground="#ccff00", command=lambda: self.select_fs(1))
        self.btn_fs1.pack(side='left', padx=4)
        
        self.btn_fs2 = tk.Button(top_frame, text="FS 2", font=("Arial", 12, "bold"), bg="#333333", fg="white", bd=1, relief="raised", padx=18, pady=4, activebackground="#ccff00", command=lambda: self.select_fs(2))
        self.btn_fs2.pack(side='left', padx=4)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True, padx=5, pady=5)
        
        # SUB TAB 0: DEVICE STATUS
        tab_status = tk.Frame(self.notebook, bg="#1a1a1a")
        self.notebook.add(tab_status, text="Device Status")
        
        top_status = tk.Frame(tab_status, bg="#1a1a1a")
        top_status.pack(fill='x', padx=5, pady=5)
        
        g_vid = tk.LabelFrame(top_status, text="VIDEO INPUT STATUS", bg="#1a1a1a", fg="white", font=("Arial", 8, "bold"))
        g_vid.pack(side='left', fill='both', expand=True, padx=3)
        for k in ["sdiVideoIn1", "sdiVideoIn2"]:
            f = tk.Frame(g_vid, bg="#1a1a1a")
            f.pack(fill='x', padx=5, pady=1)
            tk.Label(f, text=k.upper(), width=14, anchor='w', font=("Arial", 8, "bold"), bg="#1a1a1a", fg="#aaaaaa").pack(side='left')
            lbl = tk.Label(f, text="---", font=("Arial", 8, "bold"), bg="#1a1a1a", fg="white")
            lbl.pack(side='left'); self.status_labels[k] = lbl

        g_unit = tk.LabelFrame(top_status, text="UNIT STATUS", bg="#1a1a1a", fg="white", font=("Arial", 8, "bold"))
        g_unit.pack(side='left', fill='both', expand=True, padx=3)
        for k in ["fan1Status", "power1Status", "power2Status"]:
            f = tk.Frame(g_unit, bg="#1a1a1a")
            f.pack(fill='x', padx=5, pady=1)
            tk.Label(f, text=k.upper(), width=14, anchor='w', font=("Arial", 8, "bold"), bg="#1a1a1a", fg="#aaaaaa").pack(side='left')
            lbl = tk.Label(f, text="---", font=("Arial", 8, "bold"), bg="#1a1a1a", fg="white")
            lbl.pack(side='left'); self.status_labels[k] = lbl

        # Audio Grids
        b_status = tk.Frame(tab_status, bg="#1a1a1a")
        b_status.pack(fill='both', expand=True, padx=5, pady=5)
        for grp in ["sdiAudioIn1", "sdiAudioIn2"]:
            g_aud = tk.LabelFrame(b_status, text=grp.upper(), bg="#1a1a1a", fg="white", font=("Arial", 8, "bold"))
            g_aud.pack(side='left', fill='both', expand=True, padx=3, pady=2)
            gf = tk.Frame(g_aud, bg="#1a1a1a"); gf.pack(padx=5, pady=5)
            for i in range(16):
                key = f"{grp}_{i}"
                tk.Label(gf, text=f"CH {i+1:02d}:", font=("Arial", 8, "bold"), width=6, anchor='w', bg="#1a1a1a", fg="#aaaaaa").grid(row=i%8, column=(i//8)*2, sticky='w', padx=2, pady=1)
                lbl = tk.Label(gf, text="---", font=("Arial", 8, "bold"), width=7, anchor='w', bg="#1a1a1a", fg="white")
                lbl.grid(row=i%8, column=(i//8)*2+1, sticky='w', padx=(0,8), pady=1)
                self.status_labels[key] = lbl

        # SUB TAB 1: PROC AMP
        tab_proc = tk.Frame(self.notebook, bg="#1a1a1a")
        self.notebook.add(tab_proc, text="Proc Amp")
        self.create_control(tab_proc, "Video Level", "proc:vlvl", 0, 2000, 1000)
        self.create_control(tab_proc, "Chroma Level", "proc:clvl", 0, 2000, 1000)
        self.create_control(tab_proc, "Setup/Black", "proc:blvl", -1000, 1000, 0)
        self.create_control(tab_proc, "HUE", "proc:hue", -1800, 1800, 0)

        # SUB TAB 2: COLOR CORRECTION
        tab_rgb = tk.Frame(self.notebook, bg="#1a1a1a")
        self.notebook.add(tab_rgb, text="Color Correction")
        colors = {'r': {'bg': '#cc3333', 'fg': 'white'}, 'g': {'bg': '#339933', 'fg': 'white'}, 'b': {'bg': '#3366cc', 'fg': 'white'}}
        mapping = {
            "WHITE": {"r": "cc:wlvlr", "g": "cc:wlvlg", "b": "cc:wlvlb"},
            "BLACK": {"r": "cc:blvlr", "g": "cc:blvlg", "b": "cc:blvlb"},
            "GAMMA": {"r": "cc:glvlr", "g": "cc:glvlg", "b": "cc:glvlb"}
        }
        for sec, keys in mapping.items():
            # REVISI: Mengubah judul menjadi Huruf Kapital Penuh dan ditempatkan ke atas tiap kelompok slider
            tk.Label(tab_rgb, text=f"{sec} LEVEL", font=("Arial", 11, "bold"), bg="#1a1a1a", fg="#ffd200").pack(anchor='w', padx=20, pady=(12, 2))
            self.create_control(tab_rgb, "Red", keys["r"], 0, 2000, 1000, color_scheme=colors['r'])
            self.create_control(tab_rgb, "Green", keys["g"], 0, 2000, 1000, color_scheme=colors['g'])
            self.create_control(tab_rgb, "Blue", keys["b"], 0, 2000, 1000, color_scheme=colors['b'])

        # SUB TAB 3: SEPIA & MODE
        tab_sepia = tk.Frame(self.notebook, bg="#1a1a1a")
        self.notebook.add(tab_sepia, text="Sepia & Mode")
        
        # REVISI: Membuat kontainer khusus agar Correction Mode & Curve Mode merapat ke KIRI atas fader
        modes_container = tk.Frame(tab_sepia, bg="#1a1a1a")
        modes_container.pack(fill='x', padx=20, pady=10)
        
        cm_frame = tk.Frame(modes_container, bg="#1a1a1a")
        cm_frame.pack(side='left', padx=(0, 45), anchor='n')
        tk.Label(cm_frame, text="CORRECTION MODE", font=("Arial", 11, "bold"), bg="#1a1a1a", fg="#ffd200").pack(anchor='w', pady=(0, 4))
        self.mode_var = tk.IntVar(value=0)
        for val, text in [(0, "Balance"), (1, "Differential"), (2, "Sepia")]:
            tk.Radiobutton(cm_frame, text=text, variable=self.mode_var, value=val, command=lambda: self.send_command("cc:mode", self.mode_var.get(), False), bg="#1a1a1a", fg="white", selectcolor="#333333", font=("Arial", 10, "bold")).pack(anchor='w', pady=2)
        
        curve_frame = tk.Frame(modes_container, bg="#1a1a1a")
        curve_frame.pack(side='left', anchor='n')
        tk.Label(curve_frame, text="CURVE MODE", font=("Arial", 11, "bold"), bg="#1a1a1a", fg="#ffd200").pack(anchor='w', pady=(0, 4))
        self.curve_var = tk.IntVar(value=0)
        for val, text in [(0, "Center"), (1, "Black"), (2, "White")]:
            tk.Radiobutton(curve_frame, text=text, variable=self.curve_var, value=val, command=lambda: self.send_command("cc:curve", self.curve_var.get(), False), bg="#1a1a1a", fg="white", selectcolor="#333333", font=("Arial", 10, "bold")).pack(anchor='w', pady=2)
        
        # Penyekat estetis antara menu mode dan fader sepia
        tk.Frame(tab_sepia, bg="#333333", height=1).pack(fill='x', padx=20, pady=12)
        
        self.create_control(tab_sepia, "Sepia Level", "cc:slvl", 0, 1000, 25, is_sepia=True)
        self.create_control(tab_sepia, "Sepia Color", "cc:sphs", -2000, 2000, -160, is_sepia=True)

        # Set warna status tombol aktif di awal startup
        self.master_app.root.after(20, lambda: self.select_fs(self.fs_var.get(), trigger_poll=False))

    def select_fs(self, fs_num, trigger_poll=True):
        """Mengatur perubahan warna visual tombol FS secara dinamis (Anti-Lag)"""
        self.fs_var.set(fs_num)
        if fs_num == 1:
            self.btn_fs1.config(bg="#ccff00", fg="black") # Menyala Hijau Kekuningan
            self.btn_fs2.config(bg="#333333", fg="white")
        else:
            self.btn_fs1.config(bg="#333333", fg="white")
            self.btn_fs2.config(bg="#ccff00", fg="black") # Menyala Hijau Kekuningan
        
        if trigger_poll:
            self.on_fs_change()

    def create_control(self, parent, label, key, min_v, max_v, default, is_sepia=False, color_scheme=None):
        # Layout diubah menjadi merapat ke KIRI (side=left) agar slider dekat dengan teks deskripsi
        frame = tk.Frame(parent, bg="#1a1a1a")
        frame.pack(fill='x', pady=5, padx=20) # Ruang bernafas vertikal ditambah
        
        def on_change(val):
            disp = float(val) if is_sepia else float(val)/10.0
            val_label.config(text=f"{disp:.1f}")

        # Label diperbesar ukuran tulisannya
        tk.Label(frame, text=label, width=16, anchor='w', bg="#1a1a1a", fg="white", font=("Arial", 11, "bold")).pack(side='left', padx=(0, 15))
        
        # Slider / Fader dipertebal (width=20) dan diperpanjang (length=320) untuk look profesional
        s_opts = {
            'from_': min_v, 'to': max_v, 'orient': 'horizontal', 
            'length': 320, 'width': 20, 
            'command': on_change, 'bg': '#1a1a1a', 'fg': 'white', 
            'highlightthickness': 0, 'troughcolor': '#333333', 
            'activebackground': '#444444', 'showvalue': 0
        }
        if color_scheme: s_opts.update({'troughcolor': color_scheme['bg']})
        slider = tk.Scale(frame, **s_opts); slider.set(default); slider.pack(side='left', padx=10)
        
        # Angka indikator green-consolas diperbesar nilainya
        val_label = tk.Label(frame, text="0.0", width=7, bg="#1a1a1a", fg="#00ff00", font=("Consolas", 13, "bold"), anchor='w')
        val_label.pack(side='left', padx=10)

        # Isi teks inisialisasi di awal agar tidak kosong sebelum digeser
        disp_init = float(default) if is_sepia else float(default)/10.0
        val_label.config(text=f"{disp_init:.1f}")

        slider.bind("<ButtonPress-1>", lambda e: setattr(self, 'is_dragging', True))
        slider.bind("<ButtonRelease-1>", lambda e: [setattr(self, 'is_dragging', False), self.send_command(key, slider.get(), is_sepia)])
        
        ttk.Button(frame, text="Unity", style="Mini.Dark.TButton", width=6, command=lambda: [slider.set(default), self.send_command(key, default, is_sepia)]).pack(side='left', padx=5)
        self.controls[key] = {"widget": slider, "label": val_label, "is_sepia": is_sepia}
        return slider

    def on_fs_change(self):
        self.is_dragging = True; self.update_fs_urls(); self.poll_device(); self.is_dragging = False

    def on_device_change(self, event):
        self.is_dragging = True; self.update_fs_urls()
        for lbl in self.status_labels.values(): lbl.config(text="SWITCHING...", fg="orange")
        self.poll_device(); self.is_dragging = False

    def open_device_manager(self):
        manager = tk.Toplevel(self)
        manager.title("Device Manager"); manager.geometry("350x400"); manager.grab_set()
        manager.configure(bg="#222222")
        listbox = tk.Listbox(manager, font=("Consolas", 10), bg="#333333", fg="white", relief="flat"); listbox.pack(fill='both', expand=True, padx=10, pady=5)
        
        f = tk.Frame(manager, bg="#222222"); f.pack(fill='x', padx=10, pady=5)
        tk.Label(f, text="Name:", bg="#222222", fg="white").grid(row=0, column=0, sticky='w')
        en = tk.Entry(f, bg="#444444", fg="white", relief="flat"); en.grid(row=0, column=1, sticky='we', padx=5, pady=2)
        tk.Label(f, text="IP Address:", bg="#222222", fg="white").grid(row=1, column=0, sticky='w')
        eip = tk.Entry(f, bg="#444444", fg="white", relief="flat"); eip.grid(row=1, column=1, sticky='we', padx=5, pady=2)
        f.grid_columnconfigure(1, weight=1)

        def ref():
            listbox.delete(0, tk.END)
            for name, ip in self.devices.items(): listbox.insert(tk.END, f"{name} -> {ip}")
        ref()
        listbox.bind("<<ListboxSelect>>", lambda e: [en.delete(0, tk.END), en.insert(0, list(self.devices.keys())[listbox.curselection()[0]]), eip.delete(0, tk.END), eip.insert(0, list(self.devices.values())[listbox.curselection()[0]])] if listbox.curselection() else None)

        def save():
            n, ip = en.get().strip().upper(), eip.get().strip()
            if n and ip:
                if listbox.curselection():
                    old = list(self.devices.keys())[listbox.curselection()[0]]
                    if old != n: del self.devices[old]
                self.devices[n] = ip; self.save_device_config(); ref()
                self.device_combo['values'] = list(self.devices.keys())
                if self.selected_device.get() not in self.devices: self.selected_device.set(n)
                self.update_fs_urls()
        
        def delete():
            if listbox.curselection() and len(self.devices) > 1:
                n = list(self.devices.keys())[listbox.curselection()[0]]
                if messagebox.askyesno("Confirm", f"Delete {n}?"):
                    del self.devices[n]; self.save_device_config(); ref()
                    self.device_combo['values'] = list(self.devices.keys())
                    self.selected_device.set(list(self.devices.keys())[0]); self.update_fs_urls()

        bf = tk.Frame(manager, bg="#222222"); bf.pack(fill='x', padx=10, pady=10)
        tk.Button(bf, text="Add / Update", command=save, bg="#007acc", fg="white", relief="flat", width=12).pack(side='left', padx=5)
        tk.Button(bf, text="Delete", command=delete, bg="#cc0000", fg="white", relief="flat", width=12).pack(side='right', padx=5)

    def send_command(self, key, val, is_sepia=False):
        self.last_command_time = time.time()
        real_val = int(val) * 10 if is_sepia else int(val)
        payload = {key: str(real_val)}
        threading.Thread(target=lambda: requests.post(self.url_post, data=payload, timeout=2), daemon=True).start()

    def poll_device(self):
        try:
            if not self.is_dragging and (time.time() - self.last_command_time > 2.0):
                r = requests.get(self.url_get, timeout=2)
                if r.status_code == 200:
                    data = r.json()
                    map_get_to_post = {"videoLevel": "proc:vlvl", "chromaLevel": "proc:clvl", "blackLevel": "proc:blvl", "hue": "proc:hue", "whiteLevelRed": "cc:wlvlr", "whiteLevelGreen": "cc:wlvlg", "whiteLevelBlue": "cc:wlvlb", "blackLevelRed": "cc:blvlr", "blackLevelGreen": "cc:blvlg", "blackLevelBlue": "cc:blvlb", "gammaLevelRed": "cc:glvlr", "gammaLevelGreen": "cc:glvlg", "gammaLevelBlue": "cc:glvlb", "sepiaLevel": "cc:slvl", "sepiaColor": "cc:sphs"}
                    for jk, pk in map_get_to_post.items():
                        if jk in data and pk in self.controls:
                            val = int(data[jk])
                            ui_val = val // 10 if self.controls[pk]["is_sepia"] else val
                            if self.controls[pk]["widget"].get() != ui_val:
                                self.controls[pk]["widget"].set(ui_val)
                                disp = float(ui_val) if self.controls[pk]["is_sepia"] else float(ui_val)/10.0
                                self.controls[pk]["label"].config(text=f"{disp:.1f}")
            
            r_stat = requests.get(self.url_status, timeout=2)
            if r_stat.status_code == 200:
                stat = r_stat.json()
                for key, lbl in self.status_labels.items():
                    if "_" in key:
                        group, idx = key.split("_")
                        arr = stat.get(group, [])
                        val = arr[int(idx)] if len(arr) > int(idx) else "---"
                    else:
                        val = stat.get(key, "---")
                    
                    v_up = str(val).upper()
                    lbl.config(text=v_up, fg="red" if v_up in ["LOSS", "ABNORMAL", "SILENCE"] else "green")
        except: pass
        self.master_app.root.after(3000, self.poll_device)


# ==============================================================================
# 4. MAIN CONTAINER ENGINE (LAUNCHER APPLICATION)
# ==============================================================================
class MasterControlApp:
    def __init__(self, root):
        self.root = root
        self.root.title("BROADCAST SUPPORT CONTROL PANEL")
        self.root.geometry("1120x860")
        self.root.configure(bg="#1a1a1a")

        if os.path.exists(resource_path("logo.ico")):
            try: root.iconbitmap(resource_path("logo.ico"))
            except: pass

        # --- THEMES & COMPONENT STYLES ---
        style = ttk.Style()
        style.theme_use("clam")
        
        style.configure("TFrame", background="#1a1a1a")
        style.configure("TLabel", background="#1a1a1a", foreground="white")
        style.configure("TNotebook", background="#1a1a1a", borderwidth=0)
        style.configure("TNotebook.Tab", background="#333333", foreground="white", font=("Arial", 10, "bold"), padding=[18, 6])
        style.map("TNotebook.Tab", background=[("selected", "#007acc")], foreground=[("selected", "white")])
        style.configure("Treeview", background="#333333", foreground="white", fieldbackground="#333333", font=("Consolas", 10))
        style.map("Treeview", background=[("selected", "#007acc")], foreground=[("selected", "white")])
        style.configure("Treeview.Heading", background="#222222", foreground="white", font=("Arial", 9, "bold"))

        style.configure("Dark.TButton", background="#444444", foreground="white", padding=(10, 6), font=("Arial", 9, "bold"))
        style.map("Dark.TButton", background=[("active", "#555555"), ("pressed", "#333333")])
        style.configure("Big.Dark.TButton", background="#444444", foreground="white", padding=(10, 8), font=("Arial", 11, "bold"))
        style.map("Big.Dark.TButton", background=[("active", "#555555"), ("pressed", "#333333")])
        style.configure("Danger.TButton", background="#cc0000", foreground="white", padding=(10, 8), font=("Arial", 11, "bold"))
        style.map("Danger.TButton", background=[("active", "#e00000"), ("pressed", "#a80000")])
        style.configure("Accent.TButton", background="#007acc", foreground="white", padding=(10, 6), font=("Arial", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#1294ff"), ("pressed", "#005b99")])
        
        style.configure("Group.TButton", background="#2b2b2b", foreground="white", padding=(8, 4), font=("Arial", 9, "bold"))
        style.configure("GroupActive.TButton", background="#3a3a3a", foreground="white", padding=(8, 4), font=("Arial", 9, "bold"))
        style.configure("Mini.Dark.TButton", background="#333333", foreground="white", padding=(6, 2), font=("Arial", 9, "bold"))
        style.map("Mini.Dark.TButton", background=[("active", "#444444"), ("pressed", "#222222")])
        style.configure("Mini.Danger.TButton", background="#cc0000", foreground="white", padding=(6, 2), font=("Arial", 9, "bold"))

        # --- BANNER HEADER ---
        header_frame = tk.Frame(root, bg="#111111", height=55)
        header_frame.pack(fill='x', side='top')
        
        if os.path.exists(resource_path("logo.png")):
            try:
                img = Image.open(resource_path("logo.png")).resize((65, 40), Image.Resampling.LANCZOS)
                self.logo_img = ImageTk.PhotoImage(img)
                tk.Label(header_frame, image=self.logo_img, bg="#111111").pack(side='left', padx=15, pady=6)
            except: pass

        tk.Label(header_frame, text="BROADCAST SUPPORT CONTROL INTERFACE", font=("Helvetica Neue", 13, "bold"), fg="#f0c040", bg="#111111").pack(side='left', padx=5, pady=15)

        # --- NOTEBOOK TABS ---
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=8, pady=5)

        self.tab_hyperdeck = HyperDeckTab(self.notebook, self)
        self.notebook.add(self.tab_hyperdeck, text="   BMD HYPERDECK SYSTEM   ")

        self.tab_fa9520 = FA9520Tab(self.notebook, self)
        self.notebook.add(self.tab_fa9520, text="   FOR-A FA-9520 FRAMESYNC   ")

        # --- TEXT LOGGER ---
        self.log_text = tk.Text(root, height=4, state="disabled", font=("Consolas", 8), bg="#111111", fg="#999999", bd=0, highlightthickness=0)
        self.log_text.pack(fill="x", side="bottom", padx=8, pady=5)

        self.append_log("--- SYSTEM MASTER ENGINE STARTUP SUCCESSFUL ---")

        # --- WATERMARK ---
        # Menggunakan warna teks #444444 dengan background #111111 (sama dengan bg log/root) 
        # untuk menciptakan efek "transparan" dan menyatu dengan UI.
        self.watermark = tk.Label(
            root, 
            text="created by dhaniharianto", 
            font=("Arial", 8, "italic"), 
            bg="#111111", 
            fg="#444444"
        )
        # place() mengatur posisi widget secara absolut/relatif, tidak akan terganggu oleh pack() widget lain
        self.watermark.place(relx=1.0, rely=1.0, anchor="se", x=-12, y=-8)

    def append_log(self, msg: str):
        try:
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
        except Exception: pass


if __name__ == "__main__":
    main_window = tk.Tk()
    app = MasterControlApp(main_window)
    main_window.mainloop()