#!/usr/bin/env python3
"""
Jira MCP Server with Cookie Authentication
-------------------------------------------
A lightweight MCP server that proxies Jira REST API calls using browser
session cookies, bypassing API-token rate limits.

Includes a local web UI (default http://localhost:9777) where you can
paste your JSESSIONID cookie. Any MCP client (Claude Desktop, VS Code,
Cowork, Claude Code) can then query Jira through the exposed tools.
"""

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, quote

import httpx
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config — load .env.local next to this script, then allow env vars to override
# ---------------------------------------------------------------------------

def _load_env_local():
    """Read key=value pairs from .env.local (ignoring comments and Makefile syntax)."""
    # Check ENV_FILE env var first (set by brew wrapper), then fall back to script dir
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
        key = key.strip()
        value = value.strip()
        # Skip Makefile expressions like $(shell ...) — not resolvable in Python
        if "$(" in value and "HOME" not in value:
            continue
        # Expand $(HOME)
        value = value.replace("$(HOME)", str(Path.home()))
        # Only set if not already in the environment (env vars take precedence)
        if key not in os.environ:
            os.environ[key] = value

_load_env_local()

JIRA_URL = os.environ.get("JIRA_URL", "https://jira.example.com")
WEB_PORT = int(os.environ.get("WEB_PORT", "9777"))
COOKIE_FILE = Path(os.environ.get(
    "COOKIE_FILE",
    Path.home() / ".jira-mcp-cookies.json"
))
BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "").strip() or None
CIRCUIT_RECOVERY_SECONDS = float(os.environ.get("CIRCUIT_RECOVERY_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Cookie store (thread-safe via the GIL for simple reads/writes)
# ---------------------------------------------------------------------------

def load_cookies() -> dict:
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cookies(cookies: dict):
    COOKIE_FILE.write_text(json.dumps(cookies, indent=2))


def get_cookie_header() -> str:
    cookies = load_cookies()
    parts = [f"{k}={v}" for k, v in cookies.items() if not k.startswith("_")]
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Circuit breaker — trips when cookie auth returns 401 so we stop re-probing
# expired cookies on every call and route straight to BEARER_TOKEN until
# the recovery timeout elapses (then one request probes cookies again).
# ---------------------------------------------------------------------------

_cookie_breaker = {"open_until": 0.0}


def cookie_breaker_open() -> bool:
    return time.monotonic() < _cookie_breaker["open_until"]


def cookie_breaker_trip():
    _cookie_breaker["open_until"] = time.monotonic() + CIRCUIT_RECOVERY_SECONDS
    print(
        f"[circuit] Cookie auth OPENED for {int(CIRCUIT_RECOVERY_SECONDS)}s — routing via BEARER_TOKEN",
        file=sys.stderr,
    )


def cookie_breaker_close():
    if _cookie_breaker["open_until"]:
        print("[circuit] Cookie auth CLOSED — cookies accepted again", file=sys.stderr)
    _cookie_breaker["open_until"] = 0.0


# ---------------------------------------------------------------------------
# Jira HTTP helper
# ---------------------------------------------------------------------------

def jira_request(method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
    cookie_header = get_cookie_header()
    if not cookie_header and not BEARER_TOKEN:
        return {"error": "No cookies configured and no BEARER_TOKEN set. Open http://localhost:{} to set your JSESSIONID, or add BEARER_TOKEN to .env.local.".format(WEB_PORT)}

    url = f"{JIRA_URL}{path}"
    base_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    def send(auth_mode: str) -> httpx.Response:
        headers = dict(base_headers)
        if auth_mode == "cookie":
            headers["Cookie"] = cookie_header
            if load_cookies().get("atlassian.xsrf.token"):
                headers["X-Atlassian-Token"] = "no-check"
        else:  # bearer
            headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
            headers["X-Atlassian-Token"] = "no-check"
        with httpx.Client(verify=True, timeout=30) as client:
            return client.request(method, url, headers=headers, params=params, json=json_body)

    # Circuit breaker: skip cookies while circuit is open and bearer is available
    if cookie_header and not (cookie_breaker_open() and BEARER_TOKEN):
        auth_used = "cookie"
    else:
        auth_used = "bearer"
    resp = send(auth_used)
    # Trip breaker + fail over to bearer on 401 from cookie auth
    if resp.status_code == 401 and auth_used == "cookie" and BEARER_TOKEN:
        cookie_breaker_trip()
        resp = send("bearer")
        auth_used = "bearer"
    elif auth_used == "cookie" and resp.status_code < 400:
        # Half-open probe succeeded — close the circuit
        cookie_breaker_close()

    if resp.status_code == 401:
        if auth_used == "bearer":
            return {"error": "BEARER_TOKEN rejected by Jira. Check the token in .env.local."}
        return {"error": "Session expired. Please update your cookie at http://localhost:{} or set BEARER_TOKEN in .env.local.".format(WEB_PORT)}
    if resp.status_code == 429:
        return {"error": "Rate limited by Jira. Try again later or update cookies."}
    if resp.status_code >= 400:
        try:
            return {"error": resp.json()}
        except Exception:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:500]}"}

    if not resp.text.strip():
        return {"ok": True}
    return resp.json()


# ---------------------------------------------------------------------------
# Web UI for cookie management
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jira MCP – Cookie Manager</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0a0b; --surface: #141416; --border: #2a2a2e;
  --text: #e4e4e7; --muted: #71717a; --accent: #3b82f6;
  --accent-hover: #2563eb; --green: #22c55e; --red: #ef4444;
  --amber: #f59e0b;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'IBM Plex Sans',sans-serif; background:var(--bg); color:var(--text); min-height:100vh; display:flex; align-items:center; justify-content:center; padding:2rem; }
.container { max-width:540px; width:100%; }
.logo { font-family:'IBM Plex Mono',monospace; font-size:13px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin-bottom:2rem; }
.logo span { color:var(--accent); }
h1 { font-size:1.75rem; font-weight:600; margin-bottom:0.5rem; line-height:1.2; }
.subtitle { color:var(--muted); font-size:0.95rem; margin-bottom:2rem; line-height:1.6; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.5rem; margin-bottom:1rem; }
.status-bar { display:flex; align-items:center; gap:8px; margin-bottom:1.5rem; padding:10px 14px; border-radius:8px; font-size:0.85rem; font-family:'IBM Plex Mono',monospace; }
.status-bar.ok { background:rgba(34,197,94,0.08); border:1px solid rgba(34,197,94,0.2); color:var(--green); }
.status-bar.warn { background:rgba(245,158,11,0.08); border:1px solid rgba(245,158,11,0.2); color:var(--amber); }
.status-bar.err { background:rgba(239,68,68,0.08); border:1px solid rgba(239,68,68,0.2); color:var(--red); }
.dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
.status-bar.ok .dot { background:var(--green); }
.status-bar.warn .dot { background:var(--amber); }
.status-bar.err .dot { background:var(--red); }
label { display:block; font-size:0.8rem; font-weight:500; text-transform:uppercase; letter-spacing:1px; color:var(--muted); margin-bottom:6px; }
input[type="text"] { width:100%; padding:10px 14px; background:var(--bg); border:1px solid var(--border); border-radius:8px; color:var(--text); font-family:'IBM Plex Mono',monospace; font-size:0.9rem; outline:none; transition:border 0.2s; }
input:focus { border-color:var(--accent); }
.field { margin-bottom:1rem; }
.help { font-size:0.78rem; color:var(--muted); margin-top:4px; line-height:1.5; }
button { width:100%; padding:12px; background:var(--accent); color:#fff; border:none; border-radius:8px; font-family:'IBM Plex Sans',sans-serif; font-size:0.95rem; font-weight:500; cursor:pointer; transition:background 0.2s, transform 0.1s; }
button:hover { background:var(--accent-hover); }
button:active { transform:scale(0.98); }
.footer { text-align:center; font-size:0.75rem; color:var(--muted); margin-top:1.5rem; font-family:'IBM Plex Mono',monospace; }
.footer a { color:var(--accent); text-decoration:none; }
</style>
</head>
<body>
<div class="container">
  <div class="logo">Jira <span>MCP</span> Server</div>
  <h1>Cookie authentication</h1>
  <p class="subtitle">Paste your browser session cookie to let any AI agent access Jira without hitting API rate limits.</p>

  <div id="status" class="status-bar warn"><span class="dot"></span><span id="status-text">Checking connection...</span></div>

  <div class="card">
    <div class="field">
      <label>JSESSIONID</label>
      <input type="text" id="jsessionid" placeholder="e.g. 58DF4A866AE24758973BEE3BF4253E9E">
      <p class="help">Firefox: F12 → Storage → Cookies → JIRA_DOMAIN_PLACEHOLDER<br>Chrome: F12 → Application → Cookies</p>
    </div>
    <div class="field">
      <label>XSRF Token (optional)</label>
      <input type="text" id="xsrf" placeholder="e.g. BMIU-X3NN-FN9R-QGUJ_...">
      <p class="help">Needed for write operations (comments, transitions). Same location as JSESSIONID.</p>
    </div>
    <button onclick="saveCookies()">Save & test connection</button>
  </div>

  <div class="footer">
    MCP endpoint: stdio &middot; Jira: <a href="JIRA_URL_PLACEHOLDER" target="_blank">JIRA_URL_PLACEHOLDER</a>
  </div>
</div>
<script>
const BASE = '';

async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const el = document.getElementById('status');
    const txt = document.getElementById('status-text');
    if (d.authenticated) {
      el.className = 'status-bar ok';
      txt.textContent = 'Connected as ' + d.user;
    } else if (d.has_cookies) {
      el.className = 'status-bar err';
      txt.textContent = 'Cookie expired – please update';
    } else {
      el.className = 'status-bar warn';
      txt.textContent = 'No cookies set – paste your JSESSIONID below';
    }
    if (d.cookies && d.cookies.JSESSIONID) {
      document.getElementById('jsessionid').value = d.cookies.JSESSIONID;
    }
    if (d.cookies && d.cookies['atlassian.xsrf.token']) {
      document.getElementById('xsrf').value = d.cookies['atlassian.xsrf.token'];
    }
  } catch(e) {
    document.getElementById('status').className = 'status-bar err';
    document.getElementById('status-text').textContent = 'Cannot reach server';
  }
}

