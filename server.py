"""JetKVM MCP server — drive any PC through a JetKVM with an AI agent.

Two planes, both over one WebRTC connection per device:

  Screen control (vision loop):  screenshot, click, double_click, move_mouse,
                                 type_text, press_key, scroll
  Device control (deterministic): mount_media_url, mount_media_storage,
                                 unmount_media, list_storage, delete_storage_file,
                                 storage_space, power, power_state, dc_power,
                                 wake_host, wol, usb_emulation, video_state,
                                 reboot_device

Every tool takes an optional `device`: the name of the JetKVM to act on. It may be
omitted only when one device is configured; list_devices names them.

Config via env — either several devices from a file:
  JETKVM_CONFIG    path to a JSON file: {"devices": {"<name>": {"url": …,
                   "password": …, "verify_tls": …, "keyboard_layout": …}}}
or one device (named "default"):
  JETKVM_URL       e.g. http://192.168.1.50   (the device's address)
  JETKVM_PASSWORD  local password (omit if device is in noPassword mode)
  JETKVM_VERIFY_TLS  "true" to verify the cert (default false; device uses self-signed)
  JETKVM_KEYBOARD_LAYOUT  layout active on the *target* OS: us (default), uk, de, fr.
                          Wrong layout = wrong characters, silently — see keyboard_layout.

Transport:
  JETKVM_MCP_TRANSPORT  stdio (default) or http — streamable HTTP at /mcp, so one
                        long-running server (one session per device) serves every client
  JETKVM_MCP_HOST       http bind address (default 127.0.0.1 — the endpoint has no auth)
  JETKVM_MCP_PORT       http port (default 8000)
  JETKVM_MCP_ALLOWED_HOSTS  extra comma-separated hostnames clients may use to reach the
                        http server; localhost is always allowed, anything else is refused
"""

from __future__ import annotations

import os
import logging
from typing import Annotated, Mapping

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from jetkvm import JetKVMClient, keymap
from jetkvm.devices import ConfigError, load_devices
from jetkvm.pool import DevicePool

logging.basicConfig(level=logging.INFO)

_TRANSPORTS = {"stdio": "stdio", "http": "streamable-http"}


def transport_settings(env: Mapping[str, str]) -> tuple[str, str, int]:
    """(FastMCP transport, host, port) from the JETKVM_MCP_* env vars."""
    raw = env.get("JETKVM_MCP_TRANSPORT", "").strip() or "stdio"
    transport = _TRANSPORTS.get(raw.lower())
    if transport is None:
        raise ConfigError(f"JETKVM_MCP_TRANSPORT {raw!r} must be one of: {', '.join(_TRANSPORTS)}")
    host = env.get("JETKVM_MCP_HOST", "").strip() or "127.0.0.1"
    port_raw = env.get("JETKVM_MCP_PORT", "").strip() or "8000"
    if not port_raw.isdigit() or not 1 <= int(port_raw) <= 65535:
        raise ConfigError(f"JETKVM_MCP_PORT {port_raw!r} must be a port number (1-65535)")
    return transport, host, int(port_raw)


