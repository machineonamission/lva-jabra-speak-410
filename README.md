# LVA + Jabra Speak 410

https://github.com/user-attachments/assets/edbfcc84-8e09-4f04-b039-65113ec8f7ae

## NOTE

this is dependent on [the **WIP** LVA peripheral API](https://github.com/OHF-Voice/linux-voice-assistant/pull/266). Your
LVA must be, in some way, based on [LVA pull #266](https://github.com/OHF-Voice/linux-voice-assistant/pull/266), if it
has not yet merged into main (hasn't at time of writing).

I
provide [some pre-built images](https://github.com/machineonamission/linux-voice-assistant/pkgs/container/linux-voice-assistant/)
of different WIP forks of LVA based on this:

- [peripheral api (#266)](https://github.com/OHF-Voice/linux-voice-assistant/pull/266):
  `ghcr.io/machineonamission/linux-voice-assistant:leds-and-buttons-events`
- [peripheral api (#266)](https://github.com/OHF-Voice/linux-voice-assistant/pull/266) + [pipewire volume control (#272)](https://github.com/OHF-Voice/linux-voice-assistant/pull/272):
  `ghcr.io/machineonamission/linux-voice-assistant:leds-and-pipewire`
- [peripheral api (#266)](https://github.com/OHF-Voice/linux-voice-assistant/pull/266) +
  [pipewire volume control (#272)](https://github.com/OHF-Voice/linux-voice-assistant/pull/272)
  [listen during wakeword (#273)](https://github.com/OHF-Voice/linux-voice-assistant/pull/273):
  `ghcr.io/machineonamission/linux-voice-assistant:led-pipewire-wakelisten`

## setup

i've provided [a demo quadlet file](jabra.container)

### peripheral api

make sure LVA exposes port `6055` (peripheral API) and that this image can read that port

env variable `LVA_WS_URL` is the peripheral API URL. defaults to `ws://0.0.0.0:6055`, which should work if LVA runs on
the same machine and ports are exposed

### hidapi

this image mostly relies on [`hidapi`](https://pypi.org/project/hidapi/) to read/write LEDs/buttons, which means you
need to add `/dev/bus/usb` as a device

### pipewire

optionally set the env var `PW_SINK` to use a different pipewire sink than the system default

#### volume control

this isn't exactly intended behavior, but it seems `hidapi` takes full control of the USB HID stuff. this repo provides
volume control as well, but it's disabled by default. this means things like
[`alsa_volume_from_usb_hid`](https://github.com/neildavis/alsa_volume_from_usb_hid) will break.

the env var `VOLUME_CONTROLLER` sets this. 
- set it to `pipewire` to make it mod pipewire volume on button presses (works
well with  [pipewire volume control (#272)](https://github.com/OHF-Voice/linux-voice-assistant/pull/272)). 
- set to `lva`
to send LVA
volume commands (WARNING: be careful with this so that your system isn't recieving two volume down events, or the
internal volume controls aren't kicking in, resulting in multiple stacking volume down)
- any other value, including the default, will result in ignoring volume button presses

#### mute button bodge

the jabra speak is weird and currently i haven't found a way to detect the mute _button_ when the device doesn't think
it's in a call. BUT, you can expose pipewire to the container and it can read the mic, listen for all
0s (digital mute, impossible in real conditions) and trigger LVA-side mute from there, as well as being able to respond
to unmute
requests. [see LVA docs for exposing pipewire, procedure is the same](https://github.com/OHF-Voice/linux-voice-assistant/blob/main/docs/install_audioserver.md)

## internal workings

the jabra speak is bizarre, it's this god awful internal telephony state machine, and many buttons change their behavior
based on its percieved LED state (i guess to make it work better as plug-and-play). there are weird USB HID endpoints of
a ton of just Bytes, but i don't know how they work (any attempts jsut crash the thing). not sure if anything short of
custom firmware could override its behavior.