async function saveCookies() {
  const jsessionid = document.getElementById('jsessionid').value.trim();
  const xsrf = document.getElementById('xsrf').value.trim();
  if (!jsessionid) { alert('JSESSIONID is required'); return; }
  
  const cookies = { JSESSIONID: jsessionid };
  if (xsrf) cookies['atlassian.xsrf.token'] = xsrf;

  const r = await fetch('/api/cookies', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(cookies)
  });
  const d = await r.json();
  checkStatus();
}

checkStatus();
</script>
</body>
</html>""".replace("JIRA_URL_PLACEHOLDER", JIRA_URL).replace("JIRA_DOMAIN_PLACEHOLDER", JIRA_URL.split("//")[-1])


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress logs

    def do_GET(self):
        if self.path == "/api/status":
            cookies = load_cookies()
            breaker_remaining = max(0.0, _cookie_breaker["open_until"] - time.monotonic())
            result = {
                "has_cookies": bool(cookies),
                "authenticated": False,
                "cookies": cookies,
                "has_bearer_token": bool(BEARER_TOKEN),
                "cookie_circuit_open": breaker_remaining > 0,
                "cookie_circuit_reset_in_s": round(breaker_remaining, 1),
            }
            if cookies or BEARER_TOKEN:
                data = jira_request("GET", "/rest/api/2/myself")
                if "displayName" in data or "name" in data:
                    result["authenticated"] = True
                    result["user"] = data.get("displayName", data.get("name", "unknown"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())

    def do_POST(self):
        if self.path == "/api/cookies":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            save_cookies(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        else:
            self.send_response(404)
            self.end_headers()


def start_web_ui():
    server = HTTPServer(("127.0.0.1", WEB_PORT), WebHandler)
    server.serve_forever()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Jira Cookie MCP",
    instructions="""Jira MCP Server using browser cookie authentication.
    Connects to: {url}
    Cookie management UI: http://localhost:{port}
    
    If you get authentication errors, ask the user to update their 
    session cookie at http://localhost:{port}""".format(url=JIRA_URL, port=WEB_PORT)
)


