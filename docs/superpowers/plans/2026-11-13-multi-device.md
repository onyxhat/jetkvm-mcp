# Multi-Device JetKVM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One MCP server — deployable as one long-running Docker container — lets any number of LLM clients drive every JetKVM the user owns, each tool call naming the device it targets.

**Architecture:** A JSON file (path in `JETKVM_CONFIG`) names the devices; `jetkvm/devices.py` loads and validates it, falling back to today's `JETKVM_URL` env vars as a single device named `default`. `jetkvm/pool.py` replaces `server.py`'s single global client with one lazily-connected client, one lock and one keyboard layout per device. Every tool gains an optional `device` argument, and the server can serve MCP over streamable HTTP so several LLM clients share one container (and so one WebRTC session per device).

**Tech Stack:** Python 3.12, FastMCP (`mcp[cli]`), aiortc, asyncio, pydantic `Field` (already pulled in by `mcp`), Docker.

**Spec:** No separate design doc — the user asked to go straight from the 2026-11-13 conversation to a plan. The "Design decisions" section below is the spec; change it there first if a decision is wrong.

## Design decisions

1. **Config is a JSON file**, path in `JETKVM_CONFIG`, mounted read-only into the container:

   ```json
   {
     "devices": {
       "office": {"url": "http://192.168.1.50"},
       "lab":    {"url": "https://10.0.0.7", "password": "s3cret", "verify_tls": true, "keyboard_layout": "de"}
     }
   }
   ```

   Per-device keys: `url` (required, `http://` or `https://`), `password` (default `""`), `verify_tls` (JSON bool, default `false`), `keyboard_layout` (default `"us"`). Any other key is an error. Device names match `^[a-z0-9][a-z0-9_-]{0,31}$`.
   Rejected: one inline-JSON env var (passwords show in `docker inspect`, and JSON-in-JSON quoting in Claude Desktop config is miserable); per-device env prefixes like `JETKVM_LAB_URL` (they collide with the existing `JETKVM_VERIFY_TLS` / `JETKVM_KEYBOARD_LAYOUT` names).
2. **Backward compatible.** With no `JETKVM_CONFIG`, the existing `JETKVM_URL` / `JETKVM_PASSWORD` / `JETKVM_VERIFY_TLS` / `JETKVM_KEYBOARD_LAYOUT` env vars define one device named `default`. Setting both `JETKVM_CONFIG` and `JETKVM_URL` is a startup error; setting neither is a startup error. Config errors fail at startup, not on the first tool call.
3. **No implicit default with several devices.** `device` may be omitted only when exactly one device is configured; otherwise the call fails with the list of names and nothing is sent. This keeps `power` / `reboot_device` from hitting the wrong machine; the LLM recovers by calling `list_devices`. Device lookups ignore case and surrounding whitespace.
4. **Connections are lazy and independent:** one client and one `asyncio.Lock` per device, so a device that is offline or mid-reboot never stalls calls to the others. The ping-then-reconnect behaviour of today's `kvm()` (`server.py:44-70`) is kept per device.
5. **Keyboard layout is per device**, initialised from config, changed with `keyboard_layout(layout, device)`, held by the pool so it survives reconnects, not persisted across restarts. `jetkvm/keymap.py`'s module-level `set_layout` stays (its tests use it) but the server stops using it.
6. **Transport:** `JETKVM_MCP_TRANSPORT` = `stdio` (default, unchanged) | `http` (streamable HTTP at `/mcp`). `JETKVM_MCP_HOST` (default `127.0.0.1`) and `JETKVM_MCP_PORT` (default `8000`). Why: the firmware runs one WebRTC session per device and a new one displaces the old (`otherSessionConnected`, see `docs/architecture.md`), so per-client stdio containers would knock each other off the same device. A single HTTP container shares one session per device across all LLM clients. The endpoint has no auth and exposes power/reboot, so the docs publish it on `127.0.0.1` only.
7. **Non-goals:** hot-reloading the config, `password_env` indirection, idle disconnects, MCP-level auth, serialising two LLMs driving the *same* device at once (documented as a caveat).

## Global Constraints

