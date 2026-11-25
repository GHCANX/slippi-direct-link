#!/usr/bin/env python3
"""
slippi_direct_link.py - Unified cross-platform Slippi Direct Link handler & installer.

Supports both Slippi Dolphin and MeleeUnlocked on Linux and Windows.
Zero external dependencies (Python 3 standard library + ctypes only).

Usage:
  slippi-direct-link --install [optional_dolphin_user_dir ...]
  slippi-direct-link "slippi://connect?code=FOX-123"
  slippi-direct-link FOX#123
"""

import configparser
import ctypes
import glob
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import urllib.parse
import uuid

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

PIPE_NAME = "slippi_direct_link"
PIPE_PORT_INDEX = 3  # Port 4 (0-indexed)

CONNECT_CODE_REGEX = re.compile(r"^[A-Z0-9]{1,7}#[0-9]{1,4}$")

# Fullwidth Shift-JIS mapping for Melee
SJIS_TABLE = {
    "A": (0x82, 0x60), "B": (0x82, 0x61), "C": (0x82, 0x62), "D": (0x82, 0x63),
    "E": (0x82, 0x64), "F": (0x82, 0x65), "G": (0x82, 0x66), "H": (0x82, 0x67),
    "I": (0x82, 0x68), "J": (0x82, 0x69), "K": (0x82, 0x6A), "L": (0x82, 0x6B),
    "M": (0x82, 0x6C), "N": (0x82, 0x6D), "O": (0x82, 0x6E), "P": (0x82, 0x6F),
    "Q": (0x82, 0x70), "R": (0x82, 0x71), "S": (0x82, 0x72), "T": (0x82, 0x73),
    "U": (0x82, 0x74), "V": (0x82, 0x75), "W": (0x82, 0x76), "X": (0x82, 0x77),
    "Y": (0x82, 0x78), "Z": (0x82, 0x79),
    "0": (0x82, 0x4F), "1": (0x82, 0x50), "2": (0x82, 0x51), "3": (0x82, 0x52),
    "4": (0x82, 0x53), "5": (0x82, 0x54), "6": (0x82, 0x55), "7": (0x82, 0x56),
    "8": (0x82, 0x57), "9": (0x82, 0x58),
    "#": (0x81, 0x94),
}

# Offsets relative to GameCube RAM Base (0x80000000)
RAM_OFFSET_DIRECT_CODE_PRIMARY = 0x004A0740
RAM_OFFSET_DIRECT_CODE_SECONDARY = 0x004A0760
RAM_OFFSET_MENU_FLOW_DATA = 0x004A04F0
RAM_OFFSET_INJ_CHECK_AUTOFILL = 0x0023CCA4
RAM_OFFSET_NAME_ENTRY_MODE = 0x004D6643
RAM_OFFSET_NAME_ENTRY_MGR_PTR = 0x004D6C08
RAM_OFFSET_PLAYER_PORT = 0x004D6C0C
RAM_OFFSET_CSS_PLAYER_INDEX = 0x004D6CF0
RAM_OFFSET_PLAYER_INPUTS_BASE = 0x00489C30
RAM_OFFSET_HSD_PAD_MASTER = 0x004C1FAC

# GameCube controller button bitmasks
PAD_TRIGGER_Z = 0x0010
PAD_BUTTON_START = 0x1000
PAD_BUTTON_A = 0x0100

# Windows virtual key codes for Port 4 keyboard bindings
WIN_VK_MAP = {
    "START": 0x7C,  # VK_F13
    "Z": 0x7D,      # VK_F14
    "A": 0x7E,      # VK_F15
}


def normalize_connect_code(raw: str):
    if not raw:
        return None
    cleaned = raw.strip().replace("-", "#").upper()
    if len(cleaned) > 8 or "#" not in cleaned:
        return None
    if not CONNECT_CODE_REGEX.match(cleaned):
        return None
    return cleaned


