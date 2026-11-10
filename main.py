#!/usr/bin/env python3
"""Jabra Speak 410 peripheral controller for the Linux Voice Assistant (LVA).

Runs alongside the LVA container. Connects to the Jabra Speak 410 over USB HID,
drives its LED ring to reflect the assistant state, and maps its hardware buttons
to the LVA peripheral API (see OHF-Voice/linux-voice-assistant
``peripheral_api.py``). It also has an optional PipeWire integration for volume
control.

Design goals
------------
* **The Jabra is a USB device**, so it can be unplugged/replugged at any time.
  A discovery loop watches for add/remove events and recreates the HID
  connection. The process itself never crashes on a disconnect.
* **Every loop reconnects or retries on its own.** A single failed I/O operation
  is logged and swallowed — it can never take down the whole process.
* **Mute detection via audio-stream analysis was removed.** It relied on reading
  the raw mic stream for all-zero samples, which was unreliable and janky.
"""

import asyncio
import json
import os
import re
import time
import traceback
from enum import Enum, IntFlag

import hid
import websockets
from websockets import ClientConnection

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# LVA peripheral API WebSocket URL. Defaults to the host port; override when LVA
# runs elsewhere (e.g. LVA_WS_URL=ws://lva:6055).
LVA_WS_URL = os.getenv("LVA_WS_URL", "ws://0.0.0.0:6055")

# GN Netcom / Jabra USB vendor id.
JABRA_VENDOR = 0x0B0E

# PipeWire sink used by volume control. Empty means "the system default sink".
PW_SINK = os.getenv("PW_SINK", "")

# Volume-button behaviour:
#   "pipewire" -> adjust the PipeWire sink directly via wpctl
#   "lva"      -> send volume_up / volume_down commands to LVA
#   unset/other -> ignore the volume buttons entirely
VOL_CTRL = os.getenv("VOLUME_CONTROL", None)
if VOL_CTRL not in ("pipewire", "lva"):
    VOL_CTRL = None

# Tunables (seconds).
READ_POLL = 0.05  # delay between HID polls while idle
DISCOVER_INTERVAL = 1.0  # delay between USB discovery scans
RECONNECT_DELAY = 1.0  # delay between LVA reconnect attempts
MUTE_COOLDOWN = 1.0  # debounce for the call-button hardware quirk


# --------------------------------------------------------------------------- #
# HID / LED / button constants
# --------------------------------------------------------------------------- #


class Telephony(IntFlag):
    """Bits from the Jabra's undocumented telephony HID report."""

    hook_switch = 1 << 0
    line_busy_tone = 1 << 1
    speaker_phone = 1 << 2
    mute = 1 << 3
    flash = 1 << 4
    redial = 1 << 5
    speed_dial = 1 << 6
    phone_key_bit_0 = 1 << 7
    phone_key_bit_1 = 1 << 8
    phone_key_bit_2 = 1 << 9
    phone_key_bit_3 = 1 << 10
    # Undocumented: fires on hang-up while the LEDs are flashing.
    button_7 = 1 << 11


class LEDs(IntFlag):
    off_hook = 1 << 0
    speaker = 1 << 1
    mute = 1 << 2
    ring = 1 << 3
    hold = 1 << 4
    microphone = 1 << 5
    # Marked "telephony" upstream; duplicate of `ring`.
    ringer = 1 << 6


class Volume(IntFlag):
    vol_up = 1 << 0
    vol_down = 1 << 1
    mute = 1 << 2


class LEDState(IntFlag):
    """Led-ring patterns we drive. IntFlag so patterns can bit-combine."""

    default = 0
    three_green = LEDs.off_hook
    all_red = LEDs.mute | LEDs.off_hook
    flashing = LEDs.ring | LEDs.off_hook
    partial_flash = LEDs.hold


# --------------------------------------------------------------------------- #
# LVA peripheral API protocol
# --------------------------------------------------------------------------- #


