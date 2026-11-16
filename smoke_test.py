"""Live LAN smoke test: connect, hold open, verify sustained video + multiple snapshots.

    python smoke_test.py [device-name | device-url]

A URL is used as-is; a name picks a device from JETKVM_CONFIG (and may be omitted
when one device is configured); with neither, JETKVM_URL is used.
"""
import asyncio, logging, os, sys, tempfile, time
from jetkvm.devices import ConfigError, load_devices
from jetkvm.pool import DeviceError, DevicePool

logging.basicConfig(level=logging.WARNING)  # quiet; we print our own progress

ARG = sys.argv[1] if len(sys.argv) > 1 else ""
SCRATCH = os.environ.get("SMOKE_OUT", tempfile.gettempdir())
ENV = dict(os.environ)
if ARG.startswith(("http://", "https://")):
    ENV.pop("JETKVM_CONFIG", None)
    ENV["JETKVM_URL"], ARG = ARG, ""
try:
    DEVICES = load_devices(ENV)
    POOL = DevicePool(DEVICES)
    NAME = POOL.resolve(ARG)
except (ConfigError, DeviceError) as e:
    sys.exit(f"usage: smoke_test.py [device-name | device-url]\n{e}")


async def main():
    print(f"connecting to {NAME} ({DEVICES[NAME].url}) ...")
    c = await POOL.client(NAME)
    print("connected. video_state:", await c.rpc("getVideoState"))

    # Hold the connection open and snapshot every 2s for 8s to prove sustained video.
    for i in range(4):
        await asyncio.sleep(2)
        t0 = time.monotonic()
        data, w, h = await c.snapshot()
        dt = (time.monotonic() - t0) * 1000
        path = f"{SCRATCH}/shot_{i}.jpg"
        with open(path, "wb") as f:
            f.write(data)
        print(f"  snapshot {i}: {w}x{h}  {len(data):>7} bytes  (grab {dt:4.0f} ms)  frames_seen={c._frame_count}")

    await POOL.close()
    print("done.")

asyncio.run(main())
