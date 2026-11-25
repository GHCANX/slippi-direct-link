#!/usr/bin/env python3
"""
melee_memory_injector.py - Live GameCube RAM injector for Super Smash Bros. Melee / Slippi Dolphin.

Injects connect codes directly into running Dolphin process memory (/proc/<pid>/mem)
without requiring any game restart, Gecko reload, or custom emulator build.
"""

import glob
import json
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
RAM_OFFSET_NAME_ENTRY_MODE = 0x004D6643        # -0x505D(r13): 0 = normal/CSS, 1 = connect code Name Entry
RAM_OFFSET_NAME_ENTRY_MGR_PTR = 0x004D6C08     # -0x4A98(r13) pointing to NameEntry GObj/Manager
RAM_OFFSET_PLAYER_PORT = 0x004D6C0C            # -0x4A94(r13) active player port byte in Name Entry
RAM_OFFSET_CSS_PLAYER_INDEX = 0x004D6CF0       # -0x49B0(r13) player index controlling CSS
RAM_OFFSET_CSS_CHAR_SELECTED = 0x004D6CF7      # -0x49A9(r13) non-zero if character is selected
RAM_OFFSET_PLAYER_INPUTS_BASE = 0x00489C30     # Base address of controller inputs table
RAM_OFFSET_HSD_PAD_MASTER = 0x004C1FAC         # HSD_PadMaster (4 x 12-byte HSD_PadStatus structs)

# GameCube controller button bitmasks
PAD_TRIGGER_Z = 0x0010
PAD_BUTTON_START = 0x1000
PAD_BUTTON_A = 0x0100

PIPE_PORT_INDEX = 3  # Port 4 (0-indexed) dedicated to Pipe/0/slippi_direct_link
PIPE_CANDIDATE_PATHS = [
    os.path.expanduser("~/.config/SlippiOnline/Pipes/slippi_direct_link"),
    os.path.expanduser("~/.config/slippi-dolphin/netplay-beta/Pipes/slippi_direct_link"),
    os.path.expanduser("~/.config/slippi-dolphin/netplay/Pipes/slippi_direct_link"),
    os.path.expanduser("~/AppImages/MeleeUnlocked-0.9.209/User/Pipes/slippi_direct_link"),
]

def send_pipe_button(button_name: str, hold_sec: float = 0.12) -> bool:
    """
    Sends a PRESS/RELEASE button event to all active Dolphin named pipes.
    Returns True if at least one Dolphin process had the pipe open for reading.
    """
    sent = False
    open_fds = []
    for path in PIPE_CANDIDATE_PATHS:
        if not os.path.exists(path):
            continue
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, f"PRESS {button_name}\n".encode("ascii"))
            open_fds.append(fd)
            sent = True
        except OSError:
            # ENXIO means no Dolphin reader is currently attached to this pipe
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
    return sent

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
                        # Check Melee Game ID: "GALE01"
                        if hdr.startswith(b"GALE01") and len(hdr) >= 32:
                            ram_bases.append(start)
                    except (OSError, OverflowError):
                        continue
    except (PermissionError, ProcessLookupError, FileNotFoundError):
        pass

    return ram_bases

def is_in_name_entry(mem_f, ram_base) -> tuple:
    """
    Checks if Melee is currently on the active Direct Connect NameEntry screen.
    Uses OFST_R13_NAME_ENTRY_MODE (0x804D6643) which is 1 only while Name Entry is open.
    Returns (True, r28_address) if active, else (False, 0).
    """
    try:
        # Check OFST_R13_NAME_ENTRY_MODE (0x804D6643). 1 = Name Entry active, 0 = CSS
        mem_f.seek(ram_base + RAM_OFFSET_NAME_ENTRY_MODE)
        mode_byte = mem_f.read(1)
        if not mode_byte or mode_byte[0] != 1:
            return False, 0

        # Read NameEntry GObj manager pointer from -0x4A98(r13) (0x804D6C08)
        mem_f.seek(ram_base + RAM_OFFSET_NAME_ENTRY_MGR_PTR)
        ne_mgr_bytes = mem_f.read(4)
        if len(ne_mgr_bytes) < 4:
            return False, 0
        ne_mgr = struct.unpack(">I", ne_mgr_bytes)[0]
        if not (0x80000000 <= ne_mgr <= 0x81800000):
            return False, 0

        mem_f.seek(ram_base + (ne_mgr - 0x80000000 + 0x2C))
        r28_bytes = mem_f.read(4)
        if len(r28_bytes) < 4:
            return False, 0
        r28 = struct.unpack(">I", r28_bytes)[0]
        if not (0x80000000 <= r28 <= 0x81800000):
            return False, 0

        return True, r28
    except Exception:
        return False, 0

