---
title: Several JetKVMs are configured in one JSON file and served from one HTTP container
author: Isaac
date: 2026-11-13
branch: multi-device
cost: { minutes: 33, tokens: 214993 }
id: l-9a5ed858
scope: repo
status: active
src: agent
touches:
  - requirements.txt
  - server.py
  - server_test.py
  - jetkvm/pool.py
  - pool_test.py
  - devices_test.py
  - jetkvm/devices.py
  - docs/superpowers/plans/2026-11-13-multi-device.md
  - smoke_test.py
  - README.md
---

## Decided
- Devices are named in a JSON file pointed at by `JETKVM_CONFIG` (`jetkvm/devices.py`); the old `JETKVM_URL` env vars become one device named `default`. Setting both is a startup error, so it's never ambiguous which one wins.
- With more than one device configured, a tool call that omits `device` is refused before anything is sent (`DevicePool.resolve` in `jetkvm/pool.py`). There is deliberately no default device: `power` / `reboot_device` must never land on the wrong machine by guess.
- Keyboard layout lives in the pool per device, not on the client and not in `keymap`'s module global, so it survives reconnects and two targets with different layouts don't clobber each other.
- `JETKVM_MCP_TRANSPORT=http` serves streamable HTTP at `/mcp` so one long-running container serves every LLM client. Reason: the firmware allows one WebRTC session per device and a new one displaces the old (`otherSessionConnected`), so one stdio container per client would make clients knock each other off the same device.
## Tried and ruled out
- One inline-JSON env var for all devices: passwords show up in `docker inspect`, and JSON quoted inside Claude Desktop's JSON args is unreadable.
- Per-device env prefixes (`JETKVM_LAB_URL`): they collide with the existing `JETKVM_VERIFY_TLS` / `JETKVM_KEYBOARD_LAYOUT` names.
## Watch out
- The HTTP endpoint has no auth and exposes power/reboot; publish it on `127.0.0.1` only. Inside the container bind `0.0.0.0` — mcp only turns on DNS-rebinding protection for 127.0.0.1/localhost binds, so the published localhost port works.
- The container's `appuser` is uid 1000 and must be able to read the mounted `devices.json`.
In the code: `jetkvm/devices.py`, `jetkvm/pool.py`, `server.py` (`transport_settings`, `Device` annotation), `devices_test.py`, `pool_test.py`, `server_test.py`.
