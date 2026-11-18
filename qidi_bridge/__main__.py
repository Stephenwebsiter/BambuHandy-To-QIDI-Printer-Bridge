"""Start the bridge:  python -m qidi_bridge"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config
from .webserver import local_ips, serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qidi_bridge", description="Print to a QIDI printer from your phone.")
    ap.add_argument("--port", type=int, help="web page port (default 8765)")
    ap.add_argument("--host", default="0.0.0.0", help="network address to listen on")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "setup-studio"],
                    help="run the bridge (default) or set up the Bambu Studio printer")
    args = ap.parse_args(argv)
    if args.command == "setup-studio":
        return setup_studio()

    cfg = config.load()
    httpd = serve(args.port, args.host)
    port = httpd.server_address[1]
    print(f"QIDI print bridge {__version__} is running.")
    print("Open one of these on your phone (same Wi-Fi as this PC):")
    for ip in local_ips() or ["<this-pc-ip>"]:
        print(f"    http://{ip}:{port}")
    print(f"On this PC: http://localhost:{port}")
    if not cfg.get("printer_host"):
        print("First time? Open the page and it will help you find your printer.")
    if cfg.get("virtual_printer_enabled"):
        try:
            from .virtual_printer import start_virtual_printer
            start_virtual_printer(httpd)
        except Exception as e:  # never let step 2 break the phone page
            print(f"The Bambu Studio printer could not start: {e}")
    print("Leave this window open while printing. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.")
    return 0


def setup_studio() -> int:
    """Switch on the pretend Bambu printer and teach Bambu Studio / OrcaSlicer to trust it."""
    from . import certs, virtual_printer

    cfg = virtual_printer.ensure_identity(config.save({"virtual_printer_enabled": True}))
    ip = virtual_printer.pick_ip(cfg)
    try:
        certs.ensure_certificates(cfg["virtual_printer_serial"], [ip])
    except certs.CertError as e:
        print(e)
        return 1
    files = certs.slicer_cert_files()
    if not files:
        print("Could not find Bambu Studio or OrcaSlicer. Install one, then run this again.")
    ok = True
    for f in files:
        try:
            added = certs.install_ca_into(f)
            print(("Added the bridge to " if added else "Already set up: ") + f)
        except PermissionError:
            ok = False
            print(f"Windows would not let me change {f}. Run 'Set up Bambu Studio.bat' again and click Yes "
                  "when Windows asks for permission.")
    print()
    print("In Bambu Studio, the printer will show up as:")
    print(f"    Name: {cfg['virtual_printer_name']}   IP address: {ip}")
    print(f"    Access code: {cfg['virtual_printer_access_code']}")
    print("Close Bambu Studio completely and open it again so it picks up the change.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
