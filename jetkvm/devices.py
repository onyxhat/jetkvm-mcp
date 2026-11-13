"""Which JetKVMs this server drives, and how to reach each one.

Two sources, never both:
  * JETKVM_CONFIG — path to a JSON file naming several devices:
        {"devices": {"office": {"url": "http://192.168.1.50"},
                     "lab":    {"url": "https://10.0.0.7", "password": "…",
                                "verify_tls": true, "keyboard_layout": "de"}}}
  * the original single-device env vars (JETKVM_URL, JETKVM_PASSWORD,
    JETKVM_VERIFY_TLS, JETKVM_KEYBOARD_LAYOUT), which become one device named
    "default".

Everything is validated here so a mistake stops the server at startup, naming the
device and field, instead of surfacing as a confusing failure mid-session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Mapping

from . import keymap

DEFAULT_DEVICE = "default"

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_KEYS = {"url", "password", "verify_tls", "keyboard_layout"}


class ConfigError(ValueError):
    """The device configuration is missing or invalid."""


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    url: str
    password: str = field(default="", repr=False)  # never in logs or tracebacks
    verify_tls: bool = False
    keyboard_layout: str = "us"


def load_devices(env: Mapping[str, str]) -> dict[str, DeviceConfig]:
    """Device name -> config, in the order the config file lists them."""
    path = env.get("JETKVM_CONFIG", "").strip()
    url = env.get("JETKVM_URL", "").strip()
    if path and url:
        raise ConfigError("set JETKVM_CONFIG (several devices) or JETKVM_URL (one device), not both")
    if path:
        return _from_file(path)
    if url:
        return {DEFAULT_DEVICE: _device(DEFAULT_DEVICE, {
            "url": url,
            "password": env.get("JETKVM_PASSWORD", ""),
            "verify_tls": env.get("JETKVM_VERIFY_TLS", "").lower() == "true",
            "keyboard_layout": env.get("JETKVM_KEYBOARD_LAYOUT", "").strip() or "us",
        })}
    raise ConfigError(
        "no JetKVM configured: set JETKVM_CONFIG to a devices JSON file, "
        "or JETKVM_URL (e.g. http://192.168.1.50) for a single device"
    )


def _from_file(path: str) -> dict[str, DeviceConfig]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read JETKVM_CONFIG {path}: {e.strerror or e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"JETKVM_CONFIG {path} is not valid JSON: {e}") from e
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict) or not devices:
        raise ConfigError(f'{path} lists no devices; expected {{"devices": {{"<name>": {{"url": …}}}}}}')
    return {name: _device(name, spec) for name, spec in devices.items()}


def _device(name: str, spec) -> DeviceConfig:
    if not _NAME.fullmatch(name):
        raise ConfigError(
            f"device name {name!r} must be lowercase letters, digits, '-' or '_' (max 32)"
        )
    if not isinstance(spec, dict):
        raise ConfigError(f"device {name!r}: expected an object with a url")
    unknown = sorted(set(spec) - _KEYS)
    if unknown:
        raise ConfigError(
            f"device {name!r}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(_KEYS))}"
        )
    url = spec.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigError(f"device {name!r}: url is required")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"device {name!r}: url {url!r} must start with http:// or https://")
    password = spec.get("password", "")
    if not isinstance(password, str):
        raise ConfigError(f"device {name!r}: password must be a string")
    verify_tls = spec.get("verify_tls", False)
    if not isinstance(verify_tls, bool):  # "false" is a truthy string
        raise ConfigError(f"device {name!r}: verify_tls must be true or false, not {verify_tls!r}")
    try:
        layout = keymap.normalize_layout(spec.get("keyboard_layout", "us"))
    except ValueError as e:
        raise ConfigError(f"device {name!r}: {e}") from e
    return DeviceConfig(name, url, password, verify_tls, layout)