def pulse_controller_buttons(mem_f, ram_base, buttons: int, frames: int = 8, target_ports=(0, 1)):
    """
    Pulses controller buttons on target ports across `frames` frames (~16ms per frame).
    Writes to both Melee player inputs table (0x80489C30, stride 0x30: +0x08/+0x0C and +0x20/+0x24)
    and HSD_PadMaster (0x804C1FAC, stride 0x44: +0x00 held, +0x08 instant) as u32 bitmasks.
    """
    try:
        btn_u32 = buttons & 0xFFFFFFFF
        for _ in range(frames):
            for p in target_ports:
                # 1. Melee game input table (0x80489C30 + p * 0x30)
                input_offset = RAM_OFFSET_PLAYER_INPUTS_BASE + (p * 0x30)
                for field_off in (0x08, 0x0C, 0x20, 0x24):
                    mem_f.seek(ram_base + input_offset + field_off)
                    curr = struct.unpack(">I", mem_f.read(4))[0]
                    mem_f.seek(ram_base + input_offset + field_off)
                    mem_f.write(struct.pack(">I", curr | btn_u32))

                # 2. HSD_PadMaster (0x804C1FAC + p * 0x44): +0x00 held u32, +0x08 instant u32
                hsd_offset = RAM_OFFSET_HSD_PAD_MASTER + (p * 0x44)
                for field_off in (0x00, 0x08):
                    mem_f.seek(ram_base + hsd_offset + field_off)
                    curr_hsd = struct.unpack(">I", mem_f.read(4))[0]
                    mem_f.seek(ram_base + hsd_offset + field_off)
                    mem_f.write(struct.pack(">I", curr_hsd | btn_u32))

            time.sleep(0.016)

        # Release buttons
        for p in target_ports:
            input_offset = RAM_OFFSET_PLAYER_INPUTS_BASE + (p * 0x30)
            for field_off in (0x08, 0x0C, 0x20, 0x24):
                mem_f.seek(ram_base + input_offset + field_off)
                curr = struct.unpack(">I", mem_f.read(4))[0]
                mem_f.seek(ram_base + input_offset + field_off)
                mem_f.write(struct.pack(">I", curr & ~btn_u32))

            hsd_offset = RAM_OFFSET_HSD_PAD_MASTER + (p * 0x44)
            for field_off in (0x00, 0x08):
                mem_f.seek(ram_base + hsd_offset + field_off)
                curr_hsd = struct.unpack(">I", mem_f.read(4))[0]
                mem_f.seek(ram_base + hsd_offset + field_off)
                mem_f.write(struct.pack(">I", curr_hsd & ~btn_u32))
    except Exception:
        pass

def inject_code_into_melee(pid: int, ram_base: int, code: str) -> bool:
    """Writes connect code into Melee RAM, transitions from CSS if needed, renders text, and auto-starts search."""
    sjis_buf = encode_connect_code_3stride(code)
    code_len = len(code)
    mem_path = f"/proc/{pid}/mem"

    try:
        with open(mem_path, "r+b", 0) as mem_f:
            # Determine active player port (from CSS or Name Entry)
            mem_f.seek(ram_base + RAM_OFFSET_CSS_PLAYER_INDEX)
            css_port = mem_f.read(1)
            active_port = css_port[0] if css_port and css_port[0] < 4 else 0

            mem_f.seek(ram_base + RAM_OFFSET_PLAYER_PORT)
            ne_port = mem_f.read(1)
            if ne_port and ne_port[0] < 4:
                active_port = ne_port[0]

            target_ports = [active_port]
            for p in (0, 1):
                if p not in target_ports:
                    target_ports.append(p)

            # 1. Prime Primary and Secondary Connect Code Buffers (0x804A0740 & 0x804A0760)
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_PRIMARY)
            mem_f.write(sjis_buf)
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_SECONDARY)
            mem_f.write(sjis_buf)

            # 2. Check if currently inside Name Entry screen or on CSS
            in_ne, r28 = is_in_name_entry(mem_f, ram_base)
            if not in_ne:
                # On CSS with character selected: press Start via Dolphin Pipe (fallback to RAM pulse)
                if not send_pipe_button("START", hold_sec=0.12):
                    pulse_controller_buttons(mem_f, ram_base, PAD_BUTTON_START, frames=8, target_ports=target_ports)

                # Wait up to 3.0s for Name Entry screen to load
                start_wait = time.time()
                while time.time() - start_wait < 3.0:
                    in_ne, r28 = is_in_name_entry(mem_f, ram_base)
                    if in_ne:
                        # Allow 350ms for Name Entry entrance animation to complete and inputs to unlock
                        time.sleep(0.350)
                        # Re-verify r28
                        in_ne, r28 = is_in_name_entry(mem_f, ram_base)
                        break
                    time.sleep(0.040)

            # 3. Always re-write connect code buffers to ensure clean data
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_PRIMARY)
            mem_f.write(sjis_buf)
            mem_f.seek(ram_base + RAM_OFFSET_DIRECT_CODE_SECONDARY)
            mem_f.write(sjis_buf)

            # 4. Update AutoComplete Buffer (ACB / ACXB)
            mem_f.seek(ram_base + RAM_OFFSET_INJ_CHECK_AUTOFILL)
            b_inst_bytes = mem_f.read(4)
            if len(b_inst_bytes) == 4:
                b_inst = struct.unpack(">I", b_inst_bytes)[0]
                if (b_inst >> 26) == 18:  # Branch instruction
                    li = b_inst & 0x03FFFFFC
                    if li & 0x02000000:
                        li -= 0x04000000
                    gecko_target = 0x8023CCA4 + li
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
                            mem_f.seek(ram_base + (acxb_addr - 0x80000000))
                            mem_f.write(b"\x01" + sjis_buf + bytes([code_len]))

            # 5. Reset typed character count in r28 so Z trigger autocompletes from index 0
            if in_ne and 0x80000000 <= r28 <= 0x81800000:
                mem_f.seek(ram_base + (r28 - 0x80000000 + 0x58))
                mem_f.write(b"\x00")

            # 6. Set hovered option to Confirm button (57)
            mem_f.seek(ram_base + RAM_OFFSET_MENU_FLOW_DATA + 2)
            mem_f.write(struct.pack(">H", 57))

            # 7. If inside Name Entry, press Z to render text and Start to confirm search
            if in_ne:
                # Save original Name Entry active port and temporarily route to Pipe Port 4 (index 3)
                mem_f.seek(ram_base + RAM_OFFSET_PLAYER_PORT)
                orig_ne_port = mem_f.read(1)
                orig_port_val = orig_ne_port[0] if orig_ne_port else 0
                mem_f.seek(ram_base + RAM_OFFSET_PLAYER_PORT)
                mem_f.write(bytes([PIPE_PORT_INDEX]))
                try:
                    # Press Z via Pipe (fallback to RAM pulse) to trigger autocomplete & glyph render
                    if not send_pipe_button("Z", hold_sec=0.10):
                        mem_f.seek(ram_base + RAM_OFFSET_PLAYER_PORT)
                        mem_f.write(bytes([orig_port_val]))
                        pulse_controller_buttons(mem_f, ram_base, PAD_TRIGGER_Z, frames=8, target_ports=target_ports)

                    # Ensure hovered option remains Confirm (57) and r28 cursor length is set
                    mem_f.seek(ram_base + RAM_OFFSET_MENU_FLOW_DATA + 2)
                    mem_f.write(struct.pack(">H", 57))
                    if 0x80000000 <= r28 <= 0x81800000:
                        mem_f.seek(ram_base + (r28 - 0x80000000 + 0x58))
                        mem_f.write(bytes([min(code_len, 7)]))

                    # Allow 250ms for Melee text update glyph generation and chime sound to settle
                    time.sleep(0.250)

                    # 8. Press Start via Pipe (fallback to RAM pulse) to submit Confirm and start searching
                    if not send_pipe_button("START", hold_sec=0.12):
                        pulse_controller_buttons(mem_f, ram_base, PAD_BUTTON_START | PAD_BUTTON_A, frames=8, target_ports=target_ports)
                finally:
                    # Restore original Name Entry port
                    mem_f.seek(ram_base + RAM_OFFSET_PLAYER_PORT)
                    mem_f.write(bytes([orig_port_val]))

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
    """Brings the active Melee / Dolphin game render window (not the launcher) to the foreground."""
    try:
        tree = json.loads(subprocess.check_output(["swaymsg", "-t", "get_tree"], timeout=1))

        def get_all_views(node):
            views = []
            if node.get("name") or node.get("window_properties"):
                views.append(node)
            for c in node.get("nodes", []) + node.get("floating_nodes", []):
                views.extend(get_all_views(c))
            return views

        views = get_all_views(tree)
        target_con = None
        for v in views:
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

        subprocess.run(["swaymsg", '[title="^Dolphin$"] focus'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["swaymsg", '[title="(?i)GALE01"] focus'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["swaymsg", '[title="(?i)Super Smash Bros"] focus'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: melee_memory_injector.py <CONNECT_CODE>")
        sys.exit(1)

    focus_dolphin_window()
    time.sleep(0.100)
    result = inject_connect_code(sys.argv[1])
    print(result)
