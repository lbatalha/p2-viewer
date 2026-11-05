"""Command line access to the camera's vendor settings."""

from __future__ import annotations

import argparse
import sys

from .vendor import PALETTES, VendorControl, VendorError


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="p2-viewer-vendor",
        description="Read and set the P2 Pro's vendor configuration.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show device information")

    sub.add_parser("show", help="show the current configuration")

    palette = sub.add_parser("palette", help="read or set the preview palette")
    palette.add_argument("--set", type=int, metavar="ID",
                         help="palette id to set (see the 'palettes' command)")

    emis = sub.add_parser("emissivity", help="read or set object emissivity")
    emis.add_argument("--set", type=float, metavar="VALUE",
                      help="emissivity from 0.0 to 1.0")

    gain = sub.add_parser("gain", help="read or set the measurement range")
    gain.add_argument("--high", action="store_true", help="select high gain")
    gain.add_argument("--low", action="store_true", help="select low gain")

    dist = sub.add_parser("distance", help="read or set object distance")
    dist.add_argument("--set", type=float, metavar="METRES")

    sub.add_parser("palettes", help="list the known palette ids")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    try:
        cam = VendorControl()
    except VendorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "info":
            info = cam.device_info()
            print(f"firmware      : {info.firmware_version}")
            print(f"built         : {info.firmware_date}")
            print(f"qualification : {info.qualification}")
            print(f"IR sensor     : {info.ir_sensor}")
            print(f"part number   : {info.part_number}")
            print(f"serial number : {info.serial_number}")
            print(f"chip id       : {info.chip_id.hex(' ')}")
            print(f"sensor id     : {info.sensor_id.hex(' ')}")

        elif args.command == "show":
            print(f"palette       : {cam.get_palette()} "
                  f"({PALETTES.get(cam.get_palette(), 'unknown')})")
            print(f"emissivity    : {cam.get_emissivity():.4f}")
            print(f"transmittance : {cam.get_transmittance():.4f}")
            print(f"atmospheric T : {cam.get_atmospheric_temperature():.1f} K")
            print(f"reflected T   : {cam.get_reflected_temperature():.1f} K")
            print(f"distance      : {cam.get_distance():.3f} m")
            print(f"gain          : "
                  f"{'high' if cam.get_gain_select() else 'low'}")
            print(f"current vtemp : {cam.current_vtemp()}")
            print(f"shutter vtemp : {cam.shutter_vtemp()}")

        elif args.command == "palette":
            if args.set is not None:
                cam.set_palette(args.set)
                print(f"palette set to {args.set}")
            else:
                pid = cam.get_palette()
                print(f"palette: {pid} ({PALETTES.get(pid, 'unknown')})")

        elif args.command == "emissivity":
            if args.set is not None:
                cam.set_emissivity(args.set)
                print(f"emissivity set to {cam.get_emissivity():.4f}")
            else:
                print(f"emissivity: {cam.get_emissivity():.4f}")

        elif args.command == "gain":
            if args.high or args.low:
                cam.set_gain_select(args.high)
                print("gain set to", "high" if args.high else "low")
            else:
                print("gain:", "high" if cam.get_gain_select() else "low")

        elif args.command == "distance":
            if args.set is not None:
                cam.set_distance(args.set)
                print(f"distance set to {cam.get_distance():.3f} m")
            else:
                print(f"distance: {cam.get_distance():.3f} m")

        elif args.command == "palettes":
            for pid, name in sorted(PALETTES.items()):
                print(f"  {pid:2d}  {name}")
    except VendorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