- Existing single-device setups (env vars only, stdio) keep working with no config change, and tools stay callable without `device` when one device is configured.
- No new dependencies. One version-floor bump: `mcp[cli]>=1.8.0,<2` in `requirements.txt` (streamable HTTP transport).
- Passwords never appear in tool output, logs or `repr`.
- Tests are offline, standalone scripts in the repo root following `keymap_test.py`'s pattern (`failures` list, `check()`, print `ok — …` or exit 1), run with `python <name>_test.py`. No pytest. (This follows the repo's convention over the global `/tests` rule.)
- Python files stay under 500 lines.
- Work on a branch `multi-device`. `.gitignore` and `.ignore` already carry uncommitted graft-cache changes that are not part of this work — never stage them.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- A typo'd key in a device entry (`"pasword"`) must fail at startup naming the key and the device, not connect silently without a password. → Task 1 test.
- `"verify_tls": "false"` (a string, which Python treats as truthy) must be rejected, not read as true. → Task 1 test.
- The LLM omits `device` on a destructive tool while several devices are configured: expect an error listing the names and **no** RPC sent to any device. → Task 3 test.
- One device offline or slow to connect while others are in use: expect the others to answer immediately, and a failed connect to be retried on the next call rather than cached. → Task 2 tests.
- The config file is bind-mounted with permissions the container's `appuser` can't read: expect a startup error naming the path. → Task 1 test.

## File structure

| File | Change | Responsibility |
|---|---|---|
| `jetkvm/devices.py` | create | `DeviceConfig`, `ConfigError`, `load_devices(env)` |
| `jetkvm/pool.py` | create | `DevicePool`, `DeviceError` — per-device clients, locks, layouts |
| `server.py` | modify | build the pool at import, `device` arg on every tool, `list_devices`, transport selection |
| `devices_test.py`, `pool_test.py`, `server_test.py` | create | offline checks |
| `smoke_test.py` | modify | accept a device name as well as a URL |
| `requirements.txt` | modify | `mcp` floor bump |
| `README.md`, `docs/tools.md`, `docs/architecture.md`, `.env.example` | modify | multi-device + HTTP docs |
| `.gitignore`, `.dockerignore` | modify | ignore `devices.json` (holds passwords) |

`Dockerfile` needs no change: it already copies all of `jetkvm/`.

---

### Task 1: Device config loading

**Files:**
- Create: `jetkvm/devices.py`
- Test: `devices_test.py`

**Interfaces:**
- Consumes: `keymap.normalize_layout(name: str) -> str` (raises `ValueError` on unknown layouts; `jetkvm/keymap.py:175`).
- Produces:
  - `class ConfigError(ValueError)`
  - `DEFAULT_DEVICE = "default"`
  - `@dataclass(frozen=True) class DeviceConfig: name: str; url: str; password: str = field(default="", repr=False); verify_tls: bool = False; keyboard_layout: str = "us"`
  - `def load_devices(env: Mapping[str, str]) -> dict[str, DeviceConfig]` — keys in file order; `keyboard_layout` already normalised.

- [ ] **Step 0: Baseline environment**

```bash
git switch -c multi-device
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # needs FFmpeg dev libs, see README
.venv/bin/python keymap_test.py && .venv/bin/python signaling_test.py
```
Expected: both print `ok …`. If the local FFmpeg build is a problem, build the image (`docker build -t jetkvm-mcp .`) and run every test in this plan as `docker run --rm -v "$PWD":/src -w /src jetkvm-mcp python <name>_test.py`.

- [ ] **Step 1: Write the failing test `devices_test.py`**

Module docstring in the style of `keymap_test.py`. Helpers: `config_file(obj) -> str` writes a dict as JSON (or a str raw) to a temp file and returns its path; `expect_error(env, *fragments)` records a failure unless `load_devices(env)` raises `ConfigError` whose message contains every fragment. Checks:

