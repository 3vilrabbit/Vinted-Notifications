"""
RSS feeds, one per account.

Each account has a private feed at /feed/<token> (the token is created on first use and shown
in Configuration > Notifications). The feed is built from the items stored for that account, so
it survives restarts. Before any account exists, the original public feed at "/" still works.
"""

import datetime
import html
from urllib.parse import urlparse

from feedgen.feed import FeedGenerator
from flask import Flask, Response, abort

import db
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)


def _item_url(query_url, item_id):
    parsed = urlparse(query_url)
    return f"{parsed.scheme}://{parsed.netloc}/items/{item_id}"


def build_feed(user_id):
    """RSS document for an account, or None if its feed is switched off."""
    if db.get_user_setting(user_id, "rss_enabled") != "True":
        return None
    try:
        limit = max(1, int(db.get_parameter("rss_max_items") or 100))
    except ValueError:
        limit = 100
    rows, _ = db.search_items(user_id, limit=limit)

    feed = FeedGenerator()
    feed.title("Vinted Notifications")
    feed.description("Latest items from Vinted matching your search queries")
    feed.link(href="https://www.vinted.com")
    feed.language("en")
    for row in rows:
        item_id, title, price, currency, timestamp, query_url, photo, query_name = row[:8]
        seller, stars, reviews = row[8], row[9], row[10]
        url = _item_url(query_url, item_id)
        parts = [f"<p><b>{html.escape(str(price))} {html.escape(currency or '')}</b>"]
        if seller:
            rating = f" ({stars:.1f}★, {reviews})" if stars is not None else ""
            parts.append(f" · {html.escape(seller)}{rating}")
        parts.append("</p>")
        if photo:
            parts.append(f'<p><img src="{html.escape(photo)}" alt=""></p>')
        entry = feed.add_entry()
        entry.id(url)
        entry.title(f"{title} - {price} {currency}")
        entry.link(href=url)
        entry.description("".join(parts))
        entry.published(datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc))
    return feed.rss_str()


def create_app():
    app = Flask(__name__)

    @app.after_request
    def harden(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.route("/feed/<token>")
    def user_feed(token):
        user_id = db.get_user_id_by_feed_token(token)
        body = build_feed(user_id) if user_id is not None else None
        if body is None:
            abort(404)
        return Response(body, mimetype="application/rss+xml")

    @app.route("/")
    def legacy_feed():
        # The original public feed, only while no accounts exist
        if db.count_users() > 0:
            abort(404)
        body = build_feed(0)
        if body is None:
            abort(404)
        return Response(body, mimetype="application/rss+xml")

    return app


def rss_feed_process(queue=None):
    """Process function for the RSS feed server (the queue is no longer needed)."""
    logger.info("RSS feed process started")
    try:
        port = int(db.get_parameter("rss_port") or 8080)
        logger.info(f"Starting RSS feed server on port {port}")
        from waitress import serve

        # A production server with bounded threads/connections instead of Flask's development one
        serve(create_app(), host="0.0.0.0", port=port, threads=4, connection_limit=100, channel_timeout=30, max_request_body_size=1024)
    except (KeyboardInterrupt, SystemExit):
        logger.info("RSS feed process stopped")
    except Exception as e:
        logger.error(f"Error in RSS feed process: {e}", exc_info=True)
