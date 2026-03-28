import asyncio
import shutil
from enum import IntFlag, Enum

import websockets
import json
import hid
import os

from websockets import ClientConnection

# --- CONFIGURATION ---
# Default to localhost if running on host network, or use LVA container name/IP
LVA_WS_URL = os.getenv("LVA_WS_URL", "ws://192.168.0.2:6055")

JABRA_VENDOR = 0x0b0e
JABRA_PRODUCT = 0x0412

interfaces = hid.enumerate()
USAGE_PAGE = 11

devices = []
for device in hid.enumerate(JABRA_VENDOR, JABRA_PRODUCT):
    if device['usage_page'] in [11, 12]:
        print("found ", device)

        serial = device['serial_number']
        devices.append(device['path'])


if len(devices) == 0:
    print("NO JABRA ALERT WAAAAA")
    # raise Exception("no jabra speak 410 found!")

class Telephony(IntFlag):
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
    # no clue
    button_7 = 1 << 11


class LEDs(IntFlag):
    off_hook = 1 << 0
    speaker = 1 << 1
    mute = 1 << 2
    ring = 1 << 3
    hold = 1 << 4
    microphone = 1 << 5
    # marked telephony and not LED, probably why it's a dupe of ring
    ringer = 1 << 6


class LEDState(IntFlag, Enum):
    default = 0
    three_green = LEDs.off_hook
    all_red = LEDs.mute | LEDs.off_hook
    ringing_and_flashing = LEDs.ring
    flashing = LEDs.ring | LEDs.off_hook
    partial_flash = LEDs.hold


class LVAEvent(str, Enum):
    """Events broadcast from LVA to peripheral clients."""

    WAKE_WORD_DETECTED = "wake_word_detected"
    LISTENING = "listening"
    THINKING = "thinking"
    TTS_SPEAKING = "tts_speaking"
    TTS_FINISHED = "tts_finished"
    ERROR = "error"
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
    STOP_LISTENING = "stop_listening"
    MUTE_MIC = "mute_mic"
    UNMUTE_MIC = "unmute_mic"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    STOP_TIMER_RINGING = "stop_timer_ringing"
    STOP_MEDIA_PLAYER = "stop_media_player"
    STOP_SPEAKING = "stop_speaking"


class JabraSpeak:
    def __init__(self, path):
        self.path = path
        self.device = hid.device()
        self.device.open_path(self.path)
        # we can do thread fuckery to make this work
        self.device.set_nonblocking(True)

    async def read(self):
        while True:
            try:
                read_info = self.device.read(8)
                if read_info:
                    if read_info[0] == 0x03:
                        return Telephony(read_info[1] | read_info[2] << 8)
                    else:
                        print(f"Packet {read_info} of unknown type")
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("fatal error in jabra loop: ", e)
                await asyncio.sleep(1)

    async def write(self, button_state: LEDs):
        return await asyncio.to_thread(self.device.write, [0x03, button_state & 0xff, (button_state & 0xff00) >> 8])

    async def readloop(self):
        while True:
            event = await self.read()
            print(f"from jabra: {event.name} {int(event):b}")
            # print(f"last event: {last_jabra_write.name}")

            if (
                    # hangup while talking
                    ((event & Telephony.flash) and last_jabra_write == LEDState.partial_flash)
                    # hangup while listening
                    or (event is None and last_jabra_write == LEDState.three_green)
                    # hangup while flashing?? wtf is button 7????
                    or (event & Telephony.button_7)
            ):
                print("jabra to lva: hangup detected")
                await write_to_lva(LVACommand.STOP_TIMER_RINGING)
                await write_to_lva(LVACommand.STOP_SPEAKING)
                await write_to_lva(LVACommand.STOP_LISTENING)
            # mute switch
            elif event & Telephony.mute:
                print("jabra to lva: mute toggle detected")
                global muted
                if muted:
                    await write_to_lva(LVACommand.UNMUTE_MIC)
                    muted = False
                else:
                    await write_to_lva(LVACommand.MUTE_MIC)
                    muted = True
            # call button
            elif (event & Telephony.hook_switch and last_jabra_write == LEDState.default
                  # damn thing fires the hook swicth when you unmUTE
                  and last_lva_write != LVACommand.UNMUTE_MIC):
                print("jabra to lva: call button detected")
                # if lva is glitched and i dont update the state machine, it will absolutely crap out
                await write_to_jabra(LEDState.flashing)

                await write_to_lva(LVACommand.START_LISTENING)

                await asyncio.create_task(listening_bodge())


