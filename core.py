import db
import push
import re
import time
from urllib.parse import urlparse as _urlparse
import requests
from concurrent.futures import ThreadPoolExecutor
from pyVintedVN import Vinted, requester
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

# Vinted stopped publishing listing times in September 2026, so "is this item new?"
# is answered purely by "have we recorded its id before?". That makes a wiped or
# freshly seeded database the only real flood risk, and this caps the blast radius.
MAX_NOTIFICATIONS_PER_RUN = 10


# Any Vinted site (vinted.fr, www.vinted.co.uk, vinted.com, ...). The scraper requests whatever
# address is stored, so this also keeps accounts from pointing it at other hosts.
_VINTED_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*vinted\.(?:com|co\.uk|[a-z]{2})$", re.I)


def is_vinted_url(url):
    """Is this an http(s) address on a Vinted site?"""
    try:
        parsed = _urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(
        parsed.hostname and _VINTED_HOST.match(parsed.hostname)
    )


def process_query(
    query, name=None, min_stars=None, min_reviews=None, max_price=None, user_id=0, push=False
):
    """
    Process a Vinted query URL by:
    1. Checking if the URL is a brand URL and converting it to standard format if needed
    2. Parsing the URL and extracting query parameters
    3. Ensuring the order flag is set to "newest_first"
    4. Removing time and search_id parameters
    5. Rebuilding the query string and URL
    6. Checking if the query already exists in the database
    7. Adding the query to the database if it doesn't exist

    Args:
        query (str): The Vinted query URL
        name (str, optional): A name for the query. If provided, it will be used as the query name.
        min_stars (float, optional): Minimum seller rating (0-5 stars) required for items to be returned.
        min_reviews (int, optional): Minimum number of seller reviews required for items to be returned.
        max_price (float, optional): Maximum price; more expensive items are skipped.
        user_id (int): The account the query belongs to (0 before accounts exist).
        push (bool): Also send Web Push notifications for this query.

    Returns:
        tuple: (message, is_new_query)
            - message (str): Status message
            - is_new_query (bool): True if query was added, False if it already existed
    """
    query = (query or "").strip()
    if not is_vinted_url(query):
        return "Not a Vinted search URL.", False

    # Check if the URL is a brand URL (format: url/brand/id-name)
    parsed_url = urlparse(query)
    path_parts = parsed_url.path.strip("/").split("/")

    if len(path_parts) >= 2 and path_parts[0] == "brand":
        # Extract the brand ID from the format "id-name"
        brand_id_with_name = path_parts[1]
        brand_id = brand_id_with_name.split("-")[0]

        # Create a new URL with the standard format
        new_path = "/catalog"
        new_query_params = {"brand_ids[]": [brand_id]}
        new_query_string = urlencode(new_query_params, doseq=True)

        # Rebuild the URL
        query = urlunparse(
            (parsed_url.scheme, parsed_url.netloc, new_path, "", new_query_string, "")
        )
        logger.info(f"Converted brand URL to standard format: {query}")

        # Parse the URL and extract the query parameters
        parsed_url = urlparse(query)

    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Some queries are made with filters only, so we need to check if the search_text is present
    if db.is_query_in_db(processed_query, user_id) is True:
        return "Query already exists.", False
    else:
        # add the query to the db
        db.add_query_to_db(
            processed_query, name, min_stars, min_reviews, max_price, user_id, push
        )
        return "Query added.", True


def get_formatted_query_list(user_id=0):
    """
    Get a formatted list of all queries in the database.

    Returns:
        str: A formatted string with all queries, numbered
    """
    all_queries = db.get_queries(user_id)
    queries_keywords = []
    for query in all_queries:
        parsed_url = urlparse(query[1])
        query_params = parse_qs(parsed_url.query)

        # Get the name or Extract the value of 'search_text'
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )

        if query_name is None:
            # Use the query URL when there is neither a name nor a search text
            queries_keywords.append(query[1])
        else:
            queries_keywords.append(query_name)

    query_list = ("\n").join(
        [str(i + 1) + ". " + j for i, j in enumerate(queries_keywords)]
    )
    return query_list


def process_remove_query(number, user_id=0):
    """
    Process the removal of a query from the database.

    Args:
        number (str): The number of the query to remove or "all" to remove all queries

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was removed successfully
    """
    if number == "all":
        db.remove_all_queries_from_db(user_id)
        return "All queries removed.", True

    # Check if number is a valid digit
    if number.isdigit():
        # Remove the query from the database
        if db.remove_query_from_db(number, user_id):
            return "Query removed.", True
        return "Query not found.", False
    else:
        return "Invalid number.", False