@mcp.tool()
def jira_myself() -> str:
    """Get the currently authenticated Jira user profile."""
    return json.dumps(jira_request("GET", "/rest/api/2/myself"), indent=2)


@mcp.tool()
def jira_search(jql: str, max_results: int = 20, fields: str = "summary,status,priority,issuetype,assignee") -> str:
    """Search Jira issues using JQL.
    
    Args:
        jql: JQL query string, e.g. 'project = PEP AND assignee = currentUser() AND resolution = Unresolved'
        max_results: Maximum results to return (1-50)
        fields: Comma-separated field names to include
    """
    params = {"jql": jql, "maxResults": min(max_results, 50), "fields": fields}
    data = jira_request("GET", "/rest/api/2/search", params=params)
    
    if "error" in data:
        return json.dumps(data, indent=2)
    
    # Compact output for better token efficiency
    issues = []
    for issue in data.get("issues", []):
        f = issue.get("fields", {})
        compact = {
            "key": issue["key"],
            "summary": f.get("summary"),
            "status": f.get("status", {}).get("name") if f.get("status") else None,
            "priority": f.get("priority", {}).get("name") if f.get("priority") else None,
            "type": f.get("issuetype", {}).get("name") if f.get("issuetype") else None,
            "assignee": f.get("assignee", {}).get("displayName") if f.get("assignee") else None,
        }
        issues.append(compact)
    
    return json.dumps({"total": data.get("total", 0), "issues": issues}, indent=2)


