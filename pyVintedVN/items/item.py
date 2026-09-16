import time
from datetime import datetime, timezone


class Item:
    """
    Represents a single item from Vinted.

    This class parses and stores various attributes of a Vinted item,
    such as id, title, brand, size, price, etc.

    Note on timestamps: the svc-catalogue API (September 2026) no longer exposes
    when a listing was created -- the field is gone from the API and from the item
    page alike. `created_at_ts` therefore falls back to the moment we first saw the
    item. With a 5 minute poll interval that is within 5 minutes of the real
    listing time for genuinely new items, but it is an observation time, not a
    listing time, and `has_real_timestamp` says which one you are holding.

    Attributes:
        raw_data (dict): The raw data of the item as received from the API.
        id (str): The unique identifier of the item.
        title (str): The title of the item.
        brand_title (str): The brand of the item.
        size_title (str): The size of the item, or None if not available.
        currency (str): The currency code of the item's price.
        price (float): The price of the item.
        photo (str): The URL of the item's photo.
        url (str): The absolute URL of the item on Vinted.
        created_at_ts (datetime): When the item was created, or when it was first seen.
        raw_timestamp (int): The raw timestamp value behind created_at_ts.
        has_real_timestamp (bool): True when the API actually gave us a listing time.
    """

    def __init__(self, data, locale=None):
        """
        Initialize an Item with data from the Vinted API.

        Args:
            data (dict): The item data from the Vinted API.
            locale (str, optional): The Vinted host the item came from, used to
                turn the relative item URL into an absolute one.
        """
        self.raw_data = data
        self.id = data["id"]
        self.title = data["title"]

        # The catalogue API moved brand and size into the `item_box` display block,
        # where both fields are positional rather than named:
        #   branded item -> first_line = brand,  second_line = "size · condition"
        #   unbranded    -> first_line = title,  second_line = "condition"
        # so a first_line echoing the title means the listing simply has no brand.
        item_box = data.get("item_box") or {}
        brand = item_box.get("first_line")
        self.brand_title = data.get("brand_title") or (
            brand if brand and brand != self.title else None
        )

        size = data.get("size_title")
        if not size:
            second_line = item_box.get("second_line") or ""
            size = second_line.split(" · ")[0] if " · " in second_line else None
        self.size_title = size

        self.currency = data["price"]["currency_code"]
        self.price = data["price"]["amount"]
        self.photo = (data.get("photo") or {}).get("url")

        # Item URLs are now relative (/items/123-slug), so rebuild the absolute one.
        url = data["url"]
        if url.startswith("/") and locale:
            url = f"https://{locale}{url}"
        self.url = url

        # We keep everything before the "items"
        self.buy_url = (
            self.url.split("items")[0]
            + "transaction/buy/new?source_screen=item&transaction%5Bitem_id%5D="
            + str(data["id"])
        )

        # Prefer a real listing time when the API gives one, else record when we saw it.
        real_timestamp = ((data.get("photo") or {}).get("high_resolution") or {}).get(
            "timestamp"
        )
        self.has_real_timestamp = real_timestamp is not None
        self.raw_timestamp = (
            real_timestamp if self.has_real_timestamp else int(time.time())
        )
        self.created_at_ts = datetime.fromtimestamp(self.raw_timestamp, tz=timezone.utc)

    def __eq__(self, other):
        """
        Compare this item with another one.

        Two items are considered the same if they have the same ID.

        Args:
            other (Item): The other item to compare with.

        Returns:
            bool: True if the items have the same ID, False otherwise.
        """
        if not isinstance(other, Item):
            return False
        return self.id == other.id

    def __hash__(self):
        """
        Return a hash value for this item.

        The hash is based on the item's ID, which allows items to be used
        as keys in dictionaries and elements in sets.

        Returns:
            int: A hash value for the item.
        """
        return hash(("id", self.id))

    def is_new_item(self, minutes=20):
        """
        Check if this item is newly listed.

        When the API supplies a real listing time, an item counts as new if it was
        created within the specified number of minutes. When it does not -- which is
        the case for the current catalogue API -- listing age is simply unknowable,
        so this returns True and the caller is expected to fall back to
        "have I already seen this id?" deduplication.

        Args:
            minutes (int, optional): The number of minutes to consider an item as new.
                Defaults to 20.

        Returns:
            bool: True if the item is new (or its age cannot be determined).
        """
        if not self.has_real_timestamp:
            return True
        delta = datetime.now(timezone.utc) - self.created_at_ts
        return delta.total_seconds() < minutes * 60

    # Alias for backward compatibility
    isNewItem = is_new_item