def transport_security(env: Mapping[str, str]) -> TransportSecuritySettings:
    """Host/Origin checks for the http transport, on whatever address it binds.

    FastMCP only enables them for a 127.0.0.1 bind, but in Docker the server binds
    0.0.0.0 — and without them a web page that rebinds its domain to 127.0.0.1
    could drive every device."""
    names = ["127.0.0.1", "localhost", "[::1]"]
    names += [h.strip() for h in env.get("JETKVM_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h for n in names for h in (n, f"{n}:*")],
        allowed_origins=[o for n in names for s in ("http", "https") for o in (f"{s}://{n}", f"{s}://{n}:*")],
    )


# Read at import so a config mistake stops the server at startup, not mid-command.
_transport, _host, _port = transport_settings(os.environ)
pool = DevicePool(load_devices(os.environ))
mcp = FastMCP("jetkvm", host=_host, port=_port, transport_security=transport_security(os.environ))
logging.info("JetKVM devices: %s", ", ".join(pool.names()))

Device = Annotated[str, Field(
    description="Which JetKVM to act on: a name from list_devices. "
                "May be omitted only when one device is configured.",
)]


async def kvm(device: str = "") -> JetKVMClient:
    """The live connection to a device, (re)connected as needed — see DevicePool."""
    return await pool.client(device)


@mcp.tool()
async def list_devices() -> dict:
    """List the JetKVM devices this server can drive: name, address, keyboard
    layout, and whether a connection is open. Call this first when you don't
    know the device names — with several devices, every other tool needs one."""
    return {"devices": pool.describe()}


# ───────────────────────── screen control (the eyes + hands) ────────────────

@mcp.tool()
async def screenshot(device: Device = "") -> Image:
    """Capture the target machine's current screen as a JPEG, cropped to the
    visible desktop (letterbox bars from underscanning hosts are removed).
    Coordinates for click/move are pixel coordinates on the image this returns."""
    c = await kvm(device)
    data, w, h = await c.snapshot()
    return Image(data=data, format="jpeg")


@mcp.tool()
async def click(x: int, y: int, button: str = "left", device: Device = "") -> str:
    """Click at pixel (x, y) on the latest screenshot of the same device.
    button: left|right|middle."""
    await (await kvm(device)).click(x, y, button)
    return f"clicked {button} at ({x},{y})"


@mcp.tool()
async def double_click(x: int, y: int, device: Device = "") -> str:
    """Double-click at pixel (x, y)."""
    await (await kvm(device)).double_click(x, y)
    return f"double-clicked at ({x},{y})"


@mcp.tool()
async def move_mouse(x: int, y: int, device: Device = "") -> str:
    """Move the pointer to pixel (x, y) on the latest screenshot of the same
    device, without clicking."""
    await (await kvm(device)).move(x, y)
    return f"moved to ({x},{y})"


@mcp.tool()
async def type_text(text: str, layout: str = "", device: Device = "") -> str:
    """Type a string on the target machine's emulated USB keyboard.

    A USB keyboard sends key positions, not characters — what appears depends on
    the keyboard layout the *target* OS has active. Set it once per device with
    keyboard_layout (or in the device config); `layout` overrides it for this
    call. Characters the layout cannot produce are reported back, not dropped
    silently."""
    c = await kvm(device)
    active = layout or pool.layout(device)
    skipped = await c.type_text(text, active)
    if skipped:
        return (
            f"typed {len(text) - len(skipped)} of {len(text)} chars ({active} layout); "
            f"unreachable on this layout, skipped: {''.join(skipped)!r}"
        )
    return f"typed {len(text)} chars ({active} layout)"


@mcp.tool()
async def press_key(combo: str, layout: str = "", device: Device = "") -> str:
    """Press a key or chord, e.g. 'enter', 'ctrl+c', 'ctrl+alt+delete', 'win+r', 'f2'.
    'altgr' is available for third-level characters on non-US layouts. Character
    keys in a chord are resolved through the device's keyboard layout, so 'ctrl+z'
    presses the key that really is Z on the target."""
    c = await kvm(device)
    await c.press_key(combo, layout or pool.layout(device))
    return f"pressed {combo}"


@mcp.tool()
async def keyboard_layout(layout: str = "", device: Device = "") -> dict:
    """Get or set the keyboard layout a device's target OS is using.

    Call with no layout to read the current one and the available ones; pass a
    name ('de', 'German', 'en-GB', …) to switch. This is the target's layout,
    not yours — get it wrong and type_text produces plausible-looking wrong
    characters with no error (on German, 'z' arrives as 'y')."""
    name = pool.resolve(device)
    if layout:
        pool.set_layout(name, layout)
    return {
        "device": name,
        "active": pool.layout(name),
        "available": keymap.available_layouts(),
    }


@mcp.tool()
async def scroll(amount: int, device: Device = "") -> str:
    """Scroll the wheel. Positive = up, negative = down (number of detents)."""
    await (await kvm(device)).scroll(amount)
    return f"scrolled {amount}"


# ───────────────────────── device control (deterministic) ───────────────────

@mcp.tool()
async def mount_media_url(url: str, mode: str = "CDROM", device: Device = "") -> str:
    """Mount a bootable image as USB media, streamed from a URL (host big ISOs on
    your own server). mode: CDROM (read-only optical) or Disk (writable mass storage).
    The target can then boot from it. Use for OS install / recovery."""
    if mode not in ("CDROM", "Disk"):
        return "mode must be 'CDROM' or 'Disk'"
    await (await kvm(device)).rpc("mountWithHTTP", url=url, mode=mode)
    return f"mounted {url} as {mode}"


@mcp.tool()
async def mount_media_storage(filename: str, mode: str = "CDROM", device: Device = "") -> str:
    """Mount an image already uploaded to the device's local storage."""
    if mode not in ("CDROM", "Disk"):
        return "mode must be 'CDROM' or 'Disk'"
    await (await kvm(device)).rpc("mountWithStorage", filename=filename, mode=mode)
    return f"mounted {filename} as {mode}"


@mcp.tool()
async def upload_media(local_path: str, device: Device = "") -> dict:
    """Stream an image from this machine into the device's storage partition, to be
    mounted afterwards with mount_media_storage. Reads from disk in chunks and resumes
    an interrupted transfer, so full OS ISOs are fine as long as they fit (check
    storage_space; ~14 GB on current hardware). Expect ~11 MB/s over the device's
    100 Mbps port."""
    return await (await kvm(device)).upload_to_storage(local_path)


@mcp.tool()
async def upload_and_mount(local_path: str, mode: str = "CDROM", device: Device = "") -> str:
    """Upload a local image to device storage and immediately mount it as USB media."""
    if mode not in ("CDROM", "Disk"):
        return "mode must be 'CDROM' or 'Disk'"
    c = await kvm(device)
    info = await c.upload_to_storage(local_path)
    await c.rpc("mountWithStorage", filename=info["filename"], mode=mode)
    return f"uploaded and mounted {info['filename']} as {mode}"


@mcp.tool()
async def unmount_media(device: Device = "") -> str:
    """Eject / unmount the currently mounted virtual media."""
    await (await kvm(device)).rpc("unmountImage")
    return "unmounted"


@mcp.tool()
async def list_storage(device: Device = "") -> dict:
    """List image files in the device's local storage."""
    return await (await kvm(device)).rpc("listStorageFiles")


@mcp.tool()
async def delete_storage_file(filename: str, device: Device = "") -> str:
    """Delete an image from the device's local storage."""
    await (await kvm(device)).rpc("deleteStorageFile", filename=filename)
    return f"deleted {filename}"


@mcp.tool()
async def storage_space(device: Device = "") -> dict:
    """Report free/total bytes on the device's storage partition (~14 GB on current
    hardware); check it before upload_media."""
    return await (await kvm(device)).rpc("getStorageSpace")


@mcp.tool()
async def virtual_media_state(device: Device = "") -> dict:
    """Report what (if anything) is currently mounted."""
    return await (await kvm(device)).rpc("getVirtualMediaState")


@mcp.tool()
async def power(action: str, device: Device = "") -> str:
    """ATX front-panel power control (requires the ATX/header extension wired up).
    action: 'power-short' (tap power), 'power-long' (5s hard off), 'reset'."""
    if action not in ("power-short", "power-long", "reset"):
        return "action must be power-short | power-long | reset"
    await (await kvm(device)).rpc("setATXPowerAction", action=action)
    return f"ATX {action} sent"


@mcp.tool()
async def power_state(device: Device = "") -> dict:
    """Read ATX power/HDD LED state (power: on/off, hdd: activity)."""
    return await (await kvm(device)).rpc("getATXState")


@mcp.tool()
async def dc_power(enabled: bool, device: Device = "") -> str:
    """Toggle DC power output (DC-power extension)."""
    await (await kvm(device)).rpc("setDCPowerState", enabled=enabled)
    return f"DC power {'on' if enabled else 'off'}"


@mcp.tool()
async def wake_host(device: Device = "") -> str:
    """Wake the directly-attached host (USB wake)."""
    await (await kvm(device)).rpc("wakeHost")
    return "wake signal sent"


@mcp.tool()
async def wol(mac_address: str, broadcast_ip: str | None = None, device: Device = "") -> str:
    """Send a Wake-on-LAN magic packet to a MAC on the device's network."""
    params = {"macAddress": mac_address}
    if broadcast_ip:
        params["broadcastIP"] = broadcast_ip
    await (await kvm(device)).rpc("sendWOLMagicPacket", **params)
    return f"WOL sent to {mac_address}"


@mcp.tool()
async def usb_emulation(enabled: bool, device: Device = "") -> str:
    """Enable/disable the emulated USB gadget (keyboard/mouse/storage) to the target."""
    await (await kvm(device)).rpc("setUsbEmulationState", enabled=enabled)
    return f"USB emulation {'enabled' if enabled else 'disabled'}"


@mcp.tool()
async def video_state(device: Device = "") -> dict:
    """Report the captured HDMI signal state and resolution."""
    return await (await kvm(device)).rpc("getVideoState")


@mcp.tool()
async def reboot_device(force: bool = False, device: Device = "") -> str:
    """Reboot the JetKVM device itself (not the target machine)."""
    await (await kvm(device)).rpc("reboot", force=force)
    return "JetKVM reboot requested"


if __name__ == "__main__":
    mcp.run(transport=_transport)
