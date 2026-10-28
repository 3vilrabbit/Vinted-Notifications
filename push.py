"""
Web Push: notifications on phones and computers that installed the web app.

Works on Android (Chrome, Edge, Firefox) and on iOS/iPadOS 16.4+ once the app has been added to
the Home Screen. The browser needs HTTPS (or localhost). Each device registers a subscription
(endpoint + keys); messages are encrypted for it and sent through the browser vendor's push service,
authenticated with this server's VAPID key pair, which is created on first use.
"""

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor

import db
from logger import get_logger

logger = get_logger(__name__)

DEFAULT_SUBJECT = "mailto:admin@example.com"
TTL_SECONDS = 6 * 3600  # a new listing is stale after a few hours


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _vapid():
    """The server's VAPID key pair (created and stored on first use)."""
    from py_vapid import Vapid

    pem = db.get_parameter("vapid_private_pem")
    if pem:
        return Vapid.from_pem(pem.encode())
    vapid = Vapid()
    vapid.generate_keys()
    db.set_parameter("vapid_private_pem", vapid.private_pem().decode())
    return vapid


def public_key():
    """The application server key the browser needs to subscribe (URL-safe base64)."""
    from cryptography.hazmat.primitives import serialization

    point = _vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(point)


def _subject():
    return os.environ.get("VN_VAPID_SUBJECT") or db.get_parameter("vapid_subject") or DEFAULT_SUBJECT


def valid_subscription(data):
    """Shape check for what a browser's PushSubscription.toJSON() sends."""
    try:
        endpoint = data["endpoint"]
        keys = data["keys"]
        return (
            isinstance(endpoint, str)
            and endpoint.startswith("https://")
            and len(endpoint) <= 1024
            and isinstance(keys["p256dh"], str)
            and isinstance(keys["auth"], str)
            and 0 < len(keys["p256dh"]) <= 256
            and 0 < len(keys["auth"]) <= 64
        )
    except (KeyError, TypeError):
        return False


def _send_one(sub, payload):
    """Deliver to one device. Returns True on success; dead subscriptions are removed."""
    from pywebpush import WebPushException, webpush

    try:
        response = webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=_vapid(),
            vapid_claims={"sub": _subject()},
            ttl=TTL_SECONDS,
            timeout=10,
        )
        host = sub["endpoint"].split("/")[2] if sub["endpoint"].count("/") >= 2 else "?"
        logger.info(f"Push accepted by {host} (HTTP {getattr(response, 'status_code', '?')})")
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):  # the user removed the app or revoked permission
            db.remove_push_subscription(sub["endpoint"])
            logger.info("Removed an expired push subscription")
        else:
            logger.warning(f"Push delivery failed ({status}): {e}")
    except Exception as e:
        logger.warning(f"Push delivery failed: {e}")
    return False


def send_to_user(user_id, payload):
    """Send `payload` (a JSON-able dict) to every device of the account; returns how many accepted it."""
    subs = db.get_push_subscriptions(user_id)
    if not subs:
        return 0
    with ThreadPoolExecutor(max_workers=min(4, len(subs))) as pool:
        return sum(1 for ok in pool.map(lambda s: _send_one(s, payload), subs) if ok)


def schedule_send(user_id, payload, delay):
    """Send after `delay` seconds without holding up the caller (used by the settings page's test button,
    so there is time to leave the app: iOS does not show a banner for the app that is on screen)."""
    import threading

    def run():
        try:
            sent = send_to_user(user_id, payload)
            logger.info(f"Delayed push for account {user_id}: {sent} device(s) accepted it")
        except Exception as e:
            logger.error(f"Delayed push failed: {e}", exc_info=True)

    timer = threading.Timer(max(0, delay), run)
    timer.daemon = True
    timer.start()


def item_payload(title, price, url, image=None, query_name=None):
    return {
        "title": (title or "")[:120],
        "body": (price or "") + (f" - {query_name}" if query_name else ""),
        "url": url,
        "image": image,
        "tag": url,  # the same listing never stacks twice
    }
