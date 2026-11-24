# slippi-direct-link

First-class click-to-connect deep linking for Super Smash Bros. Melee / Slippi Direct & Teams mode via the `slippi://` URI scheme:

```text
slippi://connect?code=FOX-123&mode=direct
```

This umbrella repository tracks the cross-repo architecture, protocol specification ([`SPEC.md`](./SPEC.md)), test web page ([`demo/index.html`](./demo/index.html)), and simulation CLI tool ([`tools/simulate_link.py`](./tools/simulate_link.py)).

## Architecture Overview

```text
Web Page Link (<a href="slippi://connect?code=FOX-123&mode=direct">)
   │  OS custom URI handler dispatch (open-url / second-instance / argv)
   ▼
Slippi Launcher (GHCANX/slippi-launcher: src/main/main.ts + src/dolphin/manager.ts)
   │  • Validates & normalizes code ("fox-123" -> "FOX#123", mode: "direct" | "teams")
   │  • Atomically writes pending-connect.json into Netplay Slippi directory
   │  • Launches Netplay Dolphin if closed (or focuses if running)
   ▼
Slippi Rust Extensions (GHCANX/slippi-rust-extensions: user/src/pending_connect.rs + ffi/src/user.rs)
   │  • Watches/loads pending-connect.json with TTL (120s) and deduplication
   │  • Exposes slprs_user_get_pending_connect and slprs_user_consume_pending_connect FFI
   ▼
Slippi Dolphin (GHCANX/dolphin: Source/Core/Core/HW/EXI/EXI_DeviceSlippi.cpp)
   │  • Implements CMD_GET_PENDING_CONNECT (0xC5) -> returns 24-byte 3-stride Shift-JIS buffer
   │  • Implements CMD_CONSUME_PENDING_CONNECT (0xC6) -> marks consumed & deletes IPC file
   │  • Provides fallback suggestion for CMD_FETCH_CODE_SUGGESTION (0xBE)
   ▼
Melee PowerPC ASM (GHCANX/slippi-ssbm-asm)
   │  • Online/Menus/TitleMenu/OnMenuPrep.asm: Auto-navigates from Title to Direct CSS
   │  • Online/Menus/CSS/HandleInputsOnCSS.asm: On Start, copies 24 bytes to 0x804A0740,
   │    consumes pending connect, and directly launches matchmaking (bypassing Name Entry)
```

## Implemented Repositories & Commits

| Component | Repository | Branch | Key Changes |
| :--- | :--- | :--- | :--- |
| **Rust Extensions** | [GHCANX/slippi-rust-extensions](https://github.com/GHCANX/slippi-rust-extensions) | `main` | `PendingConnectManager` with TTL/dedup, unit tests, FFI functions |
| **Dolphin** | [GHCANX/dolphin](https://github.com/GHCANX/dolphin) | `slippi` | `CMD_GET_PENDING_CONNECT` (0xC5), `CMD_CONSUME_PENDING_CONNECT` (0xC6), Shift-JIS buffer |
| **Launcher** | [GHCANX/slippi-launcher](https://github.com/GHCANX/slippi-launcher) | `main` | `slippi://connect` URI handler in `main.ts`, IPC writer, `isNetplayDolphinRunning` check |
| **Melee ASM** | [GHCANX/slippi-ssbm-asm](https://github.com/GHCANX/slippi-ssbm-asm) | `master` | Direct connect bypass in `HandleInputsOnCSS.asm`, Title auto-nav in `OnMenuPrep.asm` |

## Quick Testing

### 1. Test CLI Simulation
```bash
./tools/simulate_link.py "slippi://connect?code=FOX-123&mode=direct"
```

### 2. Test Demo Web Page
Open `demo/index.html` in any browser to generate and test custom links with one click.
