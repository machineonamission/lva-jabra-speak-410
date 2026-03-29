# LVA + Jabra Speak 410

![demo.mp4](demo.mp4)

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

make sure LVA exposes port `6055` (peripheral API) and that this image can read that port

env variable `LVA_WS_URL` is the peripheral API URL. defaults to `ws://0.0.0.0:6055`, which should work if LVA runs on
the same machine and ports are exposed

this image mostly relies on [`hidapi`](https://pypi.org/project/hidapi/) to read/write LEDs/buttons, which means you
need to expose /dev to the container

the jabra speak is weird and currently i haven't found a way to detect the mute _button_ when the device doesn't think
it's in a call. BUT, you can expose pipewire to the container (same way as LVA) and it can read the mic, listen for all
0s (digital mute) and trigger LVA-side mute from there, as well as being able to respond to unmute requests.