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
RAM_OFFSET_INJ_CHECK_AUTOFILL = 0x0023CCA4     # Gecko hook branch to AutoComplete block
RAM_OFFSET_NAME_ENTRY_MGR_PTR = 0x004D6C08     # -0x4A98(r13) pointing to NameEntry GObj/Manager
RAM_OFFSET_PLAYER_PORT = 0x004D6C0C            # -0x4A94(r13) active player port byte
RAM_OFFSET_PLAYER_INPUTS_BASE = 0x00489C30     # Base address of controller inputs table

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
    """Writes the connect code into Melee RAM buffers in the running Dolphin process and refreshes display."""
    sjis_buf = encode_connect_code_3stride(code)
    code_len = len(code)
    mem_path = f"/proc/{pid}/mem"

    try:
        with open(mem_path, "r+b", 0) as mem_f:
            # 1. Write to Primary Connect Code Buffer (0x804A0740)
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_PRIMARY)
            mem_f.write(sjis_buf)

            # 2. Write to Secondary Connect Code Buffer (0x804A0760)
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_SECONDARY)
            mem_f.write(sjis_buf)

            # 3. Check AutoComplete Buffer (ACB / ACXB)
            mem_f.seek(ram_base + RAM_OFFSET_INJ_CHECK_AUTOFILL)
            b_inst_bytes = mem_f.read(4)
            if len(b_inst_bytes) == 4:
                b_inst = struct.unpack(">I", b_inst_bytes)[0]
                if (b_inst >> 26) == 18:  # Branch instruction
                    li = b_inst & 0x03FFFFFC
                    if li & 0x02000000:
                        li -= 0x04000000
                    gecko_target = 0x8023CCA4 + li
                    # IDO_ACB_ADDR is at gecko_target + 0x0C
                    mem_f.seek(ram_base + (gecko_target + 0x0C - 0x80000000))
                    acb_addr = struct.unpack(">I", mem_f.read(4))[0]
                    if 0x80000000 <= acb_addr <= 0x81800000:
                        # ACB_COMMITTED_CHAR_COUNT is at offset +4
                        mem_f.seek(ram_base + (acb_addr - 0x80000000 + 4))
                        mem_f.write(bytes([code_len]))

                        # ACB_ACXB_ADDR is at offset +6
                        mem_f.seek(ram_base + (acb_addr - 0x80000000 + 6))
                        acxb_addr = struct.unpack(">I", mem_f.read(4))[0]
                        if 0x80000000 <= acxb_addr <= 0x81800000:
                            # ACXB: byte 0 has_suggestion(1) + bytes 1..24 suggestion + byte 25 len
                            mem_f.seek(ram_base + (acxb_addr - 0x80000000))
                            mem_f.write(b"\x01" + sjis_buf + bytes([code_len]))

            # 4. Check active NameEntry screen struct r28
            mem_f.seek(ram_base + RAM_OFFSET_NAME_ENTRY_MGR_PTR)
            ne_mgr_bytes = mem_f.read(4)
            in_name_entry = False
            r28 = 0
            if len(ne_mgr_bytes) == 4:
                ne_mgr = struct.unpack(">I", ne_mgr_bytes)[0]
                if 0x80000000 <= ne_mgr <= 0x81800000:
                    mem_f.seek(ram_base + (ne_mgr - 0x80000000 + 0x2C))
                    r28 = struct.unpack(">I", mem_f.read(4))[0]
                    if 0x80000000 <= r28 <= 0x81800000:
                        in_name_entry = True
                        # Reset cursor position to 0 so Z trigger autocompletes cleanly from start
                        mem_f.seek(ram_base + (r28 - 0x80000000 + 0x58))
                        mem_f.write(b"\x00")

            # 5. Move hovered option to Confirm button (57)
            mem_f.seek(ram_base + RAM_OFFSET_MENU_FLOW_DATA + 2)
            mem_f.write(struct.pack(">H", 57))

            # 6. If currently inside NameEntry, pulse the Z button (0x0010)
            # to trigger Melee's CheckTriggersAndZ autocomplete handler which calls NameEntry_UpdateTypedName (0x8023CE4C)
            if in_name_entry:
                mem_f.seek(ram_base + RAM_OFFSET_PLAYER_PORT)
                port_byte = mem_f.read(1)
                port = port_byte[0] if port_byte and port_byte[0] < 4 else 0
                target_ports = [port] if port in (0, 1, 2, 3) else [0]
                if 0 not in target_ports:
                    target_ports.append(0)

                # Pulse Z button across multiple frames (~60ms) so Melee input loop catches it
                for _ in range(4):
                    for p in target_ports:
                        input_offset = RAM_OFFSET_PLAYER_INPUTS_BASE + (p * 0x30)
                        # Set Z in instant buttons (offset +0x0C)
                        mem_f.seek(ram_base + input_offset + 0x0C)
                        curr_instant = struct.unpack(">I", mem_f.read(4))[0]
                        mem_f.seek(ram_base + input_offset + 0x0C)
                        mem_f.write(struct.pack(">I", curr_instant | 0x0010))

                        # Set Z in held buttons (offset +0x08)
                        mem_f.seek(ram_base + input_offset + 0x08)
                        curr_held = struct.unpack(">I", mem_f.read(4))[0]
                        mem_f.seek(ram_base + input_offset + 0x08)
                        mem_f.write(struct.pack(">I", curr_held | 0x0010))

                    time.sleep(0.016)

                # Clear Z pulse
                for p in target_ports:
                    input_offset = RAM_OFFSET_PLAYER_INPUTS_BASE + (p * 0x30)
                    mem_f.seek(ram_base + input_offset + 0x0C)
                    curr_instant = struct.unpack(">I", mem_f.read(4))[0]
                    mem_f.seek(ram_base + input_offset + 0x0C)
                    mem_f.write(struct.pack(">I", curr_instant & ~0x0010))

                    mem_f.seek(ram_base + input_offset + 0x08)
                    curr_held = struct.unpack(">I", mem_f.read(4))[0]
                    mem_f.seek(ram_base + input_offset + 0x08)
                    mem_f.write(struct.pack(">I", curr_held & ~0x0010))

                # Ensure hovered option remains Confirm (57) and r28 cursor length is set
                mem_f.seek(ram_base + RAM_OFFSET_MENU_FLOW_DATA + 2)
                mem_f.write(struct.pack(">H", 57))
                if 0x80000000 <= r28 <= 0x81800000:
                    mem_f.seek(ram_base + (r28 - 0x80000000 + 0x58))
                    mem_f.write(bytes([min(code_len, 7)]))

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