def process_update_query(
    query_id, query, name, min_stars=None, min_reviews=None, max_price=None, user_id=0, push=None
):
    """
    Process the update of a query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new Vinted query URL
        name (str, optional): A new name for the query. If provided, it will be used as the query name.

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was updated successfully
    """
    query = (query or "").strip()
    if not is_vinted_url(query):
        return "Not a Vinted search URL.", False

    # Parse the URL and extract the query parameters
    parsed_url = urlparse(query)
    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Update the query in the database
    if db.update_query_in_db(
        query_id, processed_query, name, min_stars, min_reviews, max_price, user_id, push
    ):
        return "Query updated.", True
    else:
        return "Failed to update query.", False


def process_add_country(country, user_id=0):
    """
    Process the addition of a country to the allowlist.

    Args:
        country (str): The country code to add

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")
    country_list = db.get_allowlist(user_id)

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", country_list

    # Check if the country is already in the allowlist
    # If country_list is 0, it means the allowlist is empty
    if country_list != 0 and country.upper() in country_list:
        return f'Country "{country.upper()}" already in allowlist.', country_list

    # Add the country to the allowlist
    db.add_to_allowlist(country.upper(), user_id)
    return "Country added.", db.get_allowlist(user_id)


def process_remove_country(country, user_id=0):
    """
    Process the removal of a country from the allowlist.

    Args:
        country (str): The country code to remove

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", db.get_allowlist(user_id)

    # Remove the country from the allowlist
    db.remove_from_allowlist(country.upper(), user_id)
    return "Country removed.", db.get_allowlist(user_id)


_user_country_cache = {}
COUNTRY_CACHE_TTL = 24 * 3600


def get_user_country(profile_id):
    """
    Get the country code for a Vinted user.

    Makes an API request to retrieve the user's country code.
    Handles rate limiting by trying an alternative endpoint.

    Args:
        profile_id (str): The Vinted user's profile ID

    Returns:
        str: The user's country code (2-letter ISO code) or "XX" if it can't be determined
    """
    cached = _user_country_cache.get(profile_id)
    if cached and time.time() - cached[0] < COUNTRY_CACHE_TTL:
        return cached[1]
    # Users are shared between all Vinted platforms, so we can use whatever locale we want
    url = f"https://www.vinted.fr/api/v2/users/{profile_id}?localize=false"
    response = requester.get(url)
    # That's a LOT of requests, so if we get a 429 we wait a bit before retrying once
    if response.status_code == 429:
        # In case of rate limit, we're switching the endpoint. This one is slower, but it doesn't RL as soon.
        # We're limiting the items per page to 1 to grab as little data as possible
        url = f"https://www.vinted.fr/api/v2/users/{profile_id}/items?page=1&per_page=1"
        response = requester.get(url)
        try:
            user_country = response.json()["items"][0]["user"]["country_iso_code"]
        except KeyError:
            logger.warning(
                "Couldn't get the country due to too many requests. Returning default value."
            )
            user_country = "XX"
    else:
        user_country = response.json()["user"]["country_iso_code"]
    if user_country != "XX":
        _user_country_cache[profile_id] = (time.time(), user_country)
    return user_country


_user_feedback_cache = {}
FEEDBACK_CACHE_TTL = 3600  # seconds
FEEDBACK_RETRY_TTL = 120  # seconds before retrying a seller whose feedback failed to load


def _remember_feedback(profile_id, result):
    _user_feedback_cache[profile_id] = (time.time(), result)
    return result


def get_user_feedback(profile_id):
    """
    Get the seller's feedback as (stars, review_count).

    Stars (0-5) are derived from Vinted's feedback_reputation (0-1).

    Returns:
        tuple: (stars, count), or (None, None) if it can't be determined (e.g. rate limited).
    """
    if not profile_id:
        return None, None
    cached = _user_feedback_cache.get(profile_id)
    if cached:
        ttl = FEEDBACK_CACHE_TTL if cached[1][0] is not None else FEEDBACK_RETRY_TTL
        if time.time() - cached[0] < ttl:
            return cached[1]
    url = f"https://www.vinted.fr/api/v2/users/{profile_id}?localize=false"
    try:
        response = requester.get(url)
        if response.status_code != 200:
            return _remember_feedback(profile_id, (None, None))
        user = response.json()["user"]
        reputation = user.get("feedback_reputation")
        count = user.get("feedback_count")
        if reputation is None or count is None:
            return _remember_feedback(profile_id, (None, None))
        result = (round(float(reputation) * 5, 2), int(count))
    except Exception as e:
        logger.warning(f"Couldn't get feedback for user {profile_id}: {e}")
        return _remember_feedback(profile_id, (None, None))
    return _remember_feedback(profile_id, result)