class LVAEvent(str, Enum):
    """Events broadcast from LVA to peripheral clients."""

    WAKE_WORD_DETECTED = "wake_word_detected"
    LISTENING = "listening"
    THINKING = "thinking"
    TTS_SPEAKING = "tts_speaking"
    TTS_FINISHED = "tts_finished"
    PIPELINE_ERROR = "pipeline_error"
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    MUTED = "muted"
    TIMER_TICKING = "timer_ticking"
    TIMER_UPDATED = "timer_updated"
    TIMER_RINGING = "timer_ringing"
    MEDIA_PLAYER_PLAYING = "media_player_playing"
    VOLUME_CHANGED = "volume_changed"
    VOLUME_MUTED = "volume_muted"
    ZEROCONF = "zeroconf"


class LVACommand(str, Enum):
    """Commands accepted from peripheral clients."""

    START_LISTENING = "start_listening"
    STOP_PIPELINE = "stop_pipeline"
    MUTE_MIC = "mute_mic"
    UNMUTE_MIC = "unmute_mic"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    STOP_TIMER_RINGING = "stop_timer_ringing"
    STOP_MEDIA_PLAYER = "stop_media_player"
    REGISTER_BUTTON_LOCK = "register_button_lock"


# Sentinel returned by ``JabraHid.read`` when the device vanished. Never a real
# event, so it is safe to compare with ``is``.
DEVICE_GONE = object()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def log(msg: str, *args) -> None:
    if args:
        msg = f"{msg} {' '.join(str(a) for a in args)}"
    print(f"{time.time():.1f}s: {msg}", flush=True)


# --------------------------------------------------------------------------- #
# One Jabra device + its HID read loop
# --------------------------------------------------------------------------- #


class JabraHid:
    """Wraps a single USB HID Jabra device and its async read loop.

    A new instance is created every time a device is (re)discovered: on Linux a
    closed ``hid.device`` cannot be reopened, so we never reuse one.
    """

    def __init__(self, path: str, serial: str):
        self.path = path
        self.serial = serial
        self.hid = hid.device()
        self.task: asyncio.Task | None = None

    def close(self) -> None:
        try:
            self.hid.close()
        except Exception:
            pass  # device already gone — nothing to clean up

    async def write(self, state: LEDState) -> None:
        """Send the telephony LED report to the device."""
        await asyncio.to_thread(
            self.hid.write,
            [0x03, state & 0xFF, (state >> 8) & 0xFF],
        )

    async def read(self):
        """Return the next decoded event, or ``DEVICE_GONE`` if the device vanished.

        Blocks (via ``asyncio.sleep``) until either a packet arrives or the device
        is removed, so the caller never busy-loops.
        """
        while True:
            try:
                data = self.hid.read(8)
            except Exception:
                # Device unplugged / suspended / lost its driver — stop cleanly.
                # (CancelledError is *not* an ``Exception`` subclass, so task
                #  cancellation escapes here rather than being swallowed.)
                return DEVICE_GONE

            if not data:
                await asyncio.sleep(READ_POLL)
                continue

            match data[0]:
                case 0x03:  # telephony / LED report
                    return Telephony(data[1] | data[2] << 8)
                case 0x01:  # volume / consumer control report
                    return Volume(data[1])
                case _:
                    log("unknown HID packet", hex(data[0]))
                    # Keep reading; an unknown packet is not fatal.

    async def readloop(self) -> None:
        """Read events and dispatch them until the device is removed."""
        while True:
            event = await self.read()
            if event is DEVICE_GONE:
                log("jabra disconnected, read loop ended")
                return
            try:
                await handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log("error handling jabra event")
                traceback.print_exc()


# --------------------------------------------------------------------------- #
# USB discovery manager — single source of truth for connected devices
# --------------------------------------------------------------------------- #


