# Protocol & Architecture Specification (`slippi://connect`)

## 1. URI Scheme

```text
slippi://connect?code=<CODE>[&mode=direct|teams][&source=<LABEL>]
```

### Normalization & Validation
- Browsers treat `#` as a URL fragment delimiter, so websites should emit `-` (e.g. `FOX-123`) or percent-encoded `%23` (`FOX%23123`).
- Normalization pipeline (enforced in both Launcher and Dolphin/Rust):
  1. Trim whitespace and convert ASCII letters to uppercase.
  2. Replace `-` with `#`.
  3. Validate against `^[A-Z0-9]{1,7}#[0-9]{1,4}$` **and** `length <= 8`.
  4. Validate `mode` is `direct` (default, `2`) or `teams` (`3`).
- Invalid URIs must be rejected silently/logged without writing any IPC files.

---

## 2. Launcher → Dolphin IPC File (`pending-connect.json`)

Stored in the Netplay Slippi config folder alongside `direct-codes.json` and `user.json`:
- Linux: `~/.config/SlippiOnline/Slippi/pending-connect.json` (or active Slippi user dir)
- Windows: `%APPDATA%\Slippi Launcher\netplay\User\Slippi\pending-connect.json`
- macOS: `~/Library/Application Support/Slippi Launcher/netplay/User/Slippi/pending-connect.json`

### Schema
```json
{
  "commandId": "550e8400-e29b-41d4-a716-446655440000",
  "mode": "direct",
  "connectCode": "FOX#123",
  "createdAt": 1795495205,
  "ttlSeconds": 120
}
```

### Write & Read Semantics
- **Atomic writes:** Launcher writes to `pending-connect.json.tmp` and renames into place.
- **Deduplication:** Dolphin tracks `last_consumed_command_id` in memory so the same request never triggers twice.
- **Expiry:** Requests older than `createdAt + ttlSeconds` (default `120s`, clamped to max `300s`) are ignored and cleaned up.
- **Cleanup:** When consumed or cancelled by Melee, Dolphin marks `commandId` consumed and removes `pending-connect.json`.

---

## 3. EXI Protocol (`CEXISlippi`)

### `CMD_GET_PENDING_CONNECT` (`0xC5`)
- **Write payload:** `0` bytes (command byte only)
- **Read response (`27` bytes, `#pragma pack(1)`):**

| Offset | Size | Type | Field | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `0x00` | 1 | `u8` | `has_pending` | `0` = none, `1` = pending request active |
| `0x01` | 1 | `u8` | `mode` | `2` = Direct (`ONLINE_MODE_DIRECT`), `3` = Teams (`ONLINE_MODE_TEAMS`) |
| `0x02` | 24 | `u8[24]` | `code_buf` | Fullwidth Shift-JIS with 3-byte stride (`[b0, b1, 0x00] * 8`), directly `memcpy`-able to `0x804A0740` |
| `0x1A` | 1 | `u8` | `char_len` | Number of glyphs (`1..8`) |

### `CMD_CONSUME_PENDING_CONNECT` (`0xC6`)
- **Write payload:** `1` byte (`reason`: `1` = started matchmaking, `2` = user cancelled)
- **Read response:** none

### Backwards-Compatible Autocomplete Fallback
- While a pending request is active and unconsumed, `CMD_FETCH_CODE_SUGGESTION` (`0xBE`) with `CONST_ScrollReset` (`3`) returns the pending code at index `0`.

---

## 4. Melee ASM Integration (`slippi-ssbm-asm`)

### Tier 1 — Direct/Teams CSS Bypass
- **Files:** `Online/Menus/CSS/HandleInputsOnCSS.asm`, `Online/Menus/CSS/LoadCSSText.asm`, `Online/Online.s`
- Poll `CMD_GET_PENDING_CONNECT` every ~15–30 frames while `MSRB_CONNECTION_STATE == MM_STATE_IDLE`.
- When active:
  - Display status text (`"Connect to <CODE>"`, `"Press START to search"`, `"Press Z to cancel"`).
  - Pressing **Z** clears pending state and sends `CMD_CONSUME_PENDING_CONNECT(2)`.
  - Pressing **Start** (with character selected, `-0x49A9(r13) != 0`) copies `code_buf` to `0x804A0740`, sends `CMD_CONSUME_PENDING_CONNECT(1)`, and calls `FN_LOCK_IN_AND_SEARCH` directly (skipping `FN_LOAD_CODE_ENTRY`).

### Tier 2 — Main Menu / Online Submenu Auto-Navigation
- **File:** `Online/Menus/TitleMenu/OnMenuPrep.asm`
- When logged in (`OFST_R13_APP_STATE == 1`) and not inside a modal dialog:
  - Poll `CMD_GET_PENDING_CONNECT` every ~30 frames.
  - On `has_pending == 1`, set `OFST_R13_ONLINE_MODE(r13) = mode`, ensure active controller port is stored via `CSS_StoreSinglePlayerPortNumber`, play confirm SFX, and transition to `0x8` (`SCENE_ONLINE_CSS`) via `Event_StoreSceneNumber`.

---

## 5. Safety & State-Machine Guarantees

- **Never interrupt active matches:** Polling only occurs on Main Menu / Online Submenu and Idle CSS (`MM_STATE_IDLE`).
- **Never force-disconnect an opponent:** If already connected/searching, the pending code waits until the user disconnects (or expires via TTL).
- **Launcher confirmation gate:** `slippi-launcher` prompts the user before dispatching a `slippi://connect` request from an external browser link.

---

## 6. Live Process Memory Injection (Zero-Restart Active Session Bridge)

For instances where Melee is already open and running in an active Dolphin session:
1. **Target Discovery:**
   - Scans `/proc/[0-9]*/cmdline` and `/proc/[pid]/maps` for Dolphin processes (`dolphin-emu`, `AppRun.wrapped`, `Slippi_Online`).
   - Identifies the 32MB/24MB emulated GameCube RAM block (`0x80000000` guest base) by validating the Melee boot header `GALE01\x00\x02` and DVD magic `0xC2339F3D` at offset `0x1C`.
2. **Memory Buffer Injection:**
   - Converts the connect code to 24-byte fullwidth Shift-JIS with 3-byte stride (`[byte0, byte1, 0x00]`).
   - Writes directly to:
     - Primary Connect Code buffer: `0x804A0740`
     - Secondary Connect Code buffer: `0x804A0760`
     - Menu Flow Data: `0x804A04F0` (sets `MFD_HOVERED_OPTION = 57` to hover the Confirm button).
3. **Window Activation:**
   - Focuses the Dolphin window using Sway IPC (`swaymsg '[title="(?i)Faster Melee"] focus'`) or X11/Wayland window managers.
   - Result: instantaneous in-game updates while Melee remains running without emulator restarts.