```python
d = load_devices({"JETKVM_URL": "http://10.0.0.5", "JETKVM_PASSWORD": "pw",
                  "JETKVM_VERIFY_TLS": "TRUE", "JETKVM_KEYBOARD_LAYOUT": "German"})
check(d == {"default": DeviceConfig("default", "http://10.0.0.5", "pw", True, "de")}, "legacy env → one 'default' device")
check(load_devices({"JETKVM_URL": "http://10.0.0.5"})["default"] == DeviceConfig("default", "http://10.0.0.5"), "legacy defaults")

good = config_file({"devices": {
    "office": {"url": "http://192.168.1.50"},
    "lab": {"url": "https://10.0.0.7", "password": "s3cret", "verify_tls": True, "keyboard_layout": "de"}}})
d = load_devices({"JETKVM_CONFIG": good})
check(list(d) == ["office", "lab"], "file order kept")
check(d["office"] == DeviceConfig("office", "http://192.168.1.50"), "defaults applied")
check(d["lab"] == DeviceConfig("lab", "https://10.0.0.7", "s3cret", True, "de"), "all fields read")
check("s3cret" not in repr(d["lab"]), "password kept out of repr")

expect_error({}, "JETKVM_CONFIG", "JETKVM_URL")
expect_error({"JETKVM_CONFIG": good, "JETKVM_URL": "http://x"}, "not both")
expect_error({"JETKVM_CONFIG": "/nonexistent/devices.json"}, "/nonexistent/devices.json")
bad_json = config_file("{not json"); expect_error({"JETKVM_CONFIG": bad_json}, bad_json)
expect_error({"JETKVM_CONFIG": config_file({"devices": {}})}, "no devices")
expect_error({"JETKVM_CONFIG": config_file({"devices": {"Lab 1": {"url": "http://x"}}})}, "'Lab 1'")
expect_error({"JETKVM_CONFIG": config_file({"devices": {"lab": {}}})}, "lab", "url")
expect_error({"JETKVM_CONFIG": config_file({"devices": {"lab": {"url": "192.168.1.5"}}})}, "lab", "http://")
expect_error({"JETKVM_CONFIG": config_file({"devices": {"lab": {"url": "http://x", "pasword": "pw"}}})}, "lab", "pasword")
expect_error({"JETKVM_CONFIG": config_file({"devices": {"lab": {"url": "http://x", "verify_tls": "false"}}})}, "lab", "verify_tls")
expect_error({"JETKVM_CONFIG": config_file({"devices": {"lab": {"url": "http://x", "keyboard_layout": "klingon"}}})}, "lab", "klingon")
expect_error({"JETKVM_URL": "http://x", "JETKVM_KEYBOARD_LAYOUT": "klingon"}, "klingon")
if os.geteuid() != 0:  # root reads anything
    locked = config_file({"devices": {"lab": {"url": "http://x"}}}); os.chmod(locked, 0)
    expect_error({"JETKVM_CONFIG": locked}, locked)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python devices_test.py`
Expected: `ModuleNotFoundError: No module named 'jetkvm.devices'`

- [ ] **Step 3: Implement `load_devices(env: Mapping[str, str]) -> dict[str, DeviceConfig]` in `jetkvm/devices.py`**

Read the file and catch `OSError` / `json.JSONDecodeError` into `ConfigError` messages that include the path. Validate as in Design decision 1 (unknown keys, `isinstance(verify_tls, bool)`, scheme prefix, name regex). Layouts go through `keymap.normalize_layout`, re-raised as `ConfigError` naming the device. Legacy env: `verify_tls = env.get("JETKVM_VERIFY_TLS", "").lower() == "true"` (same rule as `server.py:62` today), and a blank `JETKVM_KEYBOARD_LAYOUT` means `"us"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python devices_test.py && .venv/bin/python keymap_test.py`
Expected: both print `ok …`

- [ ] **Step 5: Commit**

```bash
git add jetkvm/devices.py devices_test.py
git commit -m "Load multiple JetKVM devices from a JSON config file"
```

---

### Task 2: Per-device connection pool

**Files:**
- Create: `jetkvm/pool.py`
- Test: `pool_test.py`

