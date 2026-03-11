import sys
import time
import argparse
from typing import Optional
import serial
import struct
from time import sleep
from pathlib import Path

import serial.tools.list_ports


def find_teensy_port() -> Optional[str]:
    """
    Try to auto-detect a Teensy/USB-UART device.
    Looks for "Teensy", "usbmodem", "ttyACM" or common USB serial identifiers.
    """
    by_id = Path("/dev/serial/by-id")
    if not by_id.exists():
        return None

    for p in by_id.iterdir():
        name = p.name.lower()
        if "teensy" in name:
            return str(p.resolve())

    return None
    # for p in serial.tools.list_ports.comports():
    #     desc = (p.description or "").lower()
    #     name = (p.device or "").lower()
    #     print(f"Checking port: {p.device}, desc: {desc}")
    #     print(f"Checking port: {p.device}, name: {name}")
    #     if "teensy" in desc or "teensy" in name:
    #         return p.device
    # # fallback heuristics
    # for p in serial.tools.list_ports.comports():
    #     name = (p.device or "").lower()
    #     if (
    #         "usbmodem" in name
    #         or "ttyacm" in name
    #         or "usbserial" in name
    #         # or "ttyusb" in name
    #     ):
    #         return p.device
    # return None


def send_command(
    v_value: float,
    w_value: float,
    port: Optional[str] = None,
    baud: int = 115200,
    timeout: float = 1.0,
) -> str:
    """
    Open serial port to Teensy and send a command value (as a line).
    Returns the first line of response (empty string if none).
    """
    if port is None:
        port = find_teensy_port()
        if port is None:
            raise RuntimeError("Teensy port not found. Provide port explicitly.")
    with serial.Serial(port, baud, timeout=timeout) as ser:
        v = float(v_value)
        w = float(w_value)
        payload = b"\xaa\x55" + struct.pack("<ff", v, w) + b"\x55\xaa"
        ser.write(payload)
        # give device a short moment to respond
        time.sleep(0.05)
        try:
            resp = ser.readline().decode("utf-8", errors="replace").strip()
        except Exception:
            resp = ""
        return resp


def main(argv):
    parser = argparse.ArgumentParser(
        description="Send command value to Teensy over USB (UART)."
    )
    parser.add_argument("v_value", type=float, help="Command value to send (float).")
    parser.add_argument("w_value", type=float, help="Command value to send (float).")
    parser.add_argument("--port", "-p", help="Serial port (auto-detected if omitted).")
    parser.add_argument(
        "--baud", "-b", type=int, default=115200, help="Baud rate (default 115200)."
    )
    args = parser.parse_args(argv)

    try:
        print(
            f"Sending v={args.v_value}, w={args.w_value} to port={args.port or 'auto-detected'} at {args.baud} baud..."
        )
        resp = send_command(args.v_value, args.w_value, port=args.port, baud=args.baud)
        sleep(0.1)  # wait a moment for any response to arrive
        if resp:
            print(resp)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