class JabraManager:
    def __init__(self):
        # Keyed by USB path. A path can be reused by a freshly-plugged device,
        # which is exactly why we tear down on remove and recreate on add.
        self.devices: dict[str, JabraHid] = {}

    async def loop(self) -> None:
        """Periodically scan for the Jabra and react to add/remove events."""
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                log("discovery error")
                traceback.print_exc()
            await asyncio.sleep(DISCOVER_INTERVAL)

    async def scan(self) -> None:
        current: dict[str, dict] = {}
        try:
            for info in hid.enumerate(JABRA_VENDOR):
                current[info["path"]] = info
        except Exception:
            # hid.enumerate can transiently fail right after a plug. Ignore and
            # retry on the next scan cycle.
            log("hid.enumerate failed")
            return

        # Removed devices.
        for path in list(self.devices):
            if path not in current:
                await self._teardown(path)

        # Newly connected devices.
        for path, info in current.items():
            if path not in self.devices:
                await self._setup(path, info)

    async def _setup(self, path: str, info: dict) -> None:
        serial = info.get("serial_number") or "unknown"
        try:
            dev = JabraHid(path, serial)
        except Exception:
            # Path was in enumerate but disappeared before we could open it.
            log("could not open device @", path)
            return
        log(
            "jabra connected",
            info.get("product_string", ""),
            "s/n",
            serial,
            "@",
            path,
        )
        dev.task = asyncio.create_task(dev.readloop(), name="jabra-readloop")
        self.devices[path] = dev

    async def _teardown(self, path: str) -> None:
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        log("jabra removed", "@", path)
        if dev.task:
            dev.task.cancel()
            try:
                await dev.task
            except (asyncio.CancelledError, Exception):
                pass  # task finished/cancelled — nothing to do
        dev.close()


# --------------------------------------------------------------------------- #
# Shared controller state (mutated by both the HID and LVA loops)
# --------------------------------------------------------------------------- #

last_jabra_write: LEDState = LEDState.default
current_state: LVAEvent | None = None
muted: bool = False
buttons_locked: bool = False
lva_sock: ClientConnection | None = None
last_mute: float = 0.0
_disconnect_task: asyncio.Task | None = None

manager = JabraManager()


def off_mute_cooldown() -> bool:
    """True if ``MUTE_COOLDOWN`` seconds elapsed since the last mute toggle.

    The Jabra spuriously fires ``hook_switch`` right after an unmute, so the
    call button is debounced to ignore that glitch.
    """
    return (time.perf_counter() - last_mute) >= MUTE_COOLDOWN


def bump_mute_cooldown() -> None:
    global last_mute
    last_mute = time.perf_counter()


async def write_to_jabra(state: LEDState) -> None:
    global last_jabra_write
    last_jabra_write = state
    log("to jabra:", state.name)
    # Write to every connected device (normally just one).
    await asyncio.gather(*(d.write(state) for d in manager.devices.values()))


async def write_to_lva(command: LVACommand, data: dict | None = None) -> None:
    msg = {"command": command.value}
    if data:
        msg["data"] = data
    # Physical buttons are locked from Home Assistant; don't send commands.
    if buttons_locked:
        log("buttons locked, ignoring", command.value)
        return
    if lva_sock is None:
        log("no LVA connection, ignoring", command.value)
        return
    log("to lva:", command.value)
    await lva_sock.send(json.dumps(msg))


async def set_mute(muted_new: bool, write_lva: bool = True) -> None:
    global muted
    muted = muted_new
    bump_mute_cooldown()
    commands = [write_to_jabra(LEDState.all_red if muted_new else LEDState.default)]
    if write_lva:
        cmd = LVACommand.MUTE_MIC if muted_new else LVACommand.UNMUTE_MIC
        commands.append(write_to_lva(cmd))
    await asyncio.gather(*commands)


async def toggle_mute() -> None:
    await set_mute(muted)


# --------------------------------------------------------------------------- #
# Button event handling
# --------------------------------------------------------------------------- #


async def handle_event(event) -> None:
    """Dispatch a decoded HID event: drive LEDs and send LVA commands."""
    log("from jabra:", event.name, hex(int(event)))

    if isinstance(event, Telephony):
        await handle_telephony(event)
    elif isinstance(event, Volume):
        await handle_volume(event)


