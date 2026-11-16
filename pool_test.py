"""Offline check of the per-device connection pool — no device needed.

Each JetKVM gets its own lazily-built client, lock and keyboard layout, so one
device that is rebooting or unreachable never stalls calls to the others, and a
call that doesn't say which device it means is refused once there are several.

    python pool_test.py
"""
import asyncio
import sys

from jetkvm.devices import DeviceConfig
from jetkvm.pool import DeviceError, DevicePool

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    if not cond:
        failures.append(what)


def expect(exc: type, fn, *fragments: str) -> None:
    try:
        fn()
    except exc as e:
        missing = [f for f in fragments if f not in str(e)]
        check(not missing, f"{exc.__name__} should mention {missing}: {e}")
    except Exception as e:
        failures.append(f"expected {exc.__name__}, got {type(e).__name__}: {e}")
    else:
        failures.append(f"expected {exc.__name__}, nothing raised")


class FakeClient:
    """Stands in for JetKVMClient: connect / ping / close / connected."""

    def __init__(self, cfg: DeviceConfig):
        self.cfg = cfg
        self.alive = True
        self.closed = False
        self.connects = 0
        self.gate: asyncio.Event | None = None  # connect() waits on it when set
        self.fail = False  # connect() raises when set

    @property
    def connected(self) -> bool:
        return self.connects > 0 and self.alive and not self.closed

    async def connect(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("device unreachable")
        self.connects += 1

    async def ping(self) -> bool:
        return self.alive

    async def close(self) -> None:
        self.closed = True


made: list[FakeClient] = []
behave: dict[str, dict] = {}  # device name -> attrs for the next FakeClient made for it


def factory(cfg: DeviceConfig) -> FakeClient:
    c = FakeClient(cfg)
    c.__dict__.update(behave.pop(cfg.name, {}))
    made.append(c)
    return c


OFFICE = DeviceConfig("office", "http://10.0.0.1")
LAB = DeviceConfig("lab", "http://10.0.0.2", keyboard_layout="de")


async def main() -> None:
    # ── which device a call means ──────────────────────────────────────────
    check(DevicePool({"office": OFFICE}, factory).resolve("") == "office",
          "a single device is implied")
    pool = DevicePool({"office": OFFICE, "lab": LAB}, factory)
    expect(DeviceError, lambda: pool.resolve(""),
           "several JetKVM devices are configured (office, lab)", "device=<name>")
    expect(DeviceError, lambda: pool.resolve("nope"),
           "unknown JetKVM device 'nope'; configured: office, lab")
    check(pool.resolve(" LAB ") == "lab", "lookup ignores case and whitespace")

    # ── one cached, self-healing connection per device ──────────────────────
    a = await pool.client("office")
    check(a is await pool.client("office") and a.connects == 1, "connection reused")
    b = await pool.client("lab")
    check(b is not a and b.cfg is LAB, "each device has its own client")
    a.alive = False
    a2 = await pool.client("office")
    check(a.closed and a2 is not a, "dead connection replaced")

    b.alive = False
    behave["lab"] = {"fail": True}
    try:
        await pool.client("lab")
        failures.append("a failed connect should raise")
    except RuntimeError:
        pass
    check(made[-1].closed, "failed connect is closed, not leaked")
    b3 = await pool.client("lab")
    check(b3 is made[-1] and b3.connects == 1, "next call retries instead of caching the failure")

    # ── a slow device doesn't hold up the others ────────────────────────────
    gate = asyncio.Event()
    behave["office"] = {"gate": gate}
    slow_pool = DevicePool({"office": OFFICE, "lab": LAB}, factory)
    slow = asyncio.create_task(slow_pool.client("office"))
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(slow_pool.client("lab"), 1.0)
    except asyncio.TimeoutError:
        failures.append("lab waited on office's connect")
    check(not slow.done(), "lab served while office is still connecting")
    gate.set()
    await slow
    slow_clients = made[-2:]  # office, then lab

    # ── keyboard layout is per device ───────────────────────────────────────
    check(pool.layout("lab") == "de" and pool.layout("office") == "us", "layouts come from config")
    check(pool.set_layout("office", "French") == "fr" and pool.layout("office") == "fr",
          "set_layout normalises")
    check(pool.layout("lab") == "de", "other device's layout untouched")
    expect(ValueError, lambda: pool.set_layout("office", "klingon"), "klingon")
    check(pool.layout("office") == "fr", "bad layout keeps the old one")
    (await pool.client("office")).alive = False
    await pool.client("office")
    check(pool.layout("office") == "fr", "layout survives a reconnect")

    # ── describe, close ─────────────────────────────────────────────────────
    rows = {r["name"]: r for r in slow_pool.describe()}
    check(all(set(r) == {"name", "url", "keyboard_layout", "connected"} for r in rows.values()),
          "describe has exactly the public fields (no password)")
    check(rows["office"]["connected"] and rows["lab"]["connected"], "connected reflects live clients")
    check(not DevicePool({"office": OFFICE}, factory).describe()[0]["connected"],
          "never-connected device reads false")
    await slow_pool.close()
    check(all(c.closed for c in slow_clients), "close() closes every client")


asyncio.run(main())

if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ok — device pool")
