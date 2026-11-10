"""Create (or reset) the login and print the authenticator QR code.

    docker compose exec -it web python -m compass.auth.setup --username aryaman

Resetting replaces the password and TOTP secret and signs out every existing session.
"""
import argparse
import getpass
import sys

import qrcode

from compass.auth.core import create_user, totp_uri, verify_totp
from compass.db import connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    password = getpass.getpass("New password (12+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords differ.", file=sys.stderr)
        return 1
    conn = connect()
    try:
        secret = create_user(conn, args.username, password)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    uri = totp_uri(secret, args.username)
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.print_ascii(invert=True)
    print(f"\nOr enter this secret by hand: {secret}\n")
    for _ in range(3):
        if verify_totp(secret, input("Code from the authenticator app: ")):
            print("Login ready.")
            return 0
        print("That code did not match; try the next one.")
    print("Codes never matched. Check the phone's clock and run setup again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