@mcp.tool()
def jira_get_issue(issue_key: str, fields: str = "*all", comment_limit: int = 5) -> str:
    """Get details of a specific Jira issue.
    
    Args:
        issue_key: Issue key like PEP-6530
        fields: Comma-separated fields or '*all'
        comment_limit: Max comments to return
    """
    params = {"fields": fields}
    data = jira_request("GET", f"/rest/api/2/issue/{issue_key}", params=params)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def jira_add_comment(issue_key: str, comment: str) -> str:
    """Add a comment to a Jira issue.
    
    Args:
        issue_key: Issue key like PEP-6530
        comment: Comment text (supports Jira wiki markup)
    """
    data = jira_request("POST", f"/rest/api/2/issue/{issue_key}/comment", json_body={"body": comment})
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def jira_get_transitions(issue_key: str) -> str:
    """Get available status transitions for a Jira issue.
    
    Args:
        issue_key: Issue key like PEP-6530
    """
    data = jira_request("GET", f"/rest/api/2/issue/{issue_key}/transitions")
    return json.dumps(data, indent=2)


@mcp.tool()
def jira_transition_issue(issue_key: str, transition_id: str, comment: str = None) -> str:
    """Transition a Jira issue to a new status.
    
    Args:
        issue_key: Issue key like PEP-6530
        transition_id: Transition ID (get from jira_get_transitions)
        comment: Optional comment to add with the transition
    """
    body = {"transition": {"id": transition_id}}
    if comment:
        body["update"] = {"comment": [{"add": {"body": comment}}]}
    data = jira_request("POST", f"/rest/api/2/issue/{issue_key}/transitions", json_body=body)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def jira_get_projects() -> str:
    """List all accessible Jira projects."""
    data = jira_request("GET", "/rest/api/2/project")
    if isinstance(data, list):
        projects = [{"key": p.get("key"), "name": p.get("name"), "lead": p.get("lead", {}).get("displayName")} for p in data]
        return json.dumps(projects, indent=2)
    return json.dumps(data, indent=2)


@mcp.tool()
def jira_get_boards(project_key: str = None) -> str:
    """List agile boards, optionally filtered by project.
    
    Args:
        project_key: Optional project key to filter boards
    """
    params = {}
    if project_key:
        params["projectKeyOrId"] = project_key
    data = jira_request("GET", "/rest/agile/1.0/board", params=params)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def jira_get_sprints(board_id: int, state: str = "active") -> str:
    """Get sprints for a board.
    
    Args:
        board_id: Board ID (get from jira_get_boards)
        state: Sprint state filter: active, future, closed
    """
    params = {"state": state}
    data = jira_request("GET", f"/rest/agile/1.0/board/{board_id}/sprint", params=params)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def jira_update_cookie(jsessionid: str, xsrf_token: str = None) -> str:
    """Update the Jira session cookie. Use this if the session expires.
    
    Args:
        jsessionid: The JSESSIONID value from your browser
        xsrf_token: Optional atlassian.xsrf.token for write operations
    """
    cookies = {"JSESSIONID": jsessionid}
    if xsrf_token:
        cookies["atlassian.xsrf.token"] = xsrf_token
    save_cookies(cookies)
    
    # Test the new cookie
    data = jira_request("GET", "/rest/api/2/myself")
    if "name" in data or "displayName" in data:
        return json.dumps({"ok": True, "user": data.get("displayName", data.get("name"))})
    return json.dumps({"error": "Cookie invalid or expired", "details": data})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start web UI in background thread
    web_thread = threading.Thread(target=start_web_ui, daemon=True)
    web_thread.start()
    
    # Open browser on first run if no cookies exist
    if not load_cookies():
        try:
            webbrowser.open(f"http://localhost:{WEB_PORT}")
        except Exception:
            pass
    
    print(f"Cookie manager UI: http://localhost:{WEB_PORT}", file=sys.stderr)
    print(f"Jira URL: {JIRA_URL}", file=sys.stderr)
    
    # Start MCP server (stdio)
    mcp.run()