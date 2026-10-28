#!/usr/bin/env python3
"""
Recover an account whose password was forgotten, from the machine that runs the server.

Only someone who can already run commands inside the container (and so read the database) can use
it. There is no way to do this from the web interface and no standing "master password": the
account gets a one-time password, must replace it at the next sign-in, and is signed out
everywhere meanwhile.

Docker Compose:
    docker compose exec -u appuser vinted-notifications python reset_admin_password.py
    docker compose exec -u appuser vinted-notifications python reset_admin_password.py --user alice --generate

Without --generate you are asked for the temporary password (typing is hidden); with --generate a
random one is created and printed once.
"""

import argparse
import getpass
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import db  # noqa: E402  (needs the working directory above)
from werkzeug.security import generate_password_hash  # noqa: E402

from web_ui_plugin import passwords  # noqa: E402


def generated_password():
    """A random, memorable-enough one-time password: four groups of letters and digits."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (i, l, o, 0, 1)
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


def main():
    parser = argparse.ArgumentParser(description="Reset the password of an account (administrator by default).")
    parser.add_argument("--user", help="username to reset (default: the first administrator)")
    parser.add_argument("--generate", action="store_true", help="create a random one-time password instead of typing one")
    parser.add_argument("--enable", action="store_true", help="also re-enable the account if it was disabled")
    args = parser.parse_args()

    if not os.path.exists(db.DB_PATH):
        sys.exit(f"Database not found at {db.DB_PATH}. Run this inside the application's container or folder.")
    if db.count_users() == 0:
        sys.exit("Accounts are not turned on: the single web password is set with VN_WEB_PASSWORD or in Configuration.")

    if args.user:
        user = db.get_user_by_name(args.user)
        if user is None:
            sys.exit(f'No account named "{args.user}".')
    else:
        admins = [u for u in db.list_users() if u["role"] == "admin"]
        if not admins:
            sys.exit("There is no administrator account.")
        user = db.get_user(admins[0]["id"])

    if args.generate:
        password = generated_password()
    else:
        password = getpass.getpass(f"One-time password for {user['username']}: ")
        if password != getpass.getpass("Repeat it: "):
            sys.exit("The two passwords do not match.")
        # A typed password has to meet the same rules as any other (the breach lookup is skipped: no network needed here)
        problem = passwords.check(password, user["username"], check_breaches=False)
        if problem:
            sys.exit(f"Not accepted ({problem}): use at least {passwords.MIN_LENGTH} characters, nothing common or guessable.")

    fields = {"password_hash": generate_password_hash(password)}
    if args.enable:
        fields["disabled"] = 0
    db.update_user(user["id"], **fields)
    # The password is single-use: the next sign-in must replace it
    db.set_user_setting(user["id"], "must_change_password", "1")
    if db.get_user(user["id"])["password_hash"] != fields["password_hash"]:
        sys.exit("Could not write the database. In Docker run the command with: exec -u appuser")

    print(f'Password of "{user["username"]}" replaced; every session of that account was signed out.')
    if args.generate:
        print(f"One-time password: {password}")
    print("At the next sign-in the account is asked to choose a new password.")
    if user["disabled"] and not args.enable:
        print("Note: the account is disabled. Add --enable to re-enable it.")


if __name__ == "__main__":
    main()
