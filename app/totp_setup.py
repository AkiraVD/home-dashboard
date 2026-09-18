"""One-time TOTP setup for the web terminal.

Run it yourself in a terminal window on this machine (it shows the secret):

    ./venv/bin/python -m app.totp_setup            # create the key
    ./venv/bin/python -m app.totp_setup --force    # replace it (signs out every device)
    ./venv/bin/python -m app.totp_setup --check 123456
"""
import argparse
import os
import socket
import sys

import pyotp
import qrcode

from . import config


def _save(secret: str) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = config.TOTP_KEY_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret + "\n")
    os.replace(tmp, config.TOTP_KEY_FILE)


def _print_enrolment(secret: str, no_invert: bool) -> None:
    label = f"{os.environ.get('USER', 'user')}@{socket.gethostname()}"
    uri = pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name="Home dashboard")
    qr = qrcode.QRCode(border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    print("\nScan this with your authenticator app (Aegis, Google Authenticator, 2FAS, ...):\n")
    qr.print_ascii(invert=not no_invert)
    print("Can't scan? Enter this key manually (time-based, 6 digits, 30 s):")
    print("   ", " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up the TOTP second factor for the web terminal.")
    ap.add_argument("--force", action="store_true", help="replace the existing key (signs out every device)")
    ap.add_argument("--check", metavar="CODE", help="check a 6-digit code against the saved key")
    ap.add_argument("--show", action="store_true",
                    help="show the QR code for the existing key, to add it to another authenticator app")
    ap.add_argument("--no-invert", action="store_true", help="don't invert the QR code (for light terminals)")
    args = ap.parse_args()

    existing = config.TOTP_KEY_FILE.exists()
    if args.show:
        if not existing:
            print("No TOTP key yet. Run this without --show to create one.")
            return 1
        if not sys.stdout.isatty():
            print("Refusing to show the secret outside an interactive terminal.")
            print("Open a terminal window on this machine and run the command there.")
            return 2
        _print_enrolment(config.TOTP_KEY_FILE.read_text().strip(), args.no_invert)
        print("Same key as before: devices already set up keep working.")
        return 0
    if args.check:
        if not existing:
            print("No TOTP key yet. Run this without --check first.")
            return 1
        ok = pyotp.TOTP(config.TOTP_KEY_FILE.read_text().strip()).verify(args.check.strip(), valid_window=1)
        print("Code matches." if ok else "Code does NOT match. Is the phone's clock correct?")
        return 0 if ok else 1

    if existing and not args.force:
        print(f"A TOTP key already exists ({config.TOTP_KEY_FILE}).")
        print("Use --force to replace it; every unlocked device will need a new code.")
        return 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Refusing to show the secret outside an interactive terminal.")
        print("Open a terminal window on this machine and run the command there.")
        return 2

    secret = pyotp.random_base32()
    _print_enrolment(secret, args.no_invert)

    for _ in range(3):
        code = input("Type the 6-digit code the app shows now to confirm: ").strip()
        if pyotp.TOTP(secret).verify(code, valid_window=1):
            _save(secret)
            print(f"\nSaved to {config.TOTP_KEY_FILE}. Open the dashboard's Terminal tab and enter a code.")
            if existing:
                print("The old key is gone; every device has been signed out of the terminal.")
            return 0
        print("That code doesn't match. Try the next one.")
    print("Nothing saved.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