def encode_connect_code_3stride(code: str) -> bytes:
    buf = bytearray(24)
    for i, ch in enumerate(code[:8]):
        if ch in SJIS_TABLE:
            b0, b1 = SJIS_TABLE[ch]
            buf[i * 3] = b0
            buf[i * 3 + 1] = b1
            buf[i * 3 + 2] = 0x00
    return bytes(buf)


def to_fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else c for c in s)


def get_dolphin_user_dirs(extra_dirs=None):
    dirs = []
    if extra_dirs:
        dirs.extend(extra_dirs)

    if IS_LINUX:
        candidates = [
            os.path.expanduser("~/.config/SlippiOnline"),
            os.path.expanduser("~/.config/slippi-dolphin/netplay-beta"),
            os.path.expanduser("~/.config/slippi-dolphin/netplay"),
        ]
        # Auto-discover portable MeleeUnlocked folders
        for pattern in [
            os.path.expanduser("~/AppImages/MeleeUnlocked*/User"),
            os.path.expanduser("~/MeleeUnlocked*/User"),
            os.path.expanduser("~/Downloads/MeleeUnlocked*/User"),
        ]:
            candidates.extend(glob.glob(pattern))
    elif IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(appdata, "Slippi Launcher", "netplay", "User"),
            os.path.join(appdata, "Slippi Launcher", "netplay-beta", "User"),
            os.path.join(appdata, "SlippiOnline"),
            os.path.join(localappdata, "SlippiOnline"),
            os.path.join(userprofile, "Documents", "Dolphin Emulator"),
        ]
        for pattern in [
            os.path.join(userprofile, "Desktop", "MeleeUnlocked*", "User"),
            os.path.join(userprofile, "Downloads", "MeleeUnlocked*", "User"),
            os.path.join(localappdata, "MeleeUnlocked*", "User"),
        ]:
            candidates.extend(glob.glob(pattern))
    else:
        candidates = []

    seen = set()
    result = []
    for d in dirs + candidates:
        if d and os.path.isdir(d) and d not in seen:
            seen.add(d)
            result.append(d)
    return result


def notify(title: str, message: str):
    if IS_LINUX:
        try:
            subprocess.run(["notify-send", "-a", "Slippi Direct Link", title, message], check=False)
        except Exception:
            pass
    else:
        print(f"[{title}] {message}")


# ---------------------------------------------------------------------------
# Process Memory Abstraction (Linux /proc/<pid>/mem & Windows kernel32)
# ---------------------------------------------------------------------------

