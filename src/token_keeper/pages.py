"""HTTP responses for the connect and callback pages (spec §6.1, §6.2)."""

from __future__ import annotations

from typing import Any

Response = dict[str, Any]

HEADERS = {
    # The redirect to GitHub carries state; the one back carries code and state.
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

_ERROR_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connection failed</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem}
</style>
</head>
<body>
<h1>That didn't work</h1>
<p>This link is invalid, has expired or was already used. Go back to the app and start
the connection again.</p>
</body>
</html>
"""


def error_page(status: int = 400) -> Response:
    """The same plain page for every rejection; the reason only goes to the logs."""
    headers = {**HEADERS, "content-type": "text/html; charset=utf-8"}
    return {"statusCode": status, "headers": headers, "body": _ERROR_PAGE}


def redirect(location: str) -> Response:
    return {"statusCode": 302, "headers": {**HEADERS, "location": location}, "body": ""}


def not_found() -> Response:
    return {"statusCode": 404, "headers": dict(HEADERS), "body": ""}
