#!/usr/bin/env python3
"""
Jira HTTP Proxy
---------------
A reverse proxy that injects browser session cookies into Jira REST API
requests. Lets IDEs like PhpStorm use Jira's REST API without native
cookie-based auth support.

    PhpStorm  -->  localhost:9778  -->  jira.example.com
                   (inject cookies)
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_env_local():
    env_file = Path(os.environ["ENV_FILE"]) if "ENV_FILE" in os.environ else Path(__file__).parent / ".env.local"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if "$(" in value and "HOME" not in value:
            continue
        value = value.replace("$(HOME)", str(Path.home()))
        if key not in os.environ:
            os.environ[key] = value

_load_env_local()

JIRA_URL = os.environ.get("JIRA_URL", "https://jira.example.com")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "9778"))
COOKIE_FILE = Path(os.environ.get("COOKIE_FILE", Path.home() / ".jira-mcp-cookies.json"))
SSL_VERIFY = os.environ.get("PROXY_SSL_VERIFY", "true").lower() != "false"
SANITIZE_SUMMARIES = os.environ.get("PROXY_SANITIZE_SUMMARIES", "true").lower() != "false"
LOG_RESPONSES = os.environ.get("PROXY_LOG_RESPONSES", "false").lower() == "true"
WEB_PORT = int(os.environ.get("WEB_PORT", "9777"))
BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "").strip() or None
CIRCUIT_RECOVERY_SECONDS = float(os.environ.get("CIRCUIT_RECOVERY_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
)
log = logging.getLogger("jira-proxy")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def load_cookies() -> dict:
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text())
        except Exception:
            pass
    return {}


def get_cookie_header() -> str:
    return "; ".join(f"{k}={v}" for k, v in load_cookies().items() if not k.startswith("_"))

# ---------------------------------------------------------------------------
# Circuit breaker for cookie auth — trips on 401 so we stop re-probing
# expired cookies and route straight to BEARER_TOKEN until recovery timeout.
# ---------------------------------------------------------------------------

_cookie_breaker = {"open_until": 0.0}


def cookie_breaker_open() -> bool:
    return time.monotonic() < _cookie_breaker["open_until"]


def cookie_breaker_trip():
    _cookie_breaker["open_until"] = time.monotonic() + CIRCUIT_RECOVERY_SECONDS
    log.warning("Cookie auth circuit OPENED for %ds — routing via BEARER_TOKEN", int(CIRCUIT_RECOVERY_SECONDS))


def cookie_breaker_close():
    if _cookie_breaker["open_until"]:
        log.info("Cookie auth circuit CLOSED — cookies accepted again")
    _cookie_breaker["open_until"] = 0.0

# ---------------------------------------------------------------------------
# Summary sanitizer — strip chars invalid in git branch names
# ---------------------------------------------------------------------------

_NON_BRANCH_SAFE = re.compile(r'[^A-Za-z0-9 _-]+')


def sanitize_summary(text: str) -> str:
    """Strip characters that are invalid in git branch names."""
    text = _NON_BRANCH_SAFE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sanitize_response_body(body: bytes, content_type: str) -> bytes:
    """Sanitize summary fields in Jira JSON responses."""
    if not SANITIZE_SUMMARIES:
        return body
    if "application/json" not in content_type:
        return body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    modified = False

    # Single issue: /rest/api/2/issue/KEY
    if isinstance(data, dict) and "fields" in data and "summary" in data.get("fields", {}):
        data["fields"]["summary"] = sanitize_summary(data["fields"]["summary"])
        modified = True

    # Search results: /rest/api/2/search
    if isinstance(data, dict) and "issues" in data:
        for issue in data["issues"]:
            fields = issue.get("fields", {})
            if "summary" in fields:
                fields["summary"] = sanitize_summary(fields["summary"])
                modified = True

    return json.dumps(data, ensure_ascii=False).encode() if modified else body


# ---------------------------------------------------------------------------
# Proxy handler
# ---------------------------------------------------------------------------

HOP_BY_HOP = frozenset({
    "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "upgrade",
})


async def proxy_handler(request: Request) -> Response:
    path = request.url.path
    query = str(request.url.query)
    method = request.method
    body = await request.body()

    target_url = f"{JIRA_URL}{path}"
    if query:
        target_url += f"?{query}"

    # Forward headers, strip auth + host (we inject our own auth)
    base_headers = {}
    for k, v in request.headers.items():
        if k.lower() in ("host", "authorization", "content-length"):
            continue
        base_headers[k] = v

    cookie_header = get_cookie_header()
    if not cookie_header and not BEARER_TOKEN:
        log.warning("No cookies and no BEARER_TOKEN — user needs to visit Jira in Firefox, update at http://localhost:%d, or set BEARER_TOKEN in .env.local", WEB_PORT)
        return Response(
            content=json.dumps({
                "errorMessages": ["Proxy: no session cookies and no BEARER_TOKEN fallback. Visit Jira in Firefox or set BEARER_TOKEN in .env.local."],
                "errors": {},
            }),
            status_code=401,
            media_type="application/json",
        )

    async def forward(auth_mode: str) -> httpx.Response:
        headers = dict(base_headers)
        if auth_mode == "cookie":
            headers["cookie"] = cookie_header
            if load_cookies().get("atlassian.xsrf.token"):
                headers["x-atlassian-token"] = "no-check"
        else:  # bearer
            headers["authorization"] = f"Bearer {BEARER_TOKEN}"
            headers["x-atlassian-token"] = "no-check"
        async with httpx.AsyncClient(verify=SSL_VERIFY, timeout=30, follow_redirects=True) as client:
            return await client.request(method, target_url, headers=headers, content=body)

    t0 = time.monotonic()
    try:
        # Circuit breaker: skip cookies entirely while circuit is open and bearer is available
        if cookie_header and not (cookie_breaker_open() and BEARER_TOKEN):
            auth_used = "cookie"
        else:
            auth_used = "bearer"
        resp = await forward(auth_used)
        # Fall back to bearer on 401; trip the breaker so subsequent calls skip cookies
        if resp.status_code == 401 and auth_used == "cookie" and BEARER_TOKEN:
            log.warning("%s %s -> 401 with cookies — failing over to BEARER_TOKEN", method, path)
            cookie_breaker_trip()
            resp = await forward("bearer")
            auth_used = "bearer"
        elif auth_used == "cookie" and resp.status_code < 400:
            # Half-open probe succeeded — close the circuit
            cookie_breaker_close()
    except httpx.ConnectError as e:
        log.error("Cannot reach %s: %s", JIRA_URL, e)
        return Response(content=json.dumps({"errorMessages": [f"Proxy: cannot reach {JIRA_URL}"]}),
                        status_code=502, media_type="application/json")
    except httpx.TimeoutException:
        log.error("Timeout connecting to %s", JIRA_URL)
        return Response(content=json.dumps({"errorMessages": [f"Proxy: timeout reaching {JIRA_URL}"]}),
                        status_code=504, media_type="application/json")

    elapsed = time.monotonic() - t0

    if resp.status_code == 401:
        log.warning("%s %s -> %d (%.2fs) [%s] — auth failed, refresh cookies or check BEARER_TOKEN", method, path, resp.status_code, elapsed, auth_used)
    else:
        log.info("%s %s -> %d (%.2fs) [%s]", method, path, resp.status_code, elapsed, auth_used)

    if LOG_RESPONSES:
        log.info("RESPONSE: %s", resp.text[:4000] if resp.text else "(empty)")

    # Sanitize summaries for IDE branch name compatibility
    content_type = resp.headers.get("content-type", "")
    response_body = sanitize_response_body(resp.content, content_type)

    # Return response, stripping hop-by-hop headers
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}

    return Response(content=response_body, status_code=resp.status_code, headers=resp_headers)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def health(request: Request) -> Response:
    cookies = load_cookies()
    breaker_remaining = max(0.0, _cookie_breaker["open_until"] - time.monotonic())
    return Response(
        content=json.dumps({
            "status": "ok",
            "jira_url": JIRA_URL,
            "has_cookies": bool(cookies),
            "cookie_keys": list(cookies.keys()),
            "has_bearer_token": bool(BEARER_TOKEN),
            "cookie_circuit_open": breaker_remaining > 0,
            "cookie_circuit_reset_in_s": round(breaker_remaining, 1),
            "proxy_port": PROXY_PORT,
        }),
        media_type="application/json",
    )

# ---------------------------------------------------------------------------
# Browser redirect — /browse/KEY-123 → real Jira URL
# ---------------------------------------------------------------------------

async def browse_redirect(request: Request) -> Response:
    issue_key = request.path_params["issue_key"]
    location = f"{JIRA_URL}/browse/{issue_key}"
    log.info("Redirect /browse/%s -> %s", issue_key, location)
    return Response(status_code=301, headers={"location": location})

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Starlette(routes=[
    Route("/_proxy/health", health, methods=["GET"]),
    Route("/browse/{issue_key}", browse_redirect, methods=["GET"]),
    Route("/{path:path}", proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]),
])

if __name__ == "__main__":
    log.info("Jira HTTP Proxy starting on http://localhost:%d -> %s", PROXY_PORT, JIRA_URL)
    log.info("SSL verify: %s", SSL_VERIFY)
    log.info("Cookie file: %s", COOKIE_FILE)
    uvicorn.run(app, host="127.0.0.1", port=PROXY_PORT, log_level="warning")
