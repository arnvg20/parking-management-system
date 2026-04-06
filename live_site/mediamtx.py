from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from starlette.datastructures import Headers


HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def build_upstream_url(base_url: str, proxy_path: str) -> str:
    return f"{base_url.rstrip('/')}/{proxy_path.lstrip('/')}"


def build_forward_headers(headers: Headers) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for name, value in headers.items():
        lower_name = name.lower()
        if lower_name in HOP_BY_HOP_HEADERS or lower_name == "host":
            continue
        forwarded[name] = value
    return forwarded


def filter_response_headers(headers: Headers) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in HOP_BY_HOP_HEADERS:
            continue
        filtered[name] = value
    return filtered


def rewrite_location_header(location: str, proxy_prefix: str) -> str:
    parsed = urlsplit(location)
    upstream_path = parsed.path.lstrip("/")
    rewritten_path = f"{proxy_prefix.rstrip('/')}/{upstream_path}"
    return urlunsplit(("", "", rewritten_path, parsed.query, parsed.fragment))
