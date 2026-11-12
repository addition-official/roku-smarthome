import argparse
import json
import sys
import time

from .client import (RokuSmartHome, SessionExpired, RokuUnavailable, UnsupportedCommand,
                     DeviceNotFound, CommandFailed)

# Exit codes, so scripts and cron jobs can tell failures apart.
# (2 is argparse's code for a bad command line.)
EXIT_ERROR = 1            # bad input, unknown device, unsupported command
EXIT_SESSION_EXPIRED = 3  # saved login no longer works: run `login` again
EXIT_UNAVAILABLE = 4      # Roku unreachable or erroring: retry later
EXIT_COMMAND_FAILED = 5   # Roku received the command and reported an error


def parse_color(values):
    """Accept either one hex color (#ff8000, ff8000, #f80) or three 0-255 numbers."""
    if len(values) == 1:
        h = values[0].lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError(f"not a hex color: {values[0]!r} (expected like #ff8000)")
        try:
            return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
        except ValueError:
            raise ValueError(f"not a hex color: {values[0]!r} (expected like #ff8000)") from None
    if len(values) == 3:
        try:
            return [int(v) for v in values]
        except ValueError:
            raise ValueError(f"rgb values must be whole numbers, got {' '.join(values)}") from None
    raise ValueError("give a hex color like #ff8000 or three numbers like 255 128 0")


def describe(d):
    extra = ""
    if d.brightness is not None:
        extra += f" {d.brightness}%"
    if d.color_temp is not None:
        extra += f" {d.color_temp}K"
    if d.color_rgb:
        extra += f" rgb{tuple(d.color_rgb)}"
    return (f"{d.name:32} {d.type:8} {(d.power or '?').upper():4} "
            f"({'online' if d.online else 'OFFLINE'}){extra}")


def fail(message, code):
    print(message, file=sys.stderr)
    sys.exit(code)


def main(argv=None):
    p = argparse.ArgumentParser(prog="roku-smarthome",
                                description="Control Roku Smart Home devices.")
    p.add_argument("--cookies", help="path to cookie file (default: ~/.config/roku-smarthome/cookies.json)")
    p.epilog = ("Device names can be shortened to any unique part of the name, "
                "e.g. 'porch' for 'Porch Camera'. "
                "Exit codes: 0 ok, 1 error, 2 bad arguments, "
                "3 session expired (run login), 4 Roku unavailable (retry later), "
                "5 Roku reported the command failed.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login", help="save a Cookie header copied from my.roku.com/smarthome")
    sp.add_argument("cookie_header", nargs="?", help="omit to be prompted")

    sub.add_parser("status", help="list devices and state")
    sub.add_parser("json", help="dump raw device list as JSON")

    sp = sub.add_parser("watch", help="print device changes as they happen (Ctrl-C to stop)")
    sp.add_argument("--seconds", type=float, help="stop after this many seconds")

    for name in ("on", "off", "toggle"):
        sp = sub.add_parser(name)
        sp.add_argument("device")

    sp = sub.add_parser("brightness")
    sp.add_argument("device")
    sp.add_argument("level", type=int, help="0-100")

    sp = sub.add_parser("temp", help="set white color temperature")
    sp.add_argument("device")
    sp.add_argument("kelvin", type=int, help="1800-6500 (white bulbs: 2700-6500)")

    sp = sub.add_parser("rgb", help="set RGB color (color bulbs only)")
    sp.add_argument("device")
    sp.add_argument("color", nargs="+", metavar="COLOR",
                    help="hex like '#ff8000', or three numbers 0-255 like 255 128 0")

    args = p.parse_args(argv)
    rk = RokuSmartHome(args.cookies)

    try:
        if args.cmd == "login":
            header = args.cookie_header or input("Paste Cookie header value: ").strip()
            # checked against Roku before saving; a bad paste keeps the old login
            n = rk.import_cookie_header(header, verify=True)
            print(f"Login verified. Saved {n} cookies to {rk.cookie_file}")
            return

        if args.cmd == "status":
            for d in rk.devices():
                print(describe(d))
            return

        if args.cmd == "watch":
            rk.on_change(lambda d: print(f"{time.strftime('%H:%M:%S')}  {describe(d)}",
                                         flush=True))
            print("Watching for changes (Ctrl-C to stop)...", flush=True)
            try:
                if args.seconds is not None:
                    time.sleep(args.seconds)
                else:
                    while True:
                        time.sleep(3600)
            except KeyboardInterrupt:
                pass
            return

        if args.cmd == "json":
            print(json.dumps(rk.raw_devices(), indent=2))
            return

        d = rk.find(args.device)
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
            d.set_color_rgb(*parse_color(args.color))
        print(f"OK: {args.cmd} -> {d.name}")

    except SessionExpired as e:
        fail(f"SESSION EXPIRED: {e}", EXIT_SESSION_EXPIRED)
    except RokuUnavailable as e:
        fail(f"ROKU UNAVAILABLE: {e}", EXIT_UNAVAILABLE)
    except CommandFailed as e:
        fail(f"COMMAND FAILED: {e}", EXIT_COMMAND_FAILED)
    except (DeviceNotFound, UnsupportedCommand, ValueError) as e:
        fail(f"Error: {e}", EXIT_ERROR)
    finally:
        rk.close()


if __name__ == "__main__":
    main()