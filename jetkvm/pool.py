"""One lazily-connected client per JetKVM, each with its own lock and keyboard layout.

Connections are built on first use and health-checked on every later one: a dead
peer (device reboot, network blip) is torn down and rebuilt, so tool calls recover
instead of failing forever. Locks are per device, so a device that is slow to
connect — or not there at all — never stalls calls to the others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Mapping

from . import keymap
from .client import JetKVMClient
from .devices import DeviceConfig

log = logging.getLogger("jetkvm")

ClientFactory = Callable[[DeviceConfig], JetKVMClient]


class DeviceError(ValueError):
    """A tool call named no device, or one that isn't configured."""


def _connect_client(cfg: DeviceConfig) -> JetKVMClient:
    return JetKVMClient(base_url=cfg.url, password=cfg.password, verify_tls=cfg.verify_tls)


class DevicePool:
    def __init__(self, devices: Mapping[str, DeviceConfig], factory: ClientFactory | None = None):
        if not devices:
            raise ValueError("DevicePool needs at least one device")
        self._devices = dict(devices)
        self._factory = factory or _connect_client
        self._clients: dict[str, JetKVMClient] = {}
        self._locks = {name: asyncio.Lock() for name in self._devices}
        # Held here, not on the client, so a runtime change survives reconnects.
        self._layouts = {name: cfg.keyboard_layout for name, cfg in self._devices.items()}

    def names(self) -> list[str]:
        return list(self._devices)

    def resolve(self, device: str = "") -> str:
        """The configured name a tool call means. Omitting it is only allowed when
        there is exactly one device — guessing could power off the wrong machine."""
        name = (device or "").strip().lower()
        configured = ", ".join(self._devices)
        if not name:
            if len(self._devices) == 1:
                return next(iter(self._devices))
            raise DeviceError(
                f"several JetKVM devices are configured ({configured}); "
                "pass device=<name> (list_devices describes them)"
            )
        if name not in self._devices:
            raise DeviceError(f"unknown JetKVM device {device!r}; configured: {configured}")
        return name

    async def client(self, device: str = "") -> JetKVMClient:
        """The live client for a device, (re)connecting as needed."""
        name = self.resolve(device)
        async with self._locks[name]:
            c = self._clients.get(name)
            if c is not None and not await c.ping():
                log.warning("JetKVM %r connection is dead; reconnecting", name)
                await c.close()
                del self._clients[name]
                c = None
            if c is None:
                c = self._factory(self._devices[name])
                try:
                    await c.connect()
                except Exception:
                    await c.close()  # don't leak a half-open connection; next call retries
                    raise
                self._clients[name] = c
            return c

    def layout(self, device: str = "") -> str:
        return self._layouts[self.resolve(device)]

    def set_layout(self, device: str, layout: str) -> str:
        name = self.resolve(device)
        self._layouts[name] = keymap.normalize_layout(layout)  # raises before changing anything
        return self._layouts[name]

    def describe(self) -> list[dict]:
        """Public facts about every device — no password, no network I/O."""
        return [
            {
                "name": name,
                "url": cfg.url,
                "keyboard_layout": self._layouts[name],
                "connected": name in self._clients and self._clients[name].connected,
            }
            for name, cfg in self._devices.items()
        ]

    async def close(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for c in clients:
            await c.close()
