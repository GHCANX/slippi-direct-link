# slippi-direct-link

First-class click-to-connect deep linking for Super Smash Bros. Melee / Slippi Direct & Teams mode via the `slippi://` URI scheme:

```text
slippi://connect?code=FOX-123&mode=direct
```

This umbrella repository tracks the cross-repo architecture, protocol specification ([`SPEC.md`](./SPEC.md)), and implementation milestones across the Slippi stack.

## Forked Prerequisite Repositories

| Component | Fork | Upstream | Role |
| :--- | :--- | :--- | :--- |
| **Slippi Launcher** | [GHCANX/slippi-launcher](https://github.com/GHCANX/slippi-launcher) | [project-slippi/slippi-launcher](https://github.com/project-slippi/slippi-launcher) | Extend `slippi://` handler in `src/main/main.ts`, confirm & validate code, write `pending-connect.json`, launch/focus Netplay Dolphin |
| **Slippi Rust Extensions** | [GHCANX/slippi-rust-extensions](https://github.com/GHCANX/slippi-rust-extensions) | [project-slippi/slippi-rust-extensions](https://github.com/project-slippi/slippi-rust-extensions) | Watch/load `pending-connect.json`, track `commandId` consumption & TTL, expose FFI to Dolphin |
| **Slippi Dolphin (Mainline Beta)** | [GHCANX/dolphin](https://github.com/GHCANX/dolphin) | [project-slippi/dolphin](https://github.com/project-slippi/dolphin) | Wire `CEXISlippi` (`EXI_DeviceSlippi.cpp`) commands `CMD_GET_PENDING_CONNECT` / `CMD_CONSUME_PENDING_CONNECT` |
| **Slippi Ishiiruka (Stable)** | [GHCANX/Ishiiruka](https://github.com/GHCANX/Ishiiruka) | [project-slippi/Ishiiruka](https://github.com/project-slippi/Ishiiruka) | Mirror the same `CEXISlippi` EXI commands for stable Slippi Dolphin builds |
| **Slippi SSBM ASM** | [GHCANX/slippi-ssbm-asm](https://github.com/GHCANX/slippi-ssbm-asm) | [project-slippi/slippi-ssbm-asm](https://github.com/project-slippi/slippi-ssbm-asm) | Poll pending connect code on CSS / Main Menu, display banner, skip Name Entry on Start, and consume on match start/cancel |

## Implementation Roadmap

1. **Phase 1 — Rust Extensions & Dolphin (`slippi-rust-extensions`, `dolphin`, `Ishiiruka`)**
   - Add `pending-connect.json` reader + TTL/dedup state in Rust.
   - Expose `CMD_GET_PENDING_CONNECT` (`0xC5`) and `CMD_CONSUME_PENDING_CONNECT` (`0xC6`) over EXI.
   - Also surface pending code at index `0` of `CMD_FETCH_CODE_SUGGESTION` (`0xBE`) for graceful fallback on older ASM.
2. **Phase 2 — Slippi Launcher (`slippi-launcher`)**
   - Route `slippi://connect?code=...&mode=...` in `handleSlippiURIAsync`.
   - Add strict validation + confirmation modal.
   - Atomically write `pending-connect.json` and launch or focus Netplay Dolphin.
3. **Phase 3 — Melee PowerPC ASM (`slippi-ssbm-asm`)**
   - **Tier 1:** Display pending opponent banner on Direct/Teams CSS and connect directly on Start (bypassing Name Entry).
   - **Tier 2:** Auto-navigate from Main Menu / Online Submenu into Direct/Teams CSS when logged in.

See [`SPEC.md`](./SPEC.md) for exact URI rules, JSON schema, EXI packet layouts, and state-machine behavior.