async def handle_telephony(ev: Telephony) -> None:
    # Hang-up gesture. The Jabra maps "hang up" to different bits depending on
    # the LED state it thinks it's in, so we cover the flash-during-TTS case and
    # the undocumented button_7 catch-all for the flashing states.
    if (
        (ev & Telephony.flash)
        and last_jabra_write == LEDState.partial_flash
        or (ev & Telephony.button_7)
    ):
        log("hangup detected")
        if current_state == LVAEvent.MEDIA_PLAYER_PLAYING:
            await write_to_lva(LVACommand.STOP_MEDIA_PLAYER)
        else:
            await write_to_lva(LVACommand.STOP_TIMER_RINGING)
            await write_to_lva(LVACommand.STOP_PIPELINE)
    # Physical mute switch.
    elif ev & Telephony.mute:
        log("mute toggle")
        await toggle_mute()
    # Call button — only valid while sitting in the default (off) LED state.
    elif ev & Telephony.hook_switch and last_jabra_write == LEDState.default:
        if off_mute_cooldown():
            log("call button")
            await write_to_jabra(LEDState.flashing)
            await write_to_lva(LVACommand.START_LISTENING)
            await schedule_listening_bodge()


async def handle_volume(ev: Volume) -> None:
    if not VOL_CTRL:
        log("ignoring volume (VOLUME_CONTROL unset)")
        return
    if buttons_locked:
        log("ignoring volume (buttons locked)")
        return
    if ev & Volume.vol_up:
        log("volume up")
        await adjust_volume(+1)
    elif ev & Volume.vol_down:
        log("volume down")
        await adjust_volume(-1)
    elif ev & Volume.mute:
        log("consumer mute toggle")
        await toggle_mute()


async def adjust_volume(delta: int) -> None:
    if VOL_CTRL == "lva":
        await write_to_lva(
            LVACommand.VOLUME_UP if delta > 0 else LVACommand.VOLUME_DOWN
        )
    else:  # pipewire
        await wpctl(f"{'+' if delta > 0 else '-'}10%")


async def schedule_listening_bodge() -> None:
    """Recover from a stuck "wake word detected" state."""
    await asyncio.sleep(0.5)
    if current_state == LVAEvent.WAKE_WORD_DETECTED:
        await write_to_lva(LVACommand.STOP_PIPELINE)
        await write_to_jabra(LEDState.three_green)


# --------------------------------------------------------------------------- #
# LED animations for LVA error / disconnect states
# --------------------------------------------------------------------------- #


async def _blink_task(interval: float, on: LEDState, off: LEDState) -> None:
    try:
        while True:
            await write_to_jabra(on)
            await asyncio.sleep(interval)
            await write_to_jabra(off)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        log("blink task error")
        traceback.print_exc()


async def start_disconnect_blink() -> None:
    """Blink red to signal LVA/HA is unreachable. Runs until reconnected."""
    global _disconnect_task
    log("LVA disconnected — blinking red")
    if _disconnect_task is not None:
        _disconnect_task.cancel()
    _disconnect_task = asyncio.create_task(
        _blink_task(0.5, LEDState.all_red, LEDState.default)
    )


async def start_pipeline_error_flash() -> None:
    """Brief 4× red flash, then return to default."""
    for _ in range(4):
        await write_to_jabra(LEDState.all_red)
        await asyncio.sleep(0.1)
        await write_to_jabra(LEDState.default)
        await asyncio.sleep(0.1)


async def cancel_blink_on_reconnect() -> None:
    """Clear the disconnect blink once LVA reports a live state again."""
    global _disconnect_task
    if _disconnect_task is not None:
        _disconnect_task.cancel()
        _disconnect_task = None


# --------------------------------------------------------------------------- #
# LVA WebSocket client
# --------------------------------------------------------------------------- #


