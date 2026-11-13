---
title: "The HTTP transport always checks the Host header, even when bound to 0.0.0.0 in Docker"
author: Isaac
date: 2026-11-13
branch: multi-device
cost: { minutes: 4, tokens: 29862 }
id: l-38963fe4
scope: repo
status: active
replaces: l-9a5ed858
src: agent
touches:
  - ".dockerignore"
  - ".env.example"
  - README.md
  - docs/architecture.md
  - docs/tools.md
  - smoke_test.py
  - devices_test.py
  - server_test.py
  - server.py
  - jetkvm/devices.py
---

## Decided
- `server.py` passes `transport_security(env)` to FastMCP on every bind: Host/Origin must be localhost / 127.0.0.1 / [::1], plus names in `JETKVM_MCP_ALLOWED_HOSTS`. FastMCP turns the check on by itself only for a 127.0.0.1 bind, and the Docker container binds 0.0.0.0, so without this a web page that rebinds its domain to 127.0.0.1 could send keystrokes and power actions to every device. Publishing the port on 127.0.0.1 does not stop DNS rebinding.
- Requests without an Origin header (CLI MCP clients) still pass; mcp only checks Origin when present.
- `mcp[cli]>=1.10.0` in `requirements.txt`: `TransportSecuritySettings` doesn't exist before 1.10.0.
## Tried and ruled out
- Relying on FastMCP's default plus a 127.0.0.1 port publish: the default is off for 0.0.0.0, and rebinding arrives via the browser on localhost anyway.
In the code: `server.py` `transport_security`, `server_test.py` (0.0.0.0 bind subprocess check: foreign Host → 421, localhost → 200).
