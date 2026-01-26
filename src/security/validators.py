from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from src.config import ALLOWED_DOMAINS, MAX_URL_LENGTH
from src.errors import InvalidUrlError, UnsupportedPlatformError


_PLATFORM_MAP = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "vt.tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "instagram.com": "instagram",
    "pinterest.com": "pinterest",
    "pin.it": "pinterest",
}


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _host_matches_allowed(host: str) -> str | None:
    host = host.rstrip(".").lower()
    for base in ALLOWED_DOMAINS:
        base = base.lower()
        if host == base:
            return base
        if host.endswith("." + base):
            return base
    return None


def _reject_internal_host(host: str) -> None:
    h = host.rstrip(".").lower()

    if h == "localhost" or h.endswith(".localhost"):
        raise InvalidUrlError(
            "localhost blocked",
            user_message="🚫 URL ខាងក្នុង (localhost) មិនអនុញ្ញាតទេ។",
        )

    if _is_ip_literal(h):
        ip = ipaddress.ip_address(h)
        if _is_private_ip(ip):
            raise InvalidUrlError(
                "private ip blocked",
                user_message="🚫 URL ខាងក្នុង (IP private) មិនអនុញ្ញាតទេ។",
            )
        raise InvalidUrlError(
            "ip literal blocked",
            user_message="🚫 URL ជា IP មិនអនុញ្ញាតទេ។ សូមប្រើ link ដើម (domain)។",
        )


def _dns_resolves_to_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False

    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            ip_str = sockaddr[0]
        elif family == socket.AF_INET6:
            ip_str = sockaddr[0]
        else:
            continue
        try:
            ip = ipaddress.ip_address(ip_str)
            if _is_private_ip(ip):
                return True
        except ValueError:
            continue
    return False


def validate_and_normalize_url(raw_url: str) -> tuple[str, str]:
    url = (raw_url or "").strip()

    if not url:
        raise InvalidUrlError("empty url", user_message="⚠️ សូមបញ្ចូល link មុន។")

    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrlError(
            "url too long",
            user_message=f"⚠️ Link វែងពេក (អតិបរមា {MAX_URL_LENGTH} តួអក្សរ)។",
        )

    try:
        parts = urlsplit(url)
    except Exception:
        raise InvalidUrlError("parse failed", user_message="⚠️ ទម្រង់ URL មិនត្រឹមត្រូវ។")

    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise InvalidUrlError(
            "invalid scheme",
            user_message="⚠️ អនុញ្ញាតតែ HTTP/HTTPS ប៉ុណ្ណោះ។",
        )

    if parts.username or parts.password:
        raise InvalidUrlError(
            "userinfo in url",
            user_message="⚠️ Link មាន username/password ដែលមិនអនុញ្ញាតទេ។",
        )

    host = (parts.hostname or "").strip()
    if not host:
        raise InvalidUrlError("missing host", user_message="⚠️ Link មិនមាន domain។")

    _reject_internal_host(host)

    if _dns_resolves_to_private(host):
        raise InvalidUrlError(
            "dns resolves to private",
            user_message="🚫 Link នេះត្រូវបានបដិសេធ (DNS ទៅ IP ខាងក្នុង)។",
        )

    matched_base = _host_matches_allowed(host)
    if not matched_base:
        raise UnsupportedPlatformError(
            "unsupported platform",
            user_message=(
                "វេទិកានេះមិនត្រូវបានគាំទ្រទេ។\n\n"
                "វេទិកាដែលគាំទ្រ:\n"
                "• TikTok\n"
                "• Facebook\n"
                "• YouTube\n"
                "• Instagram\n"
                "• Pinterest"
            ),
        )

    platform = _PLATFORM_MAP.get(matched_base, "other")

    normalized = urlunsplit(
        (
            scheme,
            parts.netloc,
            parts.path or "",
            parts.query or "",
            "",
        )
    )

    return normalized, platform
