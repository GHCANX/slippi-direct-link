#!/usr/bin/env python3
"""
setup_dolphin_pipe.py - Configures Dolphin / Slippi Port 4 for Named Pipe input.

Creates <UserDir>/Pipes/slippi_direct_link, sets SIDevice3 = 6 (Standard Controller)
in Config/Dolphin.ini, and configures [GCPad4] in Config/GCPadNew.ini to read from
Pipe/0/slippi_direct_link. Leaves Ports 1-3 (GameCube Adapter) untouched.
"""

import configparser
import os
import re
import stat
import sys

PIPE_NAME = "slippi_direct_link"

CANDIDATE_USER_DIRS = [
    os.path.expanduser("~/.config/SlippiOnline"),
    os.path.expanduser("~/.config/slippi-dolphin/netplay-beta"),
    os.path.expanduser("~/.config/slippi-dolphin/netplay"),
    os.path.expanduser("~/AppImages/MeleeUnlocked-0.9.209/User"),
]


def ensure_pipe(user_dir: str) -> str:
    pipes_dir = os.path.join(user_dir, "Pipes")
    os.makedirs(pipes_dir, exist_ok=True)
    pipe_path = os.path.join(pipes_dir, PIPE_NAME)
    if os.path.exists(pipe_path):
        if not stat.S_ISFIFO(os.stat(pipe_path).st_mode):
            os.remove(pipe_path)
            os.mkfifo(pipe_path)
    else:
        os.mkfifo(pipe_path)
    return pipe_path


def configure_dolphin_ini(config_dir: str) -> bool:
    ini_path = os.path.join(config_dir, "Dolphin.ini")
    if not os.path.exists(ini_path):
        return False

    with open(ini_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Set SIDevice3 = 6 (Standard Controller on Port 4)
    if re.search(r"^\s*SIDevice3\s*=", content, re.MULTILINE):
        content = re.sub(r"^\s*SIDevice3\s*=.*$", "SIDevice3 = 6", content, flags=re.MULTILINE)
    elif "[Core]" in content:
        content = content.replace("[Core]\n", "[Core]\nSIDevice3 = 6\n", 1)
    else:
        content += "\n[Core]\nSIDevice3 = 6\n"

    # Ensure BlockingPipes = False
    if re.search(r"^\s*BlockingPipes\s*=", content, re.MULTILINE):
        content = re.sub(r"^\s*BlockingPipes\s*=.*$", "BlockingPipes = False", content, flags=re.MULTILINE)

    with open(ini_path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def configure_gcpad_ini(config_dir: str) -> bool:
    ini_path = os.path.join(config_dir, "GCPadNew.ini")
    cp = configparser.RawConfigParser()
    cp.optionxform = str  # preserve case
    if os.path.exists(ini_path):
        cp.read(ini_path, encoding="utf-8")

    section = "GCPad4"
    if not cp.has_section(section):
        cp.add_section(section)

    cp.set(section, "Device", f"Pipe/0/{PIPE_NAME}")
    cp.set(section, "Buttons/A", "`Button A`")
    cp.set(section, "Buttons/B", "`Button B`")
    cp.set(section, "Buttons/X", "`Button X`")
    cp.set(section, "Buttons/Y", "`Button Y`")
    cp.set(section, "Buttons/Z", "`Button Z`")
    cp.set(section, "Buttons/Start", "`Button START`")

    with open(ini_path, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=True)
    return True


def main():
    extra_dirs = sys.argv[1:]
    user_dirs = extra_dirs if extra_dirs else CANDIDATE_USER_DIRS
    configured = []

    for user_dir in user_dirs:
        config_dir = os.path.join(user_dir, "Config")
        if not os.path.isdir(config_dir):
            continue
        pipe_path = ensure_pipe(user_dir)
        d_ok = configure_dolphin_ini(config_dir)
        g_ok = configure_gcpad_ini(config_dir)
        configured.append((user_dir, pipe_path, d_ok, g_ok))
        print(f"Configured {user_dir}:")
        print(f"  Pipe:         {pipe_path}")
        print(f"  Dolphin.ini:  {'updated (SIDevice3 = 6)' if d_ok else 'not found'}")
        print(f"  GCPadNew.ini: {'updated ([GCPad4] -> Pipe/0/' + PIPE_NAME + ')' if g_ok else 'failed'}")

    if not configured:
        print("No Dolphin Config directories found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