# LVA state -> LED ring pattern (states that have a persistent visual).
_LED_FOR_EVENT = {
    LVAEvent.WAKE_WORD_DETECTED: LEDState.flashing,
    LVAEvent.LISTENING: LEDState.three_green,
    LVAEvent.THINKING: LEDState.flashing,
    LVAEvent.TTS_SPEAKING: LEDState.partial_flash,
    LVAEvent.TTS_FINISHED: LEDState.default,
    LVAEvent.TIMER_RINGING: LEDState.flashing,
}


async def handle_lva_message(raw: str) -> None:
    global buttons_locked, current_state
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log("bad LVA message")
        return

    event = msg.get("event")

    # On (re)connect LVA pushes a snapshot with our persisted mute + lock state.
    if event == "snapshot":
        data = msg.get("data", {})
        buttons_locked = bool(data.get("button_controls_locked", False))
        if data.get("muted"):
            await set_mute(True, write_lva=False)
        return

    if event == "button_lock_changed":
        buttons_locked = bool(msg.get("data", {}).get("locked", False))
        log("button controls", "locked" if buttons_locked else "unlocked")
        return

    try:
        state = LVAEvent(event)
    except ValueError:
        log("unknown LVA event", event)
        return

    current_state = state

    if state == LVAEvent.DISCONNECTED:
        await start_disconnect_blink()
    else:
        await cancel_blink_on_reconnect()
        if state == LVAEvent.PIPELINE_ERROR:
            await start_pipeline_error_flash()
        elif state == LVAEvent.IDLE:
            await set_mute(False, write_lva=False)
        elif state == LVAEvent.MUTED:
            await set_mute(True, write_lva=False)
        else:
            led = _LED_FOR_EVENT.get(state)
            if led is not None:
                await write_to_jabra(led)


async def wsloop() -> None:
    global lva_sock
    while True:
        try:
            async with websockets.connect(LVA_WS_URL) as ws:
                lva_sock = ws
                log("connected to LVA at", LVA_WS_URL)
                # Declare button-lock support. LVA reflects the saved lock state
                # back to us in the snapshot, so no extra state is needed.
                await ws.send(json.dumps({"command": "register_button_lock"}))
                async for raw in ws:
                    await handle_lva_message(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            log("LVA loop error, reconnecting")
            traceback.print_exc()
            lva_sock = None
            await asyncio.sleep(RECONNECT_DELAY)


# --------------------------------------------------------------------------- #
# PipeWire volume control
# --------------------------------------------------------------------------- #


async def run_cmd(cmd: list[str]) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    stdout, stderr = await proc.communicate()
    try:
        stdout = stdout.decode().strip()
        stderr = stderr.decode().strip()
    except UnicodeDecodeError:
        stdout = stdout.decode("ascii", "ignore").strip()
        stderr = stderr.decode("ascii", "ignore").strip()
    if proc.returncode != 0:
        raise RuntimeError("command failed", cmd, stdout, stderr)
    return stdout, stderr


async def wpctl(mod: str) -> None:
    """Adjust the PipeWire sink volume by ``mod`` (e.g. "+10%"), then mute-unmute
    based on the resulting volume so volume 0 maps to hardware mute."""
    try:
        await run_cmd(["wpctl", "set-volume", "-l", "1.0", PW_SINK, mod])
    except Exception:
        log("wpctl set-volume failed")
        return
    try:
        vol, _ = await run_cmd(["wpctl", "get-volume", PW_SINK])
    except Exception:
        return
    match = re.search(r"Volume:\s*([0-9.]+)", vol)
    if not match:
        return
    if float(match.group(1)) <= 0:
        await run_cmd(["wpctl", "set-mute", PW_SINK, "1"])
    else:
        await run_cmd(["wpctl", "set-mute", PW_SINK, "0"])


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def main() -> None:
    async with asyncio.TaskGroup() as tg:
        # Each loop is self-healing: discovery manages USB, wsloop manages LVA.
        tg.create_task(manager.loop())
        tg.create_task(wsloop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        pass  # clean shutdown (SIGINT/SIGTERM)
    except Exception:
        traceback.print_exc()
