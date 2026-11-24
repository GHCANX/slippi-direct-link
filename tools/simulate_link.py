#!/usr/bin/env python3
"""
Slippi Direct Link Simulation & Verification Tool
Tests URI parsing, normalization, and atomic IPC file writing into Slippi config directory.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import uuid

CONNECT_CODE_REGEX = re.compile(r"^[A-Z0-9]{1,7}#[0-9]{1,4}$")

def normalize_connect_code(raw: str) -> str:
    if not raw:
        return None
    cleaned = raw.strip().replace("-", "#").upper()
    if len(cleaned) > 8 or "#" not in cleaned:
        return None
    if not CONNECT_CODE_REGEX.match(cleaned):
        return None
    return cleaned

def get_default_slippi_dir() -> str:
    # Linux
    if sys.platform.startswith("linux"):
        return os.path.expanduser("~/.config/SlippiOnline/Slippi")
    # macOS
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/com.project-slippi.dolphin/Slippi")
    # Windows
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return os.path.join(appdata, "Slippi Launcher", "netplay", "User", "Slippi")
    return os.path.expanduser("~/.config/SlippiOnline/Slippi")

def parse_uri(uri_str: str):
    parsed = urllib.parse.urlparse(uri_str)
    if parsed.scheme != "slippi":
        raise ValueError(f"Invalid URI scheme: '{parsed.scheme}', expected 'slippi'")
    if parsed.netloc != "connect" and parsed.path != "connect" and parsed.path != "//connect":
        raise ValueError(f"Invalid host/path: '{parsed.netloc}{parsed.path}', expected 'connect'")

    params = urllib.parse.parse_qs(parsed.query)
    raw_code = params.get("code", [None])[0]
    raw_mode = params.get("mode", ["direct"])[0]

    code = normalize_connect_code(raw_code)
    if not code:
        raise ValueError(f"Invalid connect code: '{raw_code}'")

    mode = "teams" if raw_mode.lower() in ("teams", "3") else "direct"
    return code, mode

def main():
    parser = argparse.ArgumentParser(description="Simulate/test slippi://connect URI dispatch.")
    parser.add_argument("uri", help="URI string, e.g. 'slippi://connect?code=FOX-123&mode=direct'")
    parser.add_argument("--dir", default=None, help="Custom Slippi config directory (default: auto-detected)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate only, do not write file")

    args = parser.parse_args()

    print(f"[*] Input URI: {args.uri}")
    try:
        code, mode = parse_uri(args.uri)
    except Exception as e:
        print(f"[!] Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[+] Normalized Connect Code: {code}")
    print(f"[+] Target Online Mode:      {mode}")

    target_dir = args.dir or get_default_slippi_dir()
    print(f"[*] Slippi Directory:        {target_dir}")

    payload = {
        "commandId": str(uuid.uuid4()),
        "mode": mode,
        "connectCode": code,
        "createdAt": int(time.time()),
        "ttlSeconds": 120
    }

    print(f"[*] Payload:")
    print(json.dumps(payload, indent=2))

    if args.dry_run:
        print("[+] Dry-run completed successfully.")
        return

    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, "pending-connect.json")
    tmp_path = f"{target_path}.{int(time.time())}.tmp"

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    os.replace(tmp_path, target_path)
    print(f"[✓] Atomic IPC write completed: {target_path}")

    # Verify
    with open(target_path, "r", encoding="utf-8") as f:
        verified = json.load(f)
    assert verified["connectCode"] == code
    print("[✓] Verification passed.")

if __name__ == "__main__":
    main()
