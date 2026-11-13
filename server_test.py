"""Offline check of the server's device routing — no device needed.

Every tool takes an optional `device`; with several devices configured, a call
that omits it must be refused before anything reaches any device — the tools
include power and reboot.

    python server_test.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    if not cond:
        failures.append(what)


# The server loads its devices at import, so the config has to exist first.
fd, CONFIG = tempfile.mkstemp(suffix=".json")
with os.fdopen(fd, "w") as f:
    json.dump({"devices": {
        "office": {"url": "http://10.0.0.1", "password": "s3cret"},
        "lab": {"url": "http://10.0.0.2", "keyboard_layout": "de"},
    }}, f)
os.environ["JETKVM_CONFIG"] = CONFIG
os.environ.pop("JETKVM_URL", None)

import server  # noqa: E402
from jetkvm.devices import ConfigError, load_devices  # noqa: E402
from jetkvm.pool import DeviceError, DevicePool  # noqa: E402


class Recorder:
    """Stands in for JetKVMClient and records every other awaited call."""

    _RESULTS = {"snapshot": (b"jpeg", 1, 1), "type_text": [], "rpc": {}}

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls: list[tuple] = []
        self.connected = True

    async def connect(self):
        pass

    async def ping(self):
        return True

    async def close(self):
        self.connected = False

    def __getattr__(self, method):
        async def record(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            return self._RESULTS.get(method)
        return record


fakes: dict[str, Recorder] = {}


def factory(cfg):
    fakes[cfg.name] = Recorder(cfg)
    return fakes[cfg.name]


server.pool = DevicePool(load_devices(os.environ), factory)


async def expect_device_error(coro, fragment: str, what: str) -> None:
    try:
        await coro
    except DeviceError as e:
        check(fragment in str(e), f"{what}: {e}")
    else:
        failures.append(f"{what}: no DeviceError")


async def main() -> None:
    tools = await server.mcp.list_tools()
    check(len(tools) == 26, f"25 tools + list_devices, got {len(tools)}")
    for t in tools:
        if t.name != "list_devices":
            props = t.inputSchema.get("properties", {})
            check("device" in props and "device" not in t.inputSchema.get("required", []),
                  f"{t.name} takes an optional device")

    await server.mcp.call_tool("click", {"x": 3, "y": 4, "device": "lab"})
    check(("click", (3, 4, "left"), {}) in fakes["lab"].calls and "office" not in fakes,
          "click routed to lab only")

    await expect_device_error(server.click(3, 4), "several JetKVM devices", "click without device")
    await expect_device_error(server.power("reset"), "several JetKVM devices", "power without device")
    check(not any(c[0] == "rpc" and c[1][:1] == ("setATXPowerAction",)
                  for f in fakes.values() for c in f.calls),
          "no power action sent when the device is ambiguous")

    out = await server.type_text("hi", device="lab")
    check(fakes["lab"].calls[-1] == ("type_text", ("hi", "de"), {}) and "(de layout)" in out,
          f"lab types with its own layout: {fakes['lab'].calls[-1]} / {out}")
    r = await server.keyboard_layout("fr", device="office")
    check(r["device"] == "office" and r["active"] == "fr" and server.pool.layout("lab") == "de",
          f"layout set per device: {r}")

    listed = await server.list_devices()
    check([d["name"] for d in listed["devices"]] == ["office", "lab"], f"list_devices names: {listed}")
    check("s3cret" not in json.dumps(listed) and "password" not in json.dumps(listed),
          "list_devices leaks no password")


asyncio.run(main())


# ── transport: stdio by default, streamable HTTP for a shared container ─────
check(server.transport_settings({}) == ("stdio", "127.0.0.1", 8000), "stdio by default")
check(server.transport_settings({"JETKVM_MCP_TRANSPORT": "HTTP", "JETKVM_MCP_HOST": "0.0.0.0",
                                 "JETKVM_MCP_PORT": "9000"}) == ("streamable-http", "0.0.0.0", 9000),
      "http settings")
for env, bad_value in (({"JETKVM_MCP_TRANSPORT": "carrier-pigeon"}, "carrier-pigeon"),
                       ({"JETKVM_MCP_TRANSPORT": "http", "JETKVM_MCP_PORT": "eighty"}, "eighty"),
                       ({"JETKVM_MCP_TRANSPORT": "http", "JETKVM_MCP_PORT": "70000"}, "70000")):
    try:
        server.transport_settings(env)
        failures.append(f"no ConfigError for {env}")
    except ConfigError as e:
        check(bad_value in str(e), f"error should name {bad_value!r}: {e}")


def run_import(env_extra: dict, code: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("JETKVM_")}
    env.update(env_extra)
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          cwd=os.path.dirname(os.path.abspath(__file__)))


bad = run_import({"JETKVM_CONFIG": "/nonexistent/devices.json"}, "import server")
check(bad.returncode != 0 and "/nonexistent/devices.json" in bad.stderr,
      "a bad config stops the server at startup, naming the file")
legacy = run_import({"JETKVM_URL": "http://10.0.0.9"}, "import server; print(server.pool.names())")
check(legacy.stdout.strip() == "['default']", f"legacy env still works: {legacy.stdout}{legacy.stderr}")


if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ok — server device routing")
