class Urls:
    # Vinted moved the catalogue off the legacy Rails API (www.vinted.<tld>/api/v2)
    # onto a dedicated service host (api.vinted.<tld>/svc-catalogue) in September 2026.
    # The old endpoint now answers 404 for every request.
    VINTED_API_URL = "/svc-catalogue"
    VINTED_PRODUCTS_ENDPOINT = "items"

    # The auth cookies are still handed out by the www host, so we keep both around.
    VINTED_AUTH_HOST_PREFIX = "www."
    VINTED_API_HOST_PREFIX = "api."
