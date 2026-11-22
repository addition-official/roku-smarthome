import argparse
import getpass
import json
import math
import os
import sys
import threading
import time

from .client import (RokuSmartHome, SessionExpired, RokuUnavailable, UnsupportedCommand,
                     DeviceNotFound, CommandFailed)

# Exit codes, so scripts and cron jobs can tell failures apart.
# (2 is argparse's code for a bad command line.)
EXIT_ERROR = 1            # bad input, unknown device, unsupported command
EXIT_SESSION_EXPIRED = 3  # saved login no longer works: run `login` again
EXIT_UNAVAILABLE = 4      # Roku unreachable or erroring: retry later
EXIT_COMMAND_FAILED = 5   # Roku received the command and reported an error
EXIT_INTERRUPTED = 130    # Ctrl-C (the shell convention: 128 + SIGINT)


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


def printable(text) -> str:
    """Device names come from the account; don't let control characters
    (e.g. terminal escape sequences) reach the terminal."""
    return "".join(c if c.isprintable() else "?" for c in str(text))


def positive_seconds(value):
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not math.isfinite(f) or f <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {value!r}")
    return f


def describe(d, depth=0):
    extra = ""
    if d.brightness is not None:
        extra += f" {d.brightness}%"
    if d.color_temp is not None:
        extra += f" {d.color_temp}K"
    if d.color_rgb:
        extra += f" rgb{tuple(d.color_rgb)}"
    name = "  " * depth + printable(d.name)  # a group's devices indented under it
    return (f"{name:32} {printable(d.type):8} {(d.power or '?').upper():4} "
            f"({'online' if d.online else 'OFFLINE'}){extra}")


def fail(message, code):
    print(message, file=sys.stderr)
    sys.exit(code)


def _quiet_stdout():
    """Output was closed (e.g. piped into `head`): stop writing to it without
    an error at exit."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def main(argv=None):
    # A terminal that can't show a device's emoji gets a "?" instead of an
    # error after the command already ran.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    try:
        _main(argv)
    except BrokenPipeError:
        _quiet_stdout()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(EXIT_INTERRUPTED)


def _main(argv=None):
    p = argparse.ArgumentParser(prog="roku-smarthome",
                                description="Control Roku Smart Home devices.")
    p.add_argument("--cookies", help="path to cookie file (default: $ROKU_SMARTHOME_COOKIES, "
                                     "or ~/.config/roku-smarthome/cookies.json)")
    p.epilog = ("Device names can be shortened to any unique part of the name, "
                "e.g. 'porch' for 'Porch Camera'. "
                "Exit codes: 0 ok, 1 error, 2 bad arguments, "
                "3 session expired (run login), 4 Roku unavailable (retry later), "
                "5 Roku reported the command failed, 130 interrupted (Ctrl-C).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login", help="save a Cookie header copied from my.roku.com/smarthome")
    sp.add_argument("cookie_header", nargs="?",
                    help="omit to be prompted (recommended: a cookie on the command "
                         "line ends up in your shell history)")

    sub.add_parser("status", help="list devices and state")
    sub.add_parser("json", help="dump raw device list as JSON")

    sp = sub.add_parser("watch", help="print device changes as they happen (Ctrl-C to stop)")
    sp.add_argument("--seconds", type=positive_seconds, help="stop after this many seconds")

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
    rk = RokuSmartHome(os.path.expanduser(args.cookies) if args.cookies else None)

    try:
        if args.cmd == "login":
            header = args.cookie_header
            if header is None:
                try:
                    # not echoed, so the cookie doesn't stay on screen
                    header = getpass.getpass("Paste Cookie header value (hidden): ").strip()
                except EOFError:
                    fail("Error: no cookie given (input was closed)", EXIT_ERROR)
            # checked against Roku before saving; a bad paste keeps the old login
            try:
                n = rk.import_cookie_header(header, verify=True)
            except OSError as e:
                fail(f"Error: could not save the login to {rk.cookie_file}: "
                     f"{e.strerror or e}", EXIT_ERROR)
            print(f"Login verified. Saved {n} cookies to {rk.cookie_file}")
            return

        if args.cmd == "status":
            depth = {}
            for d in rk.devices(include_members=True):  # a group's devices under it
                depth[d.id] = depth[d.group_id] + 1 if d.group_id else 0
                print(describe(d, depth[d.id]))
            return

        if args.cmd == "watch":
            output_closed = threading.Event()

            def show(d):
                if output_closed.is_set():
                    return
                try:
                    print(f"{time.strftime('%H:%M:%S')}  {describe(d)}", flush=True)
                except (BrokenPipeError, OSError):
                    output_closed.set()  # e.g. `watch | head -3` is done

            # header first, so it can't land after the first change
            print("Watching for changes (Ctrl-C to stop)...", flush=True)
            rk.on_change(show)
            end = None if args.seconds is None else time.monotonic() + args.seconds
            try:
                while not output_closed.wait(1.0):
                    if end is not None and time.monotonic() >= end:
                        break
                    if isinstance(rk.live_error, SessionExpired):
                        raise rk.live_error
            except KeyboardInterrupt:
                pass
            if output_closed.is_set():
                _quiet_stdout()
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