**Interfaces:**
- Consumes: `DeviceConfig` (Task 1); `JetKVMClient(base_url, password, verify_tls)` with `await connect()`, `await ping() -> bool`, `await close()`, `connected` property (`jetkvm/client.py:61-166`); `keymap.normalize_layout`.
- Produces:
  - `class DeviceError(ValueError)`
  - `ClientFactory = Callable[[DeviceConfig], JetKVMClient]`
  - `class DevicePool`:
    - `__init__(self, devices: Mapping[str, DeviceConfig], factory: ClientFactory | None = None)` — default factory builds a `JetKVMClient` from the config.
    - `names(self) -> list[str]`
    - `resolve(self, device: str = "") -> str`
    - `async client(self, device: str = "") -> JetKVMClient`
    - `layout(self, device: str = "") -> str`
    - `set_layout(self, device: str, layout: str) -> str` — returns the normalised name; raises `ValueError` and leaves the old layout on an unknown one.
    - `describe(self) -> list[dict]` — one row per device, keys exactly `name`, `url`, `keyboard_layout`, `connected`; no network I/O.
    - `async close(self) -> None`
  - Error copy, exactly:
    - unknown: `unknown JetKVM device 'nope'; configured: office, lab`
    - omitted with several: `several JetKVM devices are configured (office, lab); pass device=<name> (list_devices describes them)`

- [ ] **Step 1: Write the failing test `pool_test.py`**

A `FakeClient(cfg)` with attributes `cfg`, `alive=True`, `closed=False`, `connects=0`, `gate: asyncio.Event | None` (connect awaits it when set), `fail=False` (connect raises `RuntimeError` when set); `connected` is `connects > 0 and alive and not closed`; `ping()` returns `alive`. A `factory` appends each new fake to `made` and applies attrs from `behave.pop(cfg.name, {})`. `expect(exc, fn, *fragments)` checks the raised type and message. Everything runs in `asyncio.run(main())`:

```python
OFFICE = DeviceConfig("office", "http://10.0.0.1")
LAB = DeviceConfig("lab", "http://10.0.0.2", keyboard_layout="de")

check(DevicePool({"office": OFFICE}, factory).resolve("") == "office", "a single device is implied")
pool = DevicePool({"office": OFFICE, "lab": LAB}, factory)
expect(DeviceError, lambda: pool.resolve(""), "several JetKVM devices are configured (office, lab)", "device=<name>")
expect(DeviceError, lambda: pool.resolve("nope"), "unknown JetKVM device 'nope'; configured: office, lab")
check(pool.resolve(" LAB ") == "lab", "lookup ignores case and whitespace")

a = await pool.client("office")
check(a is await pool.client("office") and a.connects == 1, "connection reused")
b = await pool.client("lab")
check(b is not a and b.cfg is LAB, "each device has its own client")
a.alive = False
a2 = await pool.client("office")
check(a.closed and a2 is not a, "dead connection replaced")

b.alive = False; behave["lab"] = {"fail": True}
# await pool.client("lab") must raise RuntimeError
check(made[-1].closed, "failed connect is closed, not leaked")
b3 = await pool.client("lab")
check(b3 is made[-1] and b3.connects == 1, "next call retries instead of caching the failure")

gate = asyncio.Event(); behave["office"] = {"gate": gate}
slow_pool = DevicePool({"office": OFFICE, "lab": LAB}, factory)
slow = asyncio.create_task(slow_pool.client("office")); await asyncio.sleep(0)
# await asyncio.wait_for(slow_pool.client("lab"), 1.0) must succeed (TimeoutError → failure)
check(not slow.done(), "lab served while office is still connecting")
gate.set(); await slow

check(pool.layout("lab") == "de" and pool.layout("office") == "us", "layouts come from config")
check(pool.set_layout("office", "French") == "fr" and pool.layout("office") == "fr", "set_layout normalises")
check(pool.layout("lab") == "de", "other device's layout untouched")
expect(ValueError, lambda: pool.set_layout("office", "klingon"), "klingon")
check(pool.layout("office") == "fr", "bad layout keeps the old one")
(await pool.client("office")).alive = False; await pool.client("office")
check(pool.layout("office") == "fr", "layout survives a reconnect")

rows = {r["name"]: r for r in slow_pool.describe()}
check(all(set(r) == {"name", "url", "keyboard_layout", "connected"} for r in rows.values()), "describe has no password")
check(rows["office"]["connected"] and rows["lab"]["connected"], "connected reflects live clients")
check(not DevicePool({"office": OFFICE}, factory).describe()[0]["connected"], "never-connected device reads false")
await slow_pool.close()
check(all(c.closed for c in made[-2:]), "close() closes every client")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python pool_test.py`
Expected: `ModuleNotFoundError: No module named 'jetkvm.pool'`

- [ ] **Step 3: Implement `DevicePool` in `jetkvm/pool.py`**

`client()` is the body of today's `kvm()` (`server.py:49-70`) moved here and keyed by the resolved name: per-device lock, ping, close and rebuild, close and re-raise on a failed connect. The reconnect warning names the device. Locks, clients and layouts are dicts built in `__init__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python pool_test.py && .venv/bin/python devices_test.py`
Expected: both print `ok …`

- [ ] **Step 5: Commit**

```bash
git add jetkvm/pool.py pool_test.py
git commit -m "Pool one lazily-connected client per JetKVM device"
```

---

### Task 3: Route every tool to a named device

**Files:**
- Modify: `server.py` (whole file: docstring `1-19`, globals `35-70`, every tool)
- Test: `server_test.py`

**Interfaces:**
- Consumes: `load_devices`, `ConfigError` (Task 1); `DevicePool`, `DeviceError` (Task 2).
- Produces:
  - module global `pool: DevicePool`, built at import from `load_devices(os.environ)` (a `ConfigError` stops startup).
  - `async def kvm(device: str = "") -> JetKVMClient` → `await pool.client(device)`
  - `Device = Annotated[str, Field(description="Which JetKVM to act on: a name from list_devices. May be omitted only when one device is configured.")]`
  - all 25 existing tools take `device: Device = ""` as their **last** parameter.
  - `keyboard_layout(layout: str = "", device: Device = "") -> dict` → `{"device": <name>, "active": <layout>, "available": keymap.available_layouts()}`
  - new tool `list_devices() -> dict` → `{"devices": pool.describe()}` — 26 tools in total.

- [ ] **Step 1: Write the failing test `server_test.py`**

Before `import server`, write a two-device config (`office`; `lab` with `keyboard_layout: "de"`) to a temp file, set `JETKVM_CONFIG` to it and remove `JETKVM_URL`. Then set `server.pool = DevicePool(load_devices(os.environ), factory)`, where the factory makes a `Recorder` fake per device, stored in `fakes[name]`. The `Recorder` has the `pool_test.py` fake's connect/ping/close/connected, records `(method, args, kwargs)` for every other awaited method in `.calls`, returns `(b"jpeg", 1, 1)` from `snapshot`, `[]` from `type_text` and `{}` from `rpc`. FastMCP's `@mcp.tool()` returns the plain function, so tools can be called directly. Checks:

```python
tools = await server.mcp.list_tools()
check(len(tools) == 26, "25 tools + list_devices")
for t in tools:
    if t.name != "list_devices":
        check("device" in t.inputSchema["properties"] and "device" not in t.inputSchema.get("required", []),
              f"{t.name} takes an optional device")

await server.mcp.call_tool("click", {"x": 3, "y": 4, "device": "lab"})
check(("click", (3, 4, "left"), {}) in fakes["lab"].calls and "office" not in fakes, "click routed to lab only")

# await server.click(3, 4) must raise DeviceError containing "several JetKVM devices"
# await server.power("reset") must raise DeviceError
check(not any(c[0] == "rpc" and c[1][:1] == ("setATXPowerAction",) for f in fakes.values() for c in f.calls),
      "no power action sent when the device is ambiguous")

out = await server.type_text("hi", device="lab")
check(fakes["lab"].calls[-1] == ("type_text", ("hi", "de"), {}) and "(de layout)" in out, "lab types with its own layout")
r = await server.keyboard_layout("fr", device="office")
check(r["device"] == "office" and r["active"] == "fr" and server.pool.layout("lab") == "de", "layout set per device")

listed = await server.list_devices()
check([d["name"] for d in listed["devices"]] == ["office", "lab"] and "password" not in json.dumps(listed), "list_devices")
```

