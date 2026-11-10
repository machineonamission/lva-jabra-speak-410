# LVA + Jabra Speak 410

https://github.com/user-attachments/assets/edbfcc84-8e09-4f04-b039-65113ec8f7ae

## What this is

A peripheral controller for the [Jabra Speak 410](https://www.jabra.com/business/speakerphones/jabra-speak-series/jabra-speak-410)
USB speakerphone. It runs alongside the Linux Voice Assistant (LVA) container,
connects to the Jabra over USB HID, drives its LED ring to reflect the
assistant state, and maps its buttons to the LVA peripheral API. It also has an
optional PipeWire integration for volume control.

This is also the upstream example in
[OHF-Voice/linux-voice-assistant](https://github.com/OHF-Voice/linux-voice-assistant)
(`examples/Jabra Speak 410/`), so it tracks the **current** peripheral API.

## Peripheral API

The controller talks to LVA over the [peripheral WebSocket API](https://github.com/OHF-Voice/linux-voice-assistant/blob/main/linux_voice_assistant/peripheral_api.py),
which is merged into LVA `main`. Your LVA does **not** need a special fork — any
recent LVA build with the peripheral API works.

- Expose LVA's peripheral port `6055` to this container.
- `LVA_WS_URL` (env) is the peripheral API URL. Defaults to `ws://0.0.0.0:6055`,
  which works if LVA runs on the same host with host networking.

## hidapi (USB)

`hidapi` reads/writes the LED ring and buttons, so the container needs the USB
bus:

```
AddDevice=/dev/bus/usb
```

On the host, optionally add a udev rule so the container can access the device:

```bash
sudo tee /etc/udev/rules.d/99-jabra.rules << 'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="0b0e", MODE="0666", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

The controller dynamically discovers the device on plug and replugs, so it
survives being yanked out of the USB port.

## pipewire (optional volume control)

Set `PW_SINK` (env) to override the PipeWire sink used for volume control.

### Volume control

`hidapi` takes full ownership of the USB HID interface, so tools that rely on
HID volume events (e.g. `alsa_volume_from_usb_hid`) won't work while this runs.

`VOLUME_CONTROL` (env) sets the volume-button behaviour:

- `pipewire` — adjust the PipeWire sink directly via `wpctl` (recommended with
  LVA's pipewire volume control).
- `lva` — send `volume_up` / `volume_down` commands to LVA. **Warning:** can
  double-trigger if your OS also processes HID volume events.
- anything else / unset — ignore the volume buttons.

### Button lock

On connect the controller registers LVA's **"Disable button controls"** switch
(`switch.<satellite>_disable_button_controls`). When enabled from Home Assistant,
all button commands (call/hang, volume, mute) stop being sent to LVA.

## Internal workings

The Jabra Speak 410 is bizarre: it runs an internal telephony state machine, and
many buttons change behaviour based on its perceived LED state (to work as
plug-and-play). The HID reports are a stream of opaque bytes; the documented
bits are enough to drive LEDs and read call/mute/volume, but there is no way to
override the device's firmware behaviour short of custom firmware.

## Robustness

- A discovery loop watches the USB bus and recreates the HID connection on plug
  and unplugs — the process never crashes on a disconnect.
- The LVA loop reconnects on its own and shows a red blink while disconnected.
- A failed I/O operation is logged and swallowed; it can never take down the
  whole process.
- Mute detection via the audio stream was removed (it was unreliable).