# fixes a bug where it can get stuck on wakework detected
async def listening_bodge():
    await asyncio.sleep(0.5)
    if current_state == LVAEvent.WAKE_WORD_DETECTED:
        await write_to_lva(LVACommand.STOP_LISTENING)
        await write_to_jabra(LEDState.three_green)

last_jabra_write: LEDs | LEDState = LEDState.default
last_lva_write: LVACommand | None = None
devices = [JabraSpeak(d) for d in devices]


async def write_to_jabra(state: LEDs | LEDState):
    print(f"to jabra: {state.name} {int(state):b}")
    global last_jabra_write
    last_jabra_write = state
    return await asyncio.gather(*[d.write(state) for d in devices])


async def write_to_lva(command: LVACommand, data: dict = None):
    if lva_sock:
        global last_lva_write
        last_lva_write = command
        message = json.dumps({"command": command} | ({"data": data} if data else {}))
        print(f"to lva: {message}")
        await lva_sock.send(message)
    else:
        print("to lva: failed, no websocket")


lva_sock: None | ClientConnection = None


async def cool_error():
    for _ in range(4):
        await write_to_jabra(LEDState.all_red)
        await asyncio.sleep(0.1)
        await write_to_jabra(LEDState.default)
        await asyncio.sleep(0.1)


current_state: None | LVAEvent = None

muted: bool = False


async def wsloop():
    global lva_sock
    while True:
        try:
            async with websockets.connect(LVA_WS_URL) as websocket:
                print(f"Connected to LVA at {LVA_WS_URL}")
                lva_sock = websocket
                while True:
                    data = await websocket.recv()
                    print(f"from lva: {data}")
                    json_data = json.loads(data)
                    if json_data["event"] == "snapshot":
                        global muted
                        muted = json_data["data"]["muted"]
                        if muted:
                            await write_to_jabra(LEDState.all_red)
                    global current_state
                    try:
                        current_state = LVAEvent(json_data["event"])
                    except ValueError:
                        print(f"current state is not a valid event: {json_data['event']}")
                    match current_state:
                        case LVAEvent.WAKE_WORD_DETECTED:
                            await write_to_jabra(LEDState.flashing)
                        case LVAEvent.LISTENING:
                            await write_to_jabra(LEDState.three_green)
                        case LVAEvent.THINKING:
                            await write_to_jabra(LEDState.flashing)
                        case LVAEvent.TTS_SPEAKING:
                            await write_to_jabra(LEDState.partial_flash)
                        case LVAEvent.TTS_FINISHED:
                            await write_to_jabra(LEDState.default)
                        case LVAEvent.ERROR:
                            asyncio.create_task(cool_error())
                        case LVAEvent.IDLE:
                            muted = False
                            await write_to_jabra(LEDState.default)
                        case LVAEvent.MUTED:
                            muted = True
                            await write_to_jabra(LEDState.all_red)
                        case LVAEvent.TIMER_TICKING:
                            pass
                        case LVAEvent.TIMER_UPDATED:
                            pass
                        case LVAEvent.TIMER_RINGING:
                            await write_to_jabra(LEDState.flashing)
                        case LVAEvent.MEDIA_PLAYER_PLAYING:
                            pass
                        case LVAEvent.VOLUME_CHANGED:
                            pass
                        case LVAEvent.VOLUME_MUTED:
                            pass
                        case LVAEvent.ZEROCONF:
                            pass
                        case event:
                            print(f"Unknown event: {event}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("fatal error in lva loop: ", e)
            lva_sock = None
            await asyncio.sleep(1)


async def mute_detect_bodge():
    if shutil.which("pw-record") is None:
        print("pw-record not found. make sure wireplumber is installed.")
        return
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL
            )
            while True:
                chunk = await proc.stdout.readexactly(6400)
                global muted
                # all zeroes, mic has been muted
                if not any(chunk) and not muted:
                    muted = True
                    await write_to_lva(LVACommand.MUTE_MIC)
                    await write_to_jabra(LEDs.mute)


        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("fatal error in mute_detect_bodge: ", e)
            await asyncio.sleep(1)


async def main():
    async with asyncio.TaskGroup() as tg:
        # Spawn your infinite loops here
        tg.create_task(wsloop())
        tg.create_task(mute_detect_bodge())

        # tg.create_task(block_test())
        for d in devices:
            tg.create_task(d.readloop())

        # await asyncio.sleep(0.5)
        # await write_to_jabra(LEDs.microphone | LEDs.speaker)


asyncio.run(main())