Two subprocess checks with `subprocess.run([sys.executable, "-c", ...], env=..., capture_output=True, text=True)`:
- `JETKVM_CONFIG=/nonexistent/devices.json`, `import server` → non-zero exit and `/nonexistent/devices.json` in stderr.
- legacy env only (`JETKVM_URL=http://10.0.0.9`), `import server; print(server.pool.names())` → stdout `['default']`.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python server_test.py`
Expected: FAIL — `server` has no `pool` (AttributeError) or the tool-count and `device` checks fail.

- [ ] **Step 3: Rewire `server.py`**

Replace `_env_layout`, `_client`, `_lock` and the old `kvm()` body with the `pool` global. Add `device: Device = ""` to every tool and pass it to `kvm(device)`. `type_text` and `press_key` pass `layout or pool.layout(device)` to the client, so the client never falls back to keymap's global. Add `list_devices`; its docstring tells the model to call it first when it doesn't know the device names. Change `click` / `move_mouse` docstrings from "the latest screenshot" to "the latest screenshot of the same device". Update the module docstring's config section to list `JETKVM_CONFIG` alongside the legacy vars.

- [ ] **Step 4: Run tests to verify they pass**

Run: `for t in keymap signaling devices pool server; do .venv/bin/python ${t}_test.py || break; done`
Expected: five `ok …` lines

- [ ] **Step 5: Commit**

```bash
git add server.py server_test.py
git commit -m "Add a device argument to every tool, plus list_devices"
```

---

### Task 4: Streamable HTTP transport

**Files:**
- Modify: `server.py` (`FastMCP(...)` construction and `__main__` block), `requirements.txt`
- Test: `server_test.py`

**Interfaces:**
- Consumes: `ConfigError` (Task 1).
- Produces: `def transport_settings(env: Mapping[str, str]) -> tuple[str, str, int]` in `server.py` → `(transport, host, port)`, where transport is `"stdio"` or `"streamable-http"`. `mcp = FastMCP("jetkvm", host=host, port=port)`; `__main__` calls `mcp.run(transport=transport)`.

- [ ] **Step 1: Add failing checks to `server_test.py`**

```python
check(server.transport_settings({}) == ("stdio", "127.0.0.1", 8000), "stdio by default")
check(server.transport_settings({"JETKVM_MCP_TRANSPORT": "HTTP", "JETKVM_MCP_HOST": "0.0.0.0", "JETKVM_MCP_PORT": "9000"})
      == ("streamable-http", "0.0.0.0", 9000), "http settings")
# transport_settings({"JETKVM_MCP_TRANSPORT": "carrier-pigeon"}) must raise ConfigError naming the value
# transport_settings({"JETKVM_MCP_TRANSPORT": "http", "JETKVM_MCP_PORT": "eighty"}) must raise ConfigError naming the value
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python server_test.py`
Expected: FAIL — `module 'server' has no attribute 'transport_settings'`

- [ ] **Step 3: Implement `transport_settings` and wire it in**

Change `requirements.txt` to `mcp[cli]>=1.8.0,<2` and reinstall. Accept `stdio` / `http` case-insensitively. Port must be an int in 1–65535. Call `transport_settings(os.environ)` at import, beside `load_devices`, so bad values stop startup.

- [ ] **Step 4: Verify the tests and a live HTTP handshake**

Run: `.venv/bin/python server_test.py`, then in the background `JETKVM_URL=http://10.0.0.9 JETKVM_MCP_TRANSPORT=http .venv/bin/python server.py`, then:

```bash
curl -s -X POST http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```
Expected: `ok …` from the test, and the curl response contains `"name":"jetkvm"`. No device is contacted, because connections are lazy. Stop the server afterwards.

- [ ] **Step 5: Commit**

```bash
git add server.py server_test.py requirements.txt
git commit -m "Serve MCP over streamable HTTP so one container can serve many clients"
```

---

### Task 5: Docker deployment, docs and smoke test

**Files:**
- Modify: `smoke_test.py`, `README.md`, `docs/tools.md`, `docs/architecture.md`, `.env.example`, `.gitignore`, `.dockerignore`