class ProcessMemory:
    def __init__(self, pid: int):
        self.pid = pid
        self._mem_f = None
        self._handle = None

    def __enter__(self):
        if IS_LINUX:
            self._mem_f = open(f"/proc/{self.pid}/mem", "r+b", 0)
        elif IS_WINDOWS:
            PROCESS_VM_READ = 0x0010
            PROCESS_VM_WRITE = 0x0020
            PROCESS_VM_OPERATION = 0x0008
            PROCESS_QUERY_INFORMATION = 0x0400
            access = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION
            self._handle = ctypes.windll.kernel32.OpenProcess(access, False, self.pid)
            if not self._handle:
                raise OSError(f"OpenProcess failed for PID {self.pid}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._mem_f:
            self._mem_f.close()
        if self._handle and IS_WINDOWS:
            ctypes.windll.kernel32.CloseHandle(self._handle)

    def read(self, addr: int, size: int) -> bytes:
        if IS_LINUX:
            self._mem_f.seek(addr)
            return self._mem_f.read(size)
        elif IS_WINDOWS:
            buf = ctypes.create_string_buffer(size)
            bytes_read = ctypes.c_size_t(0)
            ok = ctypes.windll.kernel32.ReadProcessMemory(
                self._handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(bytes_read)
            )
            if not ok:
                return b""
            return buf.raw[:bytes_read.value]
        return b""

    def write(self, addr: int, data: bytes) -> bool:
        if IS_LINUX:
            self._mem_f.seek(addr)
            self._mem_f.write(data)
            return True
        elif IS_WINDOWS:
            bytes_written = ctypes.c_size_t(0)
            ok = ctypes.windll.kernel32.WriteProcessMemory(
                self._handle, ctypes.c_void_p(addr), data, len(data), ctypes.byref(bytes_written)
            )
            return bool(ok)
        return False


def find_dolphin_pids():
    pids = []
    if IS_LINUX:
        sigs = [b"dolphin-emu", b"AppRun.wrapped", b"Slippi_Online", b"Slippi_Netplay", b"MeleeUnlocked"]
        for proc_dir in glob.glob("/proc/[0-9]*"):
            try:
                pid = int(os.path.basename(proc_dir))
                cmdline_path = os.path.join(proc_dir, "cmdline")
                if not os.path.exists(cmdline_path):
                    continue
                with open(cmdline_path, "rb") as f:
                    cmd = f.read()
                    if any(sig in cmd for sig in sigs):
                        pids.append(pid)
            except (PermissionError, ProcessLookupError, ValueError):
                continue
    elif IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"], creationflags=0x08000000
            ).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split(",")]
                if len(parts) >= 2:
                    exe = parts[0].lower()
                    if any(k in exe for k in ("dolphin", "slippi", "meleeunlocked")):
                        if "launcher" not in exe:
                            try:
                                pids.append(int(parts[1]))
                            except ValueError:
                                pass
        except Exception:
            pass
    return pids


def find_melee_ram_bases(pid: int):
    ram_bases = []
    if IS_LINUX:
        maps_path = f"/proc/{pid}/maps"
        mem_path = f"/proc/{pid}/mem"
        if not os.path.exists(maps_path) or not os.path.exists(mem_path):
            return ram_bases
        try:
            with open(maps_path, "r") as maps_f:
                lines = maps_f.readlines()
            with open(mem_path, "rb", 0) as mem_f:
                for line in lines:
                    parts = line.split()
                    if len(parts) < 2 or "rw" not in parts[1]:
                        continue
                    s, e = [int(x, 16) for x in parts[0].split("-")]
                    size = e - s
                    if size in (0x2000000, 0x1800000, 0x4000000) or (0x1800000 <= size <= 0x10000000):
                        try:
                            mem_f.seek(s)
                            if mem_f.read(32).startswith(b"GALE01"):
                                ram_bases.append(s)
                        except (OSError, OverflowError):
                            continue
        except (PermissionError, ProcessLookupError, FileNotFoundError):
            pass
    elif IS_WINDOWS:
        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong),
                ("PartitionId", ctypes.c_ushort),
                ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_ulong),
                ("Protect", ctypes.c_ulong),
                ("Type", ctypes.c_ulong),
            ]

        MEM_COMMIT = 0x1000
        PAGE_READWRITE = 0x04
        try:
            with ProcessMemory(pid) as pm:
                mbi = MEMORY_BASIC_INFORMATION()
                addr = 0
                max_addr = 0x7FFFFFFFFFFF
                while addr < max_addr:
                    res = ctypes.windll.kernel32.VirtualQueryEx(
                        pm._handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
                    )
                    if res == 0:
                        break
                    if (
                        mbi.State == MEM_COMMIT
                        and mbi.Protect == PAGE_READWRITE
                        and (mbi.RegionSize in (0x2000000, 0x1800000, 0x4000000) or 0x1800000 <= mbi.RegionSize <= 0x10000000)
                    ):
                        hdr = pm.read(mbi.BaseAddress, 32)
                        if hdr.startswith(b"GALE01"):
                            ram_bases.append(mbi.BaseAddress)
                    next_addr = (mbi.BaseAddress or 0) + mbi.RegionSize
                    if next_addr <= addr:
                        break
                    addr = next_addr
        except Exception:
            pass
    return ram_bases