def _scrape(vinted, by_url, queue, items_per_query):
    """Scrape each address once and hand the unseen items to every query that uses it."""
    for url, query_ids in by_url.items():
        try:
            all_items = vinted.items.search(url, nbr_items=items_per_query)
        except Exception as e:
            logger.error(f"Scraping {url} failed: {e}")
            continue
        # Filter to only include new items. This should reduce the amount of db calls.
        data = [item for item in all_items if item.is_new_item()]
        for query_id in query_ids:
            queue.put((data, query_id))
        logger.info(f"Scraped {len(data)} items for query: {url} ({len(query_ids)} subscriber(s))")


def process_items(queue):
    """
    Scrape every query in the database and put the unseen items in the queue.

    Args:
        queue (Queue): The queue the extractor reads the scraped items from.
    """
    all_queries = db.get_queries()
    # Several accounts often watch the same search: scrape each distinct URL once
    by_url = {}
    for query in all_queries:
        by_url.setdefault(query[1], []).append(query[0])

    items_per_query = int(db.get_parameter("items_per_query"))
    _scrape(Vinted(), by_url, queue, items_per_query)
    # Heartbeat for the status indicator in the interface
    db.set_parameter("last_scrape_at", str(int(time.time())))


def process_pending_scans(queue):
    """
    Scrape queries that were just added or edited, without waiting for the next cycle.
    Runs every few seconds and does nothing when nobody changed a query.
    """
    pending = db.pop_pending_scans()
    if not pending:
        return
    by_url = {}
    for query_id, url in pending:
        by_url.setdefault(url, []).append(query_id)
    logger.info(f"Immediate scan for {len(pending)} new or edited query(ies)")
    _scrape(Vinted(), by_url, queue, int(db.get_parameter("items_per_query")))


def _prefetch_feedback(items):
    """Look up the sellers of unseen items in parallel; the results land in the cache."""
    sellers = {
        (item.raw_data.get("user") or {}).get("id")
        for item in items
    }
    sellers.discard(None)
    sellers = [s for s in sellers if s not in _user_feedback_cache]
    if len(sellers) > 1:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(get_user_feedback, sellers))


