"""
Password policy following NIST SP 800-63B (revision 4) and the OWASP guidance:

* length is what counts: at least 15 characters (the NIST minimum when a password is the only
  factor) and up to 128, any character allowed (spaces and Unicode included, so passphrases work);
* no forced mixes of upper/lower/digits/symbols and no periodic changes;
* refuse what attackers try first: common passwords, the account name, the application's name,
  keyboard walks, runs and repetitions;
* refuse passwords known from data breaches, checked with the Have I Been Pwned range API. Only
  the first five characters of the password's SHA-1 leave the server (k-anonymity), the password
  never does. If the service cannot be reached the check is skipped. Set VN_PWNED_CHECK=0 to turn
  it off for servers without internet access.
"""

import hashlib
import os
import re
import unicodedata

import requests

MIN_LENGTH = 15
MAX_LENGTH = 128

# Problem codes, so the caller can translate them
TOO_SHORT = "short"
TOO_LONG = "long"
COMMON = "common"
CONTAINS_USERNAME = "username"
CONTAINS_APP_NAME = "appname"
PATTERN = "pattern"
BREACHED = "breached"

_APP_WORDS = ("vinted", "notification")

# The most used passwords and the words they are built from (lower case, letters and digits only)
_COMMON = set(
    """
    password passw0rd p4ssword passwort motdepasse contrasena contraseña senha parola password1 password12 password123
    password1234 admin administrator administrator1 root toor letmein welcome welcome1 welcome123 login master
    qwerty qwerty1 qwerty12 qwerty123 qwertyuiop qwertyuiop123 azerty azerty123 asdfgh asdfghjkl asdfghjkl123 zxcvbnm
    zxcvbnm123 1q2w3e4r 1q2w3e4r5t 1qaz2wsx 1qaz2wsx3edc qazwsx qazwsxedc q1w2e3r4 q1w2e3r4t5 iloveyou iloveyou1
    monkey dragon football baseball soccer hockey batman superman spiderman pokemon starwars sunshine princess
    shadow master123 michael jennifer jessica ashley charlie daniel thomas hunter ranger trustno1 abc123 abcd1234
    abcdef abcdefgh abcdefg123 aa123456 a1b2c3d4 changeme changeme123 default guest secret secret123 test test123
    testing testing123 temp temp123 demo demo123 user user123 pass pass123 pass1234 passphrase mypassword
    mypassword1 mypassword123 ciaociao ciao ciao123 buongiorno juventus napoli inter milan roma lazio forza
    forzajuve forzanapoli amore amore123 tiamo tiamo123 italia italia1 italia123 vinted vinted123 vintednotifications
    vintednotification vintednotifications1 vintednotifications123 docker docker123 raspberry linux ubuntu oracle
    oracle123 server server123 system system123 computer computer1 internet internet1 football1 baseball1 dragon123
    """.split()
)

_KEYBOARD_ROWS = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm", "qazwsxedcrfvtgbyhnujmikolp")


def _normalise(text):
    return unicodedata.normalize("NFKC", text)


def _letters_digits(text):
    return re.sub(r"[^a-z0-9]", "", _normalise(text).lower())


def _is_walk(text):
    """A run of characters that is only a keyboard row or consecutive letters/digits."""
    compact = _letters_digits(text)
    if len(compact) < 8:
        return False
    for source in _KEYBOARD_ROWS + ("abcdefghijklmnopqrstuvwxyz", "0123456789"):
        for variant in (source + source, source[::-1] * 2):  # runs may wrap around ("...7890123")
            # every window of the password appears in the source row (possibly repeated)
            if all(compact[i : i + 3] in variant for i in range(len(compact) - 2)):
                return True
    return False


def _is_repetitive(text):
    compact = _normalise(text).lower()
    if len(set(compact)) <= 3:
        return True
    # "abcabcabcabcabcabc" and the like
    for size in range(1, len(compact) // 2 + 1):
        if len(compact) % size == 0 and compact[:size] * (len(compact) // size) == compact:
            return True
    return False


def _is_common(text):
    base = _letters_digits(text)
    if base in _COMMON:
        return True
    stripped = base.rstrip("0123456789")  # "password2026", "ciao12345"
    return len(stripped) >= 4 and stripped in _COMMON


def breach_count(password, timeout=3):
    """How many times the password appears in known breaches, or None if it can't be checked."""
    if os.environ.get("VN_PWNED_CHECK", "1") in ("0", "false", ""):
        return None
    # SHA-1 is what the range API is keyed on; it is not used to protect anything here
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()  # nosec B324
    prefix, suffix = digest[:5], digest[5:]
    try:
        response = requests.get(
            f"https://api.pwnedpasswords.com/range/{prefix}",
            headers={"Add-Padding": "true", "User-Agent": "vinted-notifications"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        for line in response.text.splitlines():
            found, _, count = line.partition(":")
            if found.strip() == suffix:
                return int(count.strip() or 0)
        return 0
    except (requests.RequestException, ValueError):
        return None


def check(password, username="", check_breaches=True):
    """
    Returns None when the password is acceptable, otherwise a problem code:
    TOO_SHORT, TOO_LONG, COMMON, CONTAINS_USERNAME, CONTAINS_APP_NAME, PATTERN or BREACHED.
    """
    password = password or ""
    length = len(_normalise(password))
    if length < MIN_LENGTH:
        return TOO_SHORT
    if length > MAX_LENGTH:
        return TOO_LONG
    folded = _letters_digits(password)
    name = _letters_digits(username)
    if len(name) >= 3 and name in folded:
        return CONTAINS_USERNAME
    if any(word in folded for word in _APP_WORDS):
        return CONTAINS_APP_NAME
    if _is_common(password):
        return COMMON
    if _is_walk(password) or _is_repetitive(password):
        return PATTERN
    if check_breaches and breach_count(password):
        return BREACHED
    return None