# ---------------------------------------------------------------------------
# Window Focusing (Linux Sway/X11 & Windows user32)
# ---------------------------------------------------------------------------

def focus_dolphin_window() -> bool:
    if IS_LINUX:
        # Try Sway first
        try:
            tree = json.loads(subprocess.check_output(["swaymsg", "-t", "get_tree"], timeout=1))

            def get_all_views(node):
                views = []
                if node.get("name") or node.get("window_properties"):
                    views.append(node)
                for c in node.get("nodes", []) + node.get("floating_nodes", []):
                    views.extend(get_all_views(c))
                return views

            target_con = None
            for v in get_all_views(tree):
                name = v.get("name") or ""
                wp = v.get("window_properties") or {}
                title = wp.get("title") or ""
                if name == "Dolphin" or title == "Dolphin":
                    target_con = v.get("id")
                    break
                if "GALE01" in title or "Super Smash Bros" in title:
                    target_con = v.get("id")
                    break

            if target_con:
                subprocess.run(["swaymsg", f"[con_id={target_con}] focus"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
        except Exception:
            pass

        # Fallback to wmctrl / xdotool on X11
        for cmd in (["wmctrl", "-a", "Dolphin"], ["xdotool", "search", "--name", "^Dolphin$", "windowactivate"]):
            try:
                if subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    return True
            except Exception:
                pass
        return False

    elif IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            target_hwnd = [None]

            def enum_cb(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length == 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if title == "Dolphin" or "GALE01" in title or "Super Smash Bros" in title:
                    target_hwnd[0] = hwnd
                    return False
                return True

            user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
            if target_hwnd[0]:
                user32.ShowWindow(target_hwnd[0], 9)  # SW_RESTORE
                user32.SetForegroundWindow(target_hwnd[0])
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Controller Input (Linux Dolphin Named Pipe & Windows SendInput)
# ---------------------------------------------------------------------------

def send_controller_button(button_name: str, hold_sec: float = 0.12) -> bool:
    if IS_LINUX:
        open_fds = []
        for user_dir in get_dolphin_user_dirs():
            pipe_path = os.path.join(user_dir, "Pipes", PIPE_NAME)
            if not os.path.exists(pipe_path):
                continue
            try:
                fd = os.open(pipe_path, os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, f"PRESS {button_name}\n".encode("ascii"))
                open_fds.append(fd)
            except OSError:
                continue

        if not open_fds:
            return False

        time.sleep(hold_sec)
        for fd in open_fds:
            try:
                os.write(fd, f"RELEASE {button_name}\n".encode("ascii"))
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return True

    elif IS_WINDOWS:
        vk = WIN_VK_MAP.get(button_name)
        if not vk:
            return False
        try:
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            time.sleep(hold_sec)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            return True
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Melee RAM Inspection & Injection
# ---------------------------------------------------------------------------

def is_in_name_entry(pm: ProcessMemory, ram_base: int) -> tuple:
    try:
        mode_byte = pm.read(ram_base + RAM_OFFSET_NAME_ENTRY_MODE, 1)
        if not mode_byte or mode_byte[0] != 1:
            return False, 0

        ne_mgr_bytes = pm.read(ram_base + RAM_OFFSET_NAME_ENTRY_MGR_PTR, 4)
        if len(ne_mgr_bytes) < 4:
            return False, 0
        ne_mgr = struct.unpack(">I", ne_mgr_bytes)[0]
        if not (0x80000000 <= ne_mgr <= 0x81800000):
            return False, 0

        r28_bytes = pm.read(ram_base + (ne_mgr - 0x80000000 + 0x2C), 4)
        if len(r28_bytes) < 4:
            return False, 0
        r28 = struct.unpack(">I", r28_bytes)[0]
        if not (0x80000000 <= r28 <= 0x81800000):
            return False, 0

        return True, r28
    except Exception:
        return False, 0


def pulse_ram_buttons(pm: ProcessMemory, ram_base: int, buttons: int, frames: int = 8, target_ports=(0, 1)):
    try:
        btn_u32 = buttons & 0xFFFFFFFF
        for _ in range(frames):
            for p in target_ports:
                in_off = RAM_OFFSET_PLAYER_INPUTS_BASE + (p * 0x30)
                for f_off in (0x08, 0x0C, 0x20, 0x24):
                    raw = pm.read(ram_base + in_off + f_off, 4)
                    if len(raw) == 4:
                        curr = struct.unpack(">I", raw)[0]
                        pm.write(ram_base + in_off + f_off, struct.pack(">I", curr | btn_u32))
                hsd_off = RAM_OFFSET_HSD_PAD_MASTER + (p * 0x44)
                for f_off in (0x00, 0x08):
                    raw = pm.read(ram_base + hsd_off + f_off, 4)
                    if len(raw) == 4:
                        curr = struct.unpack(">I", raw)[0]
                        pm.write(ram_base + hsd_off + f_off, struct.pack(">I", curr | btn_u32))
            time.sleep(0.016)
        for p in target_ports:
            in_off = RAM_OFFSET_PLAYER_INPUTS_BASE + (p * 0x30)
            for f_off in (0x08, 0x0C, 0x20, 0x24):
                raw = pm.read(ram_base + in_off + f_off, 4)
                if len(raw) == 4:
                    curr = struct.unpack(">I", raw)[0]
                    pm.write(ram_base + in_off + f_off, struct.pack(">I", curr & ~btn_u32))
            hsd_off = RAM_OFFSET_HSD_PAD_MASTER + (p * 0x44)
            for f_off in (0x00, 0x08):
                raw = pm.read(ram_base + hsd_off + f_off, 4)
                if len(raw) == 4:
                    curr = struct.unpack(">I", raw)[0]
                    pm.write(ram_base + hsd_off + f_off, struct.pack(">I", curr & ~btn_u32))
    except Exception:
        pass


def inject_code_into_melee(pid: int, ram_base: int, code: str) -> bool:
    sjis_buf = encode_connect_code_3stride(code)
    code_len = len(code)

    try:
        with ProcessMemory(pid) as pm:
            css_port = pm.read(ram_base + RAM_OFFSET_CSS_PLAYER_INDEX, 1)
            active_port = css_port[0] if css_port and css_port[0] < 4 else 0
            ne_port = pm.read(ram_base + RAM_OFFSET_PLAYER_PORT, 1)
            if ne_port and ne_port[0] < 4:
                active_port = ne_port[0]
            target_ports = [active_port] + [p for p in (0, 1) if p != active_port]

            # 1. Prime primary and secondary buffers
            pm.write(ram_base + RAM_OFFSET_DIRECT_CODE_PRIMARY, sjis_buf)
            pm.write(ram_base + RAM_OFFSET_DIRECT_CODE_SECONDARY, sjis_buf)

            # 2. If on CSS, press Start to open Name Entry
            in_ne, r28 = is_in_name_entry(pm, ram_base)
            if not in_ne:
                if not send_controller_button("START", hold_sec=0.12):
                    pulse_ram_buttons(pm, ram_base, PAD_BUTTON_START, frames=8, target_ports=target_ports)

                start_wait = time.time()
                while time.time() - start_wait < 3.0:
                    in_ne, r28 = is_in_name_entry(pm, ram_base)
                    if in_ne:
                        time.sleep(0.350)
                        in_ne, r28 = is_in_name_entry(pm, ram_base)
                        break
                    time.sleep(0.040)

            # 3. Re-write buffers
            pm.write(ram_base + RAM_OFFSET_DIRECT_CODE_PRIMARY, sjis_buf)
            pm.write(ram_base + RAM_OFFSET_DIRECT_CODE_SECONDARY, sjis_buf)

            # 4. Update AutoComplete Buffer (ACB / ACXB)
            b_inst_bytes = pm.read(ram_base + RAM_OFFSET_INJ_CHECK_AUTOFILL, 4)
            if len(b_inst_bytes) == 4:
                b_inst = struct.unpack(">I", b_inst_bytes)[0]
                if (b_inst >> 26) == 18:
                    li = b_inst & 0x03FFFFFC
                    if li & 0x02000000:
                        li -= 0x04000000
                    gecko_target = 0x8023CCA4 + li
                    acb_raw = pm.read(ram_base + (gecko_target + 0x0C - 0x80000000), 4)
                    if len(acb_raw) == 4:
                        acb_addr = struct.unpack(">I", acb_raw)[0]
                        if 0x80000000 <= acb_addr <= 0x81800000:
                            pm.write(ram_base + (acb_addr - 0x80000000 + 4), bytes([code_len]))
                            acxb_raw = pm.read(ram_base + (acb_addr - 0x80000000 + 6), 4)
                            if len(acxb_raw) == 4:
                                acxb_addr = struct.unpack(">I", acxb_raw)[0]
                                if 0x80000000 <= acxb_addr <= 0x81800000:
                                    pm.write(ram_base + (acxb_addr - 0x80000000), b"\x01" + sjis_buf + bytes([code_len]))

            # 5. Reset cursor pos so Z autocompletes from index 0
            if in_ne and 0x80000000 <= r28 <= 0x81800000:
                pm.write(ram_base + (r28 - 0x80000000 + 0x58), b"\x00")

            # 6. Move hovered option to Confirm (57)
            pm.write(ram_base + RAM_OFFSET_MENU_FLOW_DATA + 2, struct.pack(">H", 57))

            # 7. Inside Name Entry: press Z to render text, then Start to confirm search
            if in_ne:
                orig_ne_port = pm.read(ram_base + RAM_OFFSET_PLAYER_PORT, 1)
                orig_port_val = orig_ne_port[0] if orig_ne_port else 0
                pm.write(ram_base + RAM_OFFSET_PLAYER_PORT, bytes([PIPE_PORT_INDEX]))
                try:
                    if not send_controller_button("Z", hold_sec=0.10):
                        pm.write(ram_base + RAM_OFFSET_PLAYER_PORT, bytes([orig_port_val]))
                        pulse_ram_buttons(pm, ram_base, PAD_TRIGGER_Z, frames=8, target_ports=target_ports)

                    pm.write(ram_base + RAM_OFFSET_MENU_FLOW_DATA + 2, struct.pack(">H", 57))
                    if 0x80000000 <= r28 <= 0x81800000:
                        pm.write(ram_base + (r28 - 0x80000000 + 0x58), bytes([min(code_len, 7)]))

                    time.sleep(0.250)

                    if not send_controller_button("START", hold_sec=0.12):
                        pulse_ram_buttons(pm, ram_base, PAD_BUTTON_START | PAD_BUTTON_A, frames=8, target_ports=target_ports)
                finally:
                    pm.write(ram_base + RAM_OFFSET_PLAYER_PORT, bytes([orig_port_val]))

            return True
    except Exception as e:
        sys.stderr.write(f"Injection error on PID {pid}: {e}\n")
        return False


def update_slippi_json_files(code: str, mode: str):
    payload = {
        "commandId": str(uuid.uuid4()),
        "mode": mode,
        "connectCode": code,
        "createdAt": int(time.time()),
        "ttlSeconds": 300,
    }
    fullwidth_code = to_fullwidth(code)

    for user_dir in get_dolphin_user_dirs():
        slippi_dir = os.path.join(user_dir, "Slippi")
        target_dir = slippi_dir if os.path.isdir(slippi_dir) else user_dir
        try:
            pending_path = os.path.join(target_dir, "pending-connect.json")
            tmp_path = f"{pending_path}.{int(time.time())}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, pending_path)

            codes_file = os.path.join(target_dir, "direct-codes.json" if mode == "direct" else "teams-codes.json")
            codes = []
            if os.path.exists(codes_file):
                try:
                    with open(codes_file, "r", encoding="utf-8") as f:
                        codes = json.load(f)
                except Exception:
                    codes = []
            codes = [e for e in codes if e.get("connectCode") != fullwidth_code]
            codes.insert(0, {"connectCode": fullwidth_code, "lastPlayed": int(time.time())})
            tmp_c = f"{codes_file}.{int(time.time())}.tmp"
            with open(tmp_c, "w", encoding="utf-8") as f:
                json.dump(codes, f, indent=2, ensure_ascii=False)
            os.replace(tmp_c, codes_file)
        except Exception:
            continue


def handle_connect(raw_code: str, mode: str = "direct"):
    code = normalize_connect_code(raw_code)
    if not code:
        notify("Slippi Direct Link Error", f"Invalid connect code: {raw_code}")
        return

    update_slippi_json_files(code, mode)

    pids = find_dolphin_pids()
    if not pids:
        notify("Slippi Direct Link", f"Staged {code} (no running Dolphin/MeleeUnlocked found)")
        return

    focus_dolphin_window()
    time.sleep(0.100)

    injected = 0
    for pid in pids:
        for base in find_melee_ram_bases(pid):
            if inject_code_into_melee(pid, base, code):
                injected += 1

    if injected > 0:
        notify("Slippi Direct Link", f"Injected {code} into live Melee RAM")


# ---------------------------------------------------------------------------
# One-Time Installer (Linux & Windows)
# ---------------------------------------------------------------------------

def configure_dolphin_port4(extra_dirs=None):
    user_dirs = get_dolphin_user_dirs(extra_dirs)
    for user_dir in user_dirs:
        config_dir = os.path.join(user_dir, "Config")
        if not os.path.isdir(config_dir):
            continue

        # 1. Dolphin.ini -> SIDevice3 = 6 (Standard Controller on Port 4)
        dolphin_ini = os.path.join(config_dir, "Dolphin.ini")
        if os.path.exists(dolphin_ini):
            with open(dolphin_ini, "r", encoding="utf-8") as f:
                content = f.read()
            if re.search(r"^\s*SIDevice3\s*=", content, re.MULTILINE):
                content = re.sub(r"^\s*SIDevice3\s*=.*$", "SIDevice3 = 6", content, flags=re.MULTILINE)
            elif "[Core]" in content:
                content = content.replace("[Core]\n", "[Core]\nSIDevice3 = 6\n", 1)
            else:
                content += "\n[Core]\nSIDevice3 = 6\n"
            if re.search(r"^\s*BlockingPipes\s*=", content, re.MULTILINE):
                content = re.sub(r"^\s*BlockingPipes\s*=.*$", "BlockingPipes = False", content, flags=re.MULTILINE)
            with open(dolphin_ini, "w", encoding="utf-8") as f:
                f.write(content)

        # 2. GCPadNew.ini -> [GCPad4]
        gcpad_ini = os.path.join(config_dir, "GCPadNew.ini")
        cp = configparser.RawConfigParser()
        cp.optionxform = str
        if os.path.exists(gcpad_ini):
            cp.read(gcpad_ini, encoding="utf-8")
        if not cp.has_section("GCPad4"):
            cp.add_section("GCPad4")

        if IS_LINUX:
            pipes_dir = os.path.join(user_dir, "Pipes")
            os.makedirs(pipes_dir, exist_ok=True)
            pipe_path = os.path.join(pipes_dir, PIPE_NAME)
            if os.path.exists(pipe_path):
                if not stat.S_ISFIFO(os.stat(pipe_path).st_mode):
                    os.remove(pipe_path)
                    os.mkfifo(pipe_path)
            else:
                os.mkfifo(pipe_path)
            cp.set("GCPad4", "Device", f"Pipe/0/{PIPE_NAME}")
            cp.set("GCPad4", "Buttons/A", "`Button A`")
            cp.set("GCPad4", "Buttons/B", "`Button B`")
            cp.set("GCPad4", "Buttons/Z", "`Button Z`")
            cp.set("GCPad4", "Buttons/Start", "`Button START`")
        elif IS_WINDOWS:
            cp.set("GCPad4", "Device", "DInput/0/Keyboard Mouse")
            cp.set("GCPad4", "Buttons/Start", "F13")
            cp.set("GCPad4", "Buttons/Z", "F14")
            cp.set("GCPad4", "Buttons/A", "F15")

        with open(gcpad_ini, "w", encoding="utf-8") as f:
            cp.write(f, space_around_delimiters=True)

        print(f"Configured Dolphin Port 4 in: {user_dir}")


def install_self(extra_dirs=None):
    is_frozen = getattr(sys, "frozen", False)
    src_path = os.path.abspath(sys.executable if is_frozen else __file__)

    if IS_LINUX:
        dest_dir = os.path.expanduser("~/.local/bin")
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, "slippi-url-handler")
        if os.path.abspath(src_path) != os.path.abspath(dest_path):
            shutil.copy2(src_path, dest_path)
            os.chmod(dest_path, 0o755)

        apps_dir = os.path.expanduser("~/.local/share/applications")
        os.makedirs(apps_dir, exist_ok=True)
        desktop_path = os.path.join(apps_dir, "slippi-url-handler.desktop")
        exec_line = f"{dest_path} %u" if is_frozen else f"python3 {dest_path} %u"
        desktop_content = (
            "[Desktop Entry]\n"
            "Name=Slippi Direct Link Handler\n"
            f"Exec={exec_line}\n"
            "Type=Application\n"
            "NoDisplay=true\n"
            "MimeType=x-scheme-handler/slippi;\n"
        )
        with open(desktop_path, "w", encoding="utf-8") as f:
            f.write(desktop_content)
        subprocess.run(["xdg-mime", "default", "slippi-url-handler.desktop", "x-scheme-handler/slippi"], check=False)
        print(f"Installed handler to: {dest_path}")
        print(f"Registered slippi:// protocol via: {desktop_path}")

    elif IS_WINDOWS:
        import winreg
        dest_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SlippiDirectLink")
        os.makedirs(dest_dir, exist_ok=True)
        dest_name = "slippi-direct-link.exe" if is_frozen else "slippi_direct_link.py"
        dest_path = os.path.join(dest_dir, dest_name)
        if os.path.abspath(src_path) != os.path.abspath(dest_path):
            shutil.copy2(src_path, dest_path)

        cmd = f'"{dest_path}" "%1"' if is_frozen else f'"{sys.executable}" "{dest_path}" "%1"'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\slippi") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Slippi Direct Link Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\slippi\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, cmd)
        print(f"Installed handler to: {dest_path}")
        print("Registered slippi:// protocol in HKCU\\Software\\Classes\\slippi")

    configure_dolphin_port4(extra_dirs)
    print("\nSetup complete! Restart Dolphin/MeleeUnlocked once, then click any slippi://connect?code=... link.")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("--install", "install"):
        install_self(args[1:] if args else None)
        return

    raw = args[0]
    if raw.startswith("slippi://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.netloc == "connect" or parsed.path in ("connect", "//connect"):
            params = urllib.parse.parse_qs(parsed.query)
            raw_code = params.get("code", [None])[0]
            raw_mode = params.get("mode", ["direct"])[0]
            mode = "teams" if raw_mode.lower() in ("teams", "3") else "direct"
            handle_connect(raw_code, mode)
    else:
        handle_connect(raw, "direct")


if __name__ == "__main__":
    main()