def clear_item_queue(items_queue, new_items_queue):
    """
    Process items from the items_queue.
    This function is scheduled to run frequently.
    """
    if not items_queue.empty():
        data, query_id = items_queue.get()
        owner = db.get_query_owner(query_id)
        if owner is None:  # the query was removed while its items were in flight
            return
        banwords_str = db.get_user_setting(owner, "banwords")
        allowlist = db.get_allowlist(owner)

        # Read the watermark once, before the loop. It doubles as the "has this query
        # ever produced anything?" flag, and the updates made below would otherwise
        # cut a first-run priming pass short right after the first item.
        last_query_timestamp = db.get_last_timestamp(query_id)
        is_first_run = last_query_timestamp is None
        if is_first_run:
            logger.info(
                f"First run for query {query_id}: recording {len(data)} item(s) "
                f"without notifying, so the existing catalogue is not replayed."
            )

        # The seller lookups are what makes a first scan slow: do them side by side
        _prefetch_feedback([item for item in data if not db.is_item_in_db_by_id(item.id, owner)])

        to_notify = []
        for item in reversed(data):

            # The watermark is only meaningful when the API actually supplied a
            # listing time. Otherwise raw_timestamp is merely when we saw the item,
            # and comparing it against the watermark would discard every new item.
            if (
                item.has_real_timestamp
                and last_query_timestamp is not None
                and last_query_timestamp >= item.raw_timestamp
            ):
                continue
            # Over budget: skipped before any network call
            min_stars, min_reviews, max_price = db.get_query_filters(query_id)
            if max_price and float(item.price) > max_price:
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue
            # In case of multiple queries, we need to check if the item is already in the db
            if db.is_item_in_db_by_id(item.id, owner) is True:
                # We update the timestamp
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue
            # If there's an allowlist and
            # If the user's country is not in the allowlist, we just update the timestamp
            if allowlist != 0 and (
                get_user_country(item.raw_data["user"]["id"])
            ) not in (allowlist + ["XX"]):
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue
            # Check if the item title contains any banwords
            if banwords_str and contains_banwords(item.title, banwords_str):
                # If it contains banwords, just update the timestamp and skip
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue

            # Seller feedback is stored with the item (shown in the UI) and, when the query
            # defines minimum stars/reviews, used to skip brand new or fake accounts.
            # If the feedback can't be determined the item is let through, like the
            # country check.
            seller = item.raw_data.get("user") or {}
            stars, reviews = get_user_feedback(seller.get("id"))
            if stars is not None and (
                stars < (min_stars or 0) or reviews < (min_reviews or 0)
            ):
                logger.debug(
                    f"Item {item.id} skipped: seller has {stars} stars / {reviews} "
                    f"reviews (required {min_stars or 0} / {min_reviews or 0})"
                )
                db.update_last_timestamp(query_id, item.raw_timestamp)
                continue

            # Being recorded is what stops an item coming back next run, so every
            # item that reaches this point is written to the db whether or not it
            # ends up being announced.
            to_notify.append(item)
            db.add_item_to_db(
                id=item.id,
                timestamp=item.raw_timestamp,
                price=item.price,
                title=item.title,
                photo_url=item.photo,
                query_id=query_id,
                currency=item.currency,
                seller_id=seller.get("id"),
                seller_login=seller.get("login"),
                seller_stars=stars,
                seller_reviews=reviews,
                user_id=owner,
            )

        if is_first_run:
            return

        # data arrives newest first and is walked in reverse, so to_notify runs
        # oldest to newest. Trim from the front: a backlog is worth dropping, the
        # listings that just appeared are not.
        if len(to_notify) > MAX_NOTIFICATIONS_PER_RUN:
            logger.warning(
                f"{len(to_notify)} unseen items for query {query_id}; notifying the "
                f"{MAX_NOTIFICATIONS_PER_RUN} most recent and recording the rest silently."
            )
            to_notify = to_notify[-MAX_NOTIFICATIONS_PER_RUN:]

        for item in to_notify:
            # We create the message
            content = render_message(
                db.get_user_setting(owner, "message_template") or "",
                title=item.title,
                price=str(item.price) + " " + item.currency,
                brand=item.brand_title,
                image=item.photo,
            )
            # add the item to the queue
            # (account, message, link, link text, buy link, buy text)
            push_payload = None
            if db.get_query_push(query_id):
                push_payload = push.item_payload(
                    item.title, f"{item.price} {item.currency}", item.url, item.photo
                )
            new_items_queue.put((owner, content, item.url, "Open Vinted", None, None, push_payload))


_PLACEHOLDER = re.compile(r"\{(title|price|brand|image)\}")


def render_message(template, **values):
    """
    Fill {title}, {price}, {brand} and {image} in a user-written template.

    Only these four placeholders are substituted: str.format() would let a template read
    attributes of the values, which isn't something an account should be able to do.
    """
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1)) or ""), template)


def contains_banwords(title, banwords_str):
    """
    Check if a title contains any banwords.

    Args:
        title (str): The title to check
        banwords_str (str): List of banwords separated by 3 pipe character
    Returns:
        bool: True if the title contains any banwords, False otherwise
    """

    # Split the banwords string into a list using pipe as delimiter
    banwords = [
        word.strip().lower() for word in banwords_str.split("|||") if word.strip()
    ]

    # If the list is empty, return False
    if not banwords:
        return False

    # Check if any banword is in the title (case-insensitive)
    title_lower = title.lower()
    for word in banwords:
        if word in title_lower:
            return True

    return False


def check_version():
    """
    Check if the application is up to date
    """
    try:
        # Get URL from the database
        github_url = db.get_parameter("github_url")
        # Get version from the database
        ver = db.get_parameter("version")
        # Get latest version from the repository
        url = f"{github_url}/releases/latest"
        response = requests.get(url, timeout=5)

        if response.status_code == 200:
            latest_version = response.url.split("/")[-1]
            is_up_to_date = ver == latest_version
            return is_up_to_date, ver, latest_version, github_url
        else:
            # If we can't check, assume it's up to date
            return True, ver, ver, github_url
    except Exception as e:
        logger.error(f"Error checking for new version: {str(e)}", exc_info=True)
        # If we can't check, assume it's up to date
        return True, ver, ver, github_url