**Interfaces:**
- Consumes: everything above. `smoke_test.py` reaches its client through `DevicePool(load_devices(env)).client(name)`.

- [ ] **Step 1: `smoke_test.py` takes a device name or a URL**

Usage becomes `smoke_test.py [device-name | device-url]`. An argument starting with `http://` / `https://` behaves exactly as today (it becomes `JETKVM_URL` with `JETKVM_CONFIG` dropped). Otherwise the argument is a device name, resolved through the pool, so it can be omitted when one device is configured.

- [ ] **Step 2: Keep the config with passwords out of git and out of the image**

Add `devices.json` to `.dockerignore` and `.gitignore`. When staging `.gitignore`, stage only that hunk: the graft hunk already in the working tree is not part of this work (`git diff .gitignore` to see it; stage via a one-hunk patch and `git apply --cached`, or ask the user).

- [ ] **Step 3: Docs**

- `README.md`: a new **"Multiple devices"** section after **Docker** containing:
  - the `devices.json` example from Design decision 1;
  - a stdio `docker run` example with `-v "$PWD/devices.json:/config/devices.json:ro" -e JETKVM_CONFIG=/config/devices.json`;
  - a **shared HTTP container** example: `docker run -d --name jetkvm-mcp --restart unless-stopped -p 127.0.0.1:8000:8000 -v …:ro -e JETKVM_CONFIG=… -e JETKVM_MCP_TRANSPORT=http -e JETKVM_MCP_HOST=0.0.0.0 jetkvm-mcp`, plus the client side: `claude mcp add --transport http jetkvm http://127.0.0.1:8000/mcp`.

  The section also explains:
  - why there should be one container rather than one per client: each device allows only one session at a time;
  - that the HTTP endpoint has no auth and can power off machines, so it should be published on `127.0.0.1` only;
  - that the file must be readable by the container's `appuser` (e.g. `chmod 644`, or give the file to that uid);
  - that two LLMs driving the same device at the same time will interleave their input.

  Elsewhere in the README: add `list_devices` to the tools table, change "25 tools" to "26 tools", and add `jetkvm/devices.py`, `jetkvm/pool.py` and the three new tests to the Development list.
- `docs/tools.md`: change "All 25" to "All 26". Add a note that every tool takes an optional `device`. Add a `list_devices` row. Make `keyboard_layout` per device. Add a line to the destructive-tool checklist saying that with several devices these tools refuse to run without a device name.
- `docs/architecture.md` §"Reconnect / device restarts": "one cached connection" becomes one cached connection and lock per device in `DevicePool`.
- `.env.example`: a commented `JETKVM_CONFIG=/config/devices.json` block, plus `JETKVM_MCP_TRANSPORT` / `JETKVM_MCP_HOST` / `JETKVM_MCP_PORT`, each with a one-line comment.

- [ ] **Step 4: Verify in the container**

```bash
docker build -t jetkvm-mcp .
docker run --rm -v "$PWD":/src:ro -w /src jetkvm-mcp sh -c 'for t in keymap signaling devices pool server; do python ${t}_test.py || exit 1; done'
docker run --rm -v <scratch>/devices.json:/config/devices.json:ro -e JETKVM_CONFIG=/config/devices.json \
  jetkvm-mcp python -c "import server; print(server.pool.names())"
```
Expected: five `ok …` lines, then the device names from the scratch config, which proves `appuser` can read a mounted config. If real devices are reachable, also run `docker run --rm -v …:ro -e JETKVM_CONFIG=… jetkvm-mcp python smoke_test.py <name>` once per device. Expected: `connected.` and four snapshots each.

- [ ] **Step 5: Record the decisions, check against learnings, commit**

```bash
trail note --title "Several JetKVMs are configured in one JSON file and served from one HTTP container"   # body: Design decisions 1, 3 and 6 with their reasons, under ## Decided / ## Tried and ruled out
trail check
git add smoke_test.py README.md docs/tools.md docs/architecture.md .env.example .dockerignore .trail/learnings
git commit -m "Document multi-device and shared-container deployment"
```
Expected: `trail check` exits 0 before the commit.
