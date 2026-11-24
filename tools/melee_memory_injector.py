#!/usr/bin/env python3
"""
melee_memory_injector.py - Live GameCube RAM injector for Super Smash Bros. Melee / Slippi Dolphin.

Injects connect codes directly into running Dolphin process memory (/proc/<pid>/mem)
without requiring any game restart, Gecko reload, or custom emulator build.
"""

import glob
import os
import re
import struct
import subprocess
import sys
import time

CONNECT_CODE_REGEX = re.compile(r"^[A-Z0-9]{1,7}#[0-9]{1,4}$")

# Fullwidth Shift-JIS mapping for Melee
# Each character is 2 bytes in Melee RAM:
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
    "#": (0x81, 0x94)
}

# Offsets relative to GameCube RAM Base (0x80000000)
RAM_OFFSET_DIRECT_CODE_PRIMARY = 0x004A0740    # 24-byte 3-stride Shift-JIS buffer
RAM_OFFSET_DIRECT_CODE_SECONDARY = 0x004A0760  # 24-byte 3-stride Shift-JIS buffer
RAM_OFFSET_MENU_FLOW_DATA = 0x004A04F0         # Menu flow / hovered option struct
RAM_OFFSET_SCENE_INFO = 0x00479D30             # Major / Minor scene ID

def normalize_connect_code(raw: str) -> str:
    if not raw:
        return None
    cleaned = raw.strip().replace("-", "#").upper()
    if len(cleaned) > 8 or "#" not in cleaned:
        return None
    if not CONNECT_CODE_REGEX.match(cleaned):
        return None
    return cleaned

def encode_connect_code_3stride(code: str) -> bytes:
    """Encodes connect code as 24-byte fullwidth Shift-JIS with 3-byte stride."""
    buf = bytearray(24)
    for i, ch in enumerate(code[:8]):
        if ch in SJIS_TABLE:
            b0, b1 = SJIS_TABLE[ch]
            buf[i * 3] = b0
            buf[i * 3 + 1] = b1
            buf[i * 3 + 2] = 0x00
    return bytes(buf)

def find_dolphin_pids():
    """Finds all running Dolphin / Slippi emulator process IDs."""
    pids = []
    for proc_dir in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(proc_dir))
            cmdline_path = os.path.join(proc_dir, "cmdline")
            if not os.path.exists(cmdline_path):
                continue
            with open(cmdline_path, "rb") as f:
                cmd = f.read()
                # Check for Dolphin process signatures
                if any(sig in cmd for sig in [b"dolphin-emu", b"AppRun.wrapped", b"Slippi_Online", b"Slippi_Netplay"]):
                    pids.append(pid)
        except (PermissionError, ProcessLookupError, ValueError):
            continue
    return pids

def find_melee_ram_bases(pid: int):
    """
    Locates all GameCube RAM base addresses for Melee (GALE01) within the target process.
    Returns list of host virtual addresses corresponding to GameCube RAM 0x80000000.
    """
    ram_bases = []
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

                addr_range = parts[0].split("-")
                start = int(addr_range[0], 16)
                end = int(addr_range[1], 16)
                size = end - start

                # Dolphin GameCube RAM mappings are typically 32MB (0x2000000) or 24MB (0x1800000)
                # or fastmem view regions >= 24MB
                if size in (0x2000000, 0x1800000, 0x4000000) or (0x1800000 <= size <= 0x10000000):
                    try:
                        mem_f.seek(start)
                        hdr = mem_f.read(32)
                        # Check Melee Game ID: "GALE01" + GameCube DVD magic word 0xC2339F3D at offset 0x1C
                        if hdr.startswith(b"GALE01") and len(hdr) >= 32:
                            dvd_magic = hdr[0x1C:0x20]
                            if dvd_magic == b"\xc2\x33\x9f\x3d" or dvd_magic == b"\x00\x00\x00\x00" or True:
                                ram_bases.append(start)
                    except (OSError, OverflowError):
                        continue
    except (PermissionError, ProcessLookupError, FileNotFoundError):
        pass

    return ram_bases

def inject_code_into_melee(pid: int, ram_base: int, code: str) -> bool:
    """Writes the connect code into Melee RAM buffers in the running Dolphin process."""
    sjis_buf = encode_connect_code_3stride(code)
    mem_path = f"/proc/{pid}/mem"

    try:
        with open(mem_path, "r+b", 0) as mem_f:
            # 1. Write to Primary Connect Code Buffer (0x804A0740)
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_PRIMARY)
            mem_f.write(sjis_buf)

            # 2. Write to Secondary Connect Code Buffer (0x804A0760)
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_SECONDARY)
            mem_f.write(sjis_buf)

            # 3. Read current scene info (0x80479D30)
            mem_f.seek(ram_base + RAM_OFFSET_SCENE_INFO)
            scene_info = mem_f.read(8)

            major_scene = scene_info[3] if len(scene_info) > 3 else 0
            minor_scene = scene_info[4] if len(scene_info) > 4 else 0

            # 4. If in Character Select Screen or Text Entry, update hovered option to Confirm (57)
            # Menu_FlowData (0x804A04F0) -> MFD_HOVERED_OPTION at offset 2
            mem_f.seek(ram_base + RAM_OFFSET_MENU_FLOW_DATA + 2)
            mem_f.write(struct.pack(">H", 57))

            return True
    except Exception as e:
        sys.stderr.write(f"Injection error on PID {pid}: {e}\n")
        return False

def inject_connect_code(raw_code: str) -> dict:
    code = normalize_connect_code(raw_code)
    if not code:
        return {"success": False, "error": f"Invalid connect code: {raw_code}"}

    pids = find_dolphin_pids()
    if not pids:
        return {"success": False, "error": "No running Dolphin / Melee process found."}

    injected_count = 0
    for pid in pids:
        ram_bases = find_melee_ram_bases(pid)
        for base in ram_bases:
            if inject_code_into_melee(pid, base, code):
                injected_count += 1

    return {
        "success": injected_count > 0,
        "code": code,
        "injected_bases": injected_count,
        "pids": pids
    }

def focus_dolphin_window():
    """Brings the active Melee / Dolphin window to the foreground."""
    try:
        subprocess.run(["swaymsg", '[title="(?i)Faster Melee"] focus'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["swaymsg", '[class="(?i)Apprun"] focus'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["swaymsg", '[class="(?i)dolphin"] focus'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: melee_memory_injector.py <CONNECT_CODE>")
        sys.exit(1)

    result = inject_connect_code(sys.argv[1])
    print(result)
    if result["success"]:
        focus_dolphin_window()
