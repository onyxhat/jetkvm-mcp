"""Offline check of device config loading — no device needed.

One server can drive several JetKVMs, named in a JSON file (JETKVM_CONFIG); the
old JETKVM_URL env vars still describe a single device. A config mistake has to
stop the server at startup with a message naming the culprit — a typo'd
"pasword" that silently connected without a password would be worse than a crash.

    python devices_test.py
"""
import json
import os
import sys
import tempfile

from jetkvm.devices import ConfigError, DeviceConfig, load_devices

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    if not cond:
        failures.append(what)


def config_file(obj) -> str:
    """Write obj (a dict as JSON, a str verbatim) to a temp file; return its path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))
    return path


def expect_error(env: dict, *fragments: str) -> None:
    try:
        load_devices(env)
    except ConfigError as e:
        missing = [f for f in fragments if f not in str(e)]
        check(not missing, f"error for {env} should mention {missing}: {e}")
    else:
        failures.append(f"no ConfigError for {env}")


def cfg(devices: dict) -> dict:
    return {"JETKVM_CONFIG": config_file({"devices": devices})}


# ── legacy single-device env still works ────────────────────────────────────
d = load_devices({"JETKVM_URL": "http://10.0.0.5", "JETKVM_PASSWORD": "pw",
                  "JETKVM_VERIFY_TLS": "TRUE", "JETKVM_KEYBOARD_LAYOUT": "German"})
check(d == {"default": DeviceConfig("default", "http://10.0.0.5", "pw", True, "de")},
      "legacy env → one 'default' device")
check(load_devices({"JETKVM_URL": "http://10.0.0.5"})["default"]
      == DeviceConfig("default", "http://10.0.0.5"), "legacy defaults")

# ── the config file ─────────────────────────────────────────────────────────
good = config_file({"devices": {
    "office": {"url": "http://192.168.1.50"},
    "lab": {"url": "https://10.0.0.7", "password": "s3cret", "verify_tls": True,
            "keyboard_layout": "de"},
}})
d = load_devices({"JETKVM_CONFIG": good})
check(list(d) == ["office", "lab"], "file order kept")
check(d["office"] == DeviceConfig("office", "http://192.168.1.50"), "defaults applied")
check(d["lab"] == DeviceConfig("lab", "https://10.0.0.7", "s3cret", True, "de"), "all fields read")
check("s3cret" not in repr(d["lab"]), "password kept out of repr")

# ── mistakes fail loudly, naming what is wrong ──────────────────────────────
expect_error({}, "JETKVM_CONFIG", "JETKVM_URL")
expect_error({"JETKVM_CONFIG": good, "JETKVM_URL": "http://x"}, "not both")
expect_error({"JETKVM_CONFIG": "/nonexistent/devices.json"}, "/nonexistent/devices.json")
bad_json = config_file("{not json")
expect_error({"JETKVM_CONFIG": bad_json}, bad_json)
expect_error(cfg({}), "no devices")
expect_error(cfg({"Lab 1": {"url": "http://x"}}), "'Lab 1'")
expect_error(cfg({"lab": {}}), "lab", "url")
expect_error(cfg({"lab": {"url": "192.168.1.5"}}), "lab", "http://")
expect_error(cfg({"lab": {"url": "http://x", "pasword": "pw"}}), "lab", "pasword")
expect_error(cfg({"lab": {"url": "http://x", "verify_tls": "false"}}), "lab", "verify_tls")
expect_error(cfg({"lab": {"url": "http://x", "keyboard_layout": "klingon"}}), "lab", "klingon")
expect_error({"JETKVM_URL": "http://x", "JETKVM_KEYBOARD_LAYOUT": "klingon"}, "klingon")
if os.geteuid() != 0:  # root reads anything
    locked = config_file({"devices": {"lab": {"url": "http://x"}}})
    os.chmod(locked, 0)
    expect_error({"JETKVM_CONFIG": locked}, locked)


if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ok — device config loading")
