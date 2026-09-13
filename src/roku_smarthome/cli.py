import argparse
import json
import sys

from .client import RokuSmartHome, SessionExpired, UnsupportedCommand, DeviceNotFound


def main(argv=None):
    p = argparse.ArgumentParser(prog="roku-smarthome",
                                description="Control Roku Smart Home devices.")
    p.add_argument("--cookies", help="path to cookie file (default: ~/.config/roku-smarthome/cookies.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login", help="save a Cookie header copied from my.roku.com/smarthome")
    sp.add_argument("cookie_header", nargs="?", help="omit to be prompted")

    sub.add_parser("status", help="list devices and state")
    sub.add_parser("json", help="dump raw device list as JSON")

    for name in ("on", "off", "toggle"):
        sp = sub.add_parser(name)
        sp.add_argument("device")

    sp = sub.add_parser("brightness")
    sp.add_argument("device")
    sp.add_argument("level", type=int, help="0-100")

    sp = sub.add_parser("temp", help="set white color temperature")
    sp.add_argument("device")
    sp.add_argument("kelvin", type=int, help="e.g. 2700-6500")

    sp = sub.add_parser("rgb", help="set RGB color (color bulbs only)")
    sp.add_argument("device")
    sp.add_argument("r", type=int, help="0-255")
    sp.add_argument("g", type=int, help="0-255")
    sp.add_argument("b", type=int, help="0-255")

    args = p.parse_args(argv)
    rk = RokuSmartHome(args.cookies)

    try:
        if args.cmd == "login":
            header = args.cookie_header or input("Paste Cookie header value: ").strip()
            n = rk.import_cookie_header(header)
            print(f"Saved {n} cookies to {rk.cookie_file}")
            rk.devices()  # verify it works
            print("Login verified.")
            return

        if args.cmd == "status":
            for d in rk.devices():
                extra = ""
                if d.brightness is not None:
                    extra += f" {d.brightness}%"
                if d.color_temp is not None:
                    extra += f" {d.color_temp}K"
                if d.color_rgb:
                    extra += f" rgb{tuple(d.color_rgb)}"
                print(f"{d.name:32} {d.type:8} {(d.power or '?').upper():4} "
                      f"({'online' if d.online else 'OFFLINE'}){extra}")
            return

        if args.cmd == "json":
            print(json.dumps(rk.raw_devices(), indent=2))
            return

        d = rk.get(args.device)
        if args.cmd == "on":
            d.turn_on()
        elif args.cmd == "off":
            d.turn_off()
        elif args.cmd == "toggle":
            d.toggle()
        elif args.cmd == "brightness":
            d.set_brightness(args.level)
        elif args.cmd == "temp":
            d.set_color_temp(args.kelvin)
        elif args.cmd == "rgb":
            d.set_color_rgb(args.r, args.g, args.b)
        print(f"OK: {args.cmd} -> {d.name}")

    except SessionExpired as e:
        sys.exit(f"SESSION EXPIRED: {e}")
    except (DeviceNotFound, UnsupportedCommand, ValueError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()