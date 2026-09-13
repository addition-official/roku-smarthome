[![PyPI](https://img.shields.io/pypi/v/roku-smarthome?cacheSeconds=300)](https://pypi.org/project/roku-smarthome/)

# roku-smarthome

Control Roku Smart Home plugs and bulbs from Python. Read status, switch power, set brightness and color temperature, from a command line or as a library you drop into your own project.

Roku publishes no API for its Smart Home line, and the only integrations are Alexa and Google Assistant. This package talks to the same backend that Roku's own web app at [my.roku.com/smarthome](https://my.roku.com/smarthome) uses, with your own account session. **No reverse-engineered mobile app login, no email verification codes, no soldering.** Log in once and it stays logged in.

Not affiliated with or endorsed by Roku.

Developed and tested against the **Roku Indoor Smart Plug SE (PS1000X)** and the **Roku Smart Bulb (BS1000X)**. Cameras show up in the device list with online state; video is out of scope.

## What it can do

- List every device on your Roku Smart Home account with its state: power, online, brightness, color temperature
- Turn plugs and bulbs on and off
- Set bulb brightness (0 to 100) and white color temperature (2700K to 6500K on the tested bulb)
- Set RGB color on color bulbs (implemented from Roku's web app, untested on hardware; see notes)
- Send any other command a device advertises, via a generic escape hatch
- Keep itself logged in: Roku renews the session on every request and this client saves the renewed cookie, so the login never expires as long as you use it at least once a year

## Install

    pip install roku-smarthome

Or clone this repo and `pip install .` to build the exact package that's published to PyPI.

## Requirements

- Python 3.9+
- `requests` and `websockets` (installed automatically by pip)

## Setup

### 1. Copy your session cookie (once)

1. Sign in at https://my.roku.com/smarthome in a desktop browser.
2. Open DevTools (F12) and go to the **Network** tab. Type `leaves` in the filter box and reload the page.
3. Click the `leaves` request. In **Headers**, scroll down to **Request Headers** and copy the entire value of the `Cookie` header. (Right-click the request > Copy > "Copy as cURL" also works; the value is on the `-H 'Cookie: ...'` line.)

### 2. Save it

```
roku-smarthome login
```

Paste the cookie value when prompted. It's verified against your account and saved to a per-user file (`%USERPROFILE%\.config\roku-smarthome\cookies.json` on Windows, `~/.config/roku-smarthome/cookies.json` on macOS/Linux). Set `ROKU_SMARTHOME_COOKIES` or pass `--cookies PATH` to use a different location.

> **Keep that file private.** It is a working login to your Roku account. Don't share it, and don't paste the cookie into chat windows or bug reports. It's stored outside the project folder, so it can't be committed by accident.

### 3. Run

```
roku-smarthome status
```

## Command line

```
roku-smarthome status                                   # list devices and state
roku-smarthome on "Living room lamp"
roku-smarthome off "Living room lamp"
roku-smarthome toggle "Living room lamp"
roku-smarthome brightness "Bedroom bulb" 40             # 0-100
roku-smarthome temp "Bedroom bulb" 3000                 # Kelvin
roku-smarthome rgb "Color bulb" 255 80 0                # color bulbs only, untested
roku-smarthome json                                     # raw device list, for piping elsewhere
roku-smarthome --cookies PATH status                    # use a different cookie file
```

Device names are matched case-insensitively against the names in the Roku Smart Home app.

## Use it as a library

```python
from roku_smarthome import RokuSmartHome

rk = RokuSmartHome()                 # uses the default cookie file
for d in rk.devices():
    print(d.name, d.type, d.power, d.online, d.brightness, d.color_temp)

lamp = rk.get("Living room lamp")
lamp.turn_on()
lamp.turn_off()
lamp.toggle()
print(lamp.is_on)

bulb = rk.get("Bedroom bulb")
bulb.set_brightness(40)
bulb.set_color_temp(3000)
bulb.set_color_rgb(255, 80, 0)       # color bulbs only, untested
bulb.refresh()                       # re-fetch state from Roku
print(bulb.brightness, bulb.color_temp)

# anything else the device advertises in supportedCommands
bulb.send("color", {"colorType": "temperature", "temperature": 4000})
```

`Device` attributes: `id`, `name`, `type` (`plug`, `light`, `camera`), `model`, `local_ip`, `power` (`"on"`/`"off"`), `online`, `brightness`, `color_temp`, `color_type`, `color_rgb`, `supported_commands`, and `raw` (the untouched JSON).

Exceptions: `SessionExpired` (saved cookie no longer works; run `login` again), `DeviceNotFound`, and `UnsupportedCommand` (raised instead of silently sending a command the device doesn't list).

## Keeping it logged in

Every request to Roku's API comes back with a re-issued session cookie carrying a fresh 365-day expiry. This client merges that cookie back into its file on every call. In practice: if anything uses this library at least once a year, the login is permanent. A daily cron entry like

    0 9 * * * /path/to/venv/bin/roku-smarthome status > /dev/null

is enough on a machine that would otherwise sit idle.

Roku's sign-in page emails a verification code on every new login, which is why this package deliberately does **not** try to automate the email/password flow. Copying the cookie once is simpler and more reliable.

## How it works

- `GET https://my.roku.com/smarthome/api/v1/leaves` returns every device ("leaf") with its current state.
- Commands go over a websocket at `wss://aspen-sockets.aspen.msc.roku.com/v1/socket` as
  `{"id": ..., "type": "send_command", "payload": {"leafId": ..., "leafType": "device", "command": {"command": ..., "parameters": {...}}}}`.
- Known commands: `power {"power": "on"|"off"}`, `brightness {"level": 0-100}`, `color {"colorType": "temperature", "temperature": K}`, `color {"colorType": "rgb", "rgb": [r, g, b]}`.
- Authentication is the `ks.session` cookie from the browser, sent on both the REST call and the websocket handshake.

The Roku Smart Home line is Wyze hardware sold under the Roku brand, but the devices are registered to Roku's cloud, not Wyze's, so Wyze libraries can't log in to them. The earlier community effort (a 2022 fork of `wyzeapy` that reproduced the Roku mobile app's AWS Cognito login) stopped working when Roku removed that endpoint.

## Notes and limitations

- **Cloud only.** The plugs have a local API on port 88 but it's encrypted with a key that only the Roku/Wyze login flow hands out. Local control would mean flashing ESPHome or Tasmota onto the ESP32 inside, which requires opening the device.
- **Unofficial.** Roku could change or remove these endpoints at any time. If `status` starts failing with something other than `SESSION EXPIRED`, that's probably what happened; please open an issue with the error (and the cookie value removed).
- **Cameras** are listed with name and online state only. Streams, events and settings are visible in the API but not implemented here.
- **RGB color is implemented but untested.** I don't own a color bulb. The parameter shape (`{"colorType": "rgb", "rgb": [r, g, b]}`, each 0 to 255) is taken directly from Roku's web app, so it should work. If you have a color bulb, please confirm in an issue either way.
- Commands are fire-and-forget over the websocket. Call `refresh()` if you need to confirm the new state.

## Project layout

```
src/roku_smarthome/
    __init__.py       package entry, exports RokuSmartHome and Device
    client.py         the library: cookie handling, device list, commands
    cli.py            the command line
pyproject.toml        packaging / build config
LICENSE               MIT
```

Clone and `pip install .` to build exactly what's published to PyPI.

## A note on how this was built

This started as "can I read my smart plug from Python," went through a dead 2022 library, a Windows DNS bug, and a login endpoint Roku had deleted, before landing on the web app's own API. Watching the network traffic on my.roku.com/smarthome showed that the device list included plugs and bulbs even though the page only displays cameras; from there the websocket command format came out of the web app's JavaScript, was confirmed on real hardware, and testing showed that Roku renews the session cookie on every request, which is what makes this usable long-term without any login automation.

I used AI assistance throughout: for digging through the minified web app, for the client and CLI code, and for this README. The testing on real devices, the decisions about what to build and what to leave out, and the packaging are mine.

## Acknowledgements

Thanks to the people in the [Home Assistant community thread](https://community.home-assistant.io/t/roku-smart-home-integration/477613) who mapped the original Wyze/Roku relationship and the 2022 mobile-app login, and to [wyze-sdk](https://github.com/shauntarves/wyze-sdk) and [wyzeapy](https://github.com/SecKatie/wyzeapy) for showing what the underlying hardware speaks.
