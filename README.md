# Jira MCP Server + Firefox Cookie Bridge

An MCP (Model Context Protocol) server that gives Claude access to your Jira instance, paired with a Firefox extension that **automatically syncs your Jira session cookies** — no more manual copy-paste.

## How It Works

```
┌──────────────────────┐     cookies.onChanged      ┌──────────────────────┐
│   Firefox Browser    │ ──────────────────────────→ │  Background Script   │
│   (visit Jira)       │     webNavigation event     │  (extension)         │
└──────────────────────┘                             └──────────┬───────────┘
                                                                │
                                                   sendNativeMessage()
                                                                │
                                                                ▼
                                                  ┌─────────────────────────┐
                                                  │  jira_cookie_bridge.py  │
                                                  │  (native messaging host)│
                                                  └──────────┬──────────────┘
                                                             │
                                                        write JSON
                                                             │
                                                             ▼
                                                  ┌─────────────────────────┐
                                                  │ ~/.jira-mcp-cookies.json│
                                                  └──────────┬──────────────┘
                                                             │
                                                          read on
                                                        each request
                                                             │
                                                             ▼
                                                  ┌─────────────────────────┐
                                                  │     server.py           │
                                                  │  (Jira MCP server)     │
                                                  └─────────────────────────┘
```

1. You visit Jira in Firefox (or a cookie refreshes in the background).
2. The extension reads the `JSESSIONID` and `atlassian.xsrf.token` cookies via the `browser.cookies` API (which **can** access httpOnly cookies).
3. It sends them to a tiny Python script via Firefox Native Messaging.
4. The script writes them to `~/.jira-mcp-cookies.json`.
5. The MCP server reads that file on every Jira API call.

No running daemon, no localhost HTTP server dependency — the cookie file is the only shared state.

## Prerequisites

- **macOS** (the Makefile uses macOS paths for Firefox native messaging)
- **Firefox** (109+ for Manifest V2 background scripts)
- **Python 3** (for the native messaging host — no pip dependencies)
- **GNU Make**

## Install via Homebrew

```bash
brew tap posalex/tap
brew install jira-mcp-server
```

Then follow the post-install instructions printed by `brew info jira-mcp-server`.

## Quick Start (from source)

### 1. Configure

Edit `.env.local` to match your environment:

```bash
# .env.local — at minimum, set this:
JIRA_URL=https://jira.example.com
```

The defaults should work for most setups. Run `make check-env` to verify:

```bash
make check-env
```

### 2. Build & Install

```bash
make all
```

This does three things:

- **Builds** the extension from templates → `build/extension/`
- **Installs** the native messaging host manifest → `~/Library/Application Support/Mozilla/NativeMessagingHosts/`
- **Packages** the extension as `build/jira-cookie-bridge.xpi`

### 3. Load the Extension in Firefox

**Option A — Temporary (for development):**

1. Open Firefox and go to `about:debugging#/runtime/this-firefox`
2. Click **"Load Temporary Add-on…"**
3. Select `build/extension/manifest.json`

> Note: Temporary extensions are removed when Firefox restarts.

**Option B — Permanent (self-signed .xpi):**

For a permanent install you'd need to sign the extension via [addons.mozilla.org](https://addons.mozilla.org) or use Firefox Developer/Nightly with `xpinstall.signatures.required` set to `false`.

### 4. Verify

1. Open the Firefox **Browser Console** (`Cmd+Shift+J`) and look for:
   ```
   [Jira Cookie Bridge] Watching jira.example.com for cookie changes.
   ```
2. Navigate to your Jira instance.
3. You should see:
   ```
   [Jira Cookie Bridge] Cookies synced (navigation): ["JSESSIONID", "atlassian.xsrf.token"]
   ```
4. Check the cookie file:
   ```bash
   cat ~/.jira-mcp-cookies.json
   ```

## HTTP Proxy for IDE Integration (PhpStorm, IntelliJ)

The proxy lets PhpStorm's native Jira integration work with cookie-based
authentication. It intercepts local REST API requests, injects your browser
session cookies, and forwards them to Jira.

```
PhpStorm  -->  localhost:9778  -->  jira.example.com
               (inject cookies)
```

### Run the proxy

```bash
# Foreground (development)
make proxy

# Install as macOS service (runs at login, auto-restarts)
make proxy-start

# Other service commands
make proxy-stop
make proxy-restart
make proxy-status
make proxy-logs
make proxy-uninstall
```

### PhpStorm Configuration

1. Open **Settings -> Tools -> Tasks -> Servers**
2. Click **+** and choose **Jira**
3. Set:
   - **Server URL**: `http://localhost:9778`
   - **Username**: `proxy` (any value works, it's ignored)
   - **Password**: `proxy` (any value works, it's ignored)
   - **Search**: leave default
4. Click **Test** — should succeed if cookies are valid

The proxy strips the Basic Auth header PhpStorm sends and replaces it
with your browser session cookies.

### Health check

```bash
curl http://localhost:9778/_proxy/health
```

## Makefile Targets

| Target       | Description                                       |
| ------------ | ------------------------------------------------- |
| `make all`   | Build + install native host + package .xpi        |
| `make build` | Generate extension from templates into `build/`   |
| `make install` | Register native messaging host with Firefox     |
| `make uninstall` | Remove native messaging host registration    |
| `make xpi`   | Package extension as `.xpi`                       |
| `make clean` | Remove `build/` directory                         |
| `make check-env` | Print resolved configuration                 |
| `make proxy` | Run the HTTP proxy in the foreground              |
| `make proxy-start` | Install and start proxy as macOS service   |
| `make proxy-stop` | Stop the proxy service                      |
| `make proxy-restart` | Restart the proxy service                |
| `make proxy-status` | Check if the proxy is running              |
| `make proxy-logs` | Tail the proxy log files                     |
| `make proxy-uninstall` | Remove the proxy service                 |

## File Structure

```
jira-mcp-server/
├── .env.local                          # All configuration lives here
├── Makefile                            # Build system
├── README.md                           # This file
├── server.py                           # The MCP server
├── proxy.py                            # HTTP reverse proxy for IDE integration
│
├── native-host/
│   └── jira_cookie_bridge.py           # Native messaging host script
│
├── firefox-extension/
│   ├── manifest.json.template          # Extension manifest (with @@placeholders@@)
│   ├── background.js.template          # Background script (with @@placeholders@@)
│   └── icons/
│       └── icon-48.svg                 # Extension icon
│
├── launchd/
│   └── com.jira-mcp.proxy.plist.template  # macOS service template for the proxy
│
└── build/                              # Generated by `make build`
    ├── extension/
    │   ├── manifest.json               # Ready-to-load extension
    │   ├── background.js
    │   └── icons/icon-48.svg
    └── jira-cookie-bridge.xpi          # Packaged extension
```

## Troubleshooting

### Firefox Extension

**"Error: No such native application"**
The native messaging host manifest isn't installed or has the wrong path. Run:
```bash
make install
cat ~/Library/Application\ Support/Mozilla/NativeMessagingHosts/jira.cookie.bridge.json
```
Verify `"path"` points to the actual location of `jira_cookie_bridge.py`. If you
installed via Homebrew, the path should use `/opt/homebrew/opt/jira-mcp-server/libexec/...`
(not a versioned Cellar path). Re-run `make -C /opt/homebrew/opt/jira-mcp-server/libexec install`
to fix it.

**Cookies not syncing**
- Open the Browser Console (`Cmd+Shift+J`) and check for `[Jira Cookie Bridge]` log messages.
- Make sure you're on the correct domain (derived from `JIRA_URL` in `.env.local`).
- Verify the extension is loaded at `about:debugging#/runtime/this-firefox`.
- Check `~/.jira-mcp-cookies.json` — the `_updated_at` field shows when cookies were last synced.

**Cookies stale after `brew upgrade`**
The native messaging host manifest may point to an old Cellar path that no longer exists.
Fix it by re-running:
```bash
make -C /opt/homebrew/opt/jira-mcp-server/libexec install
```
Then reload the extension in Firefox (`about:addons` -> remove -> reinstall from `.xpi`).

**"Permission denied" on the native host script**
```bash
chmod +x native-host/jira_cookie_bridge.py
```

**Cookie file not updating**
Test the native host directly:
```bash
echo -ne '\x0d\x00\x00\x00{"action":"status"}' | python3 native-host/jira_cookie_bridge.py
```
You should get a JSON response with `"ok": true`.

### HTTP Proxy

**PhpStorm: "Login failed. Check your credentials."**
1. Check if the proxy is running: `curl http://localhost:9778/_proxy/health`
2. If not running, start it: `jira-proxy` or `make proxy-start`
3. If running but returning 401, your session cookies have expired. Visit your Jira
   instance in Firefox to refresh them. Check `~/.jira-mcp-cookies.json` for the
   `_updated_at` timestamp and `_expiry` TTL.

**Proxy not starting (port in use)**
Another process is using port 9778. Either stop it or change `PROXY_PORT` in `.env.local`.
```bash
lsof -i :9778
```

**Proxy not responding after `brew upgrade`**
The proxy service auto-restarts on upgrade, but if it doesn't:
```bash
# Check status
launchctl list | grep jira-mcp

# Manually restart
launchctl unload ~/Library/LaunchAgents/com.jira-mcp.proxy.plist
launchctl load ~/Library/LaunchAgents/com.jira-mcp.proxy.plist
```

**Proxy logs**
```bash
# If running as a service
tail -f ~/Library/Logs/jira-mcp/jira-proxy.err

# Or via make (from source)
make proxy-logs
```

**Enable verbose response logging**
Set `PROXY_LOG_RESPONSES=true` in `.env.local` and restart the proxy. Every
response body will be logged (up to 4KB). Disable when done debugging.

**Branch names invalid in PhpStorm**
The proxy sanitizes Jira issue summaries by default, keeping only `A-Za-z0-9`, spaces,
`_` and `-`. If this causes issues, disable it with `PROXY_SANITIZE_SUMMARIES=false`
in `.env.local`.

### MCP Server

**MCP tools not working / authentication errors**
The MCP server reads the same `~/.jira-mcp-cookies.json` file. If cookies are
expired, visit Jira in Firefox to refresh them. You can also paste cookies
manually at `http://localhost:9777`.

**"No cookies configured"**
The cookie file doesn't exist or is empty. Either:
- Visit Jira in Firefox (extension syncs automatically), or
- Open `http://localhost:9777` and paste your `JSESSIONID` manually

### General

**Check your configuration**
```bash
make check-env
```

**Check cookie file health**
```bash
cat ~/.jira-mcp-cookies.json
```
Look at `_updated_at` to see when cookies were last synced, and `_expiry` for
TTL remaining. If `ttl` shows `"expired"`, visit Jira in Firefox.

## Uninstalling

```bash
# From source
make uninstall        # Remove native messaging host manifest
make proxy-uninstall  # Remove proxy service
make clean            # Remove build artifacts

# Homebrew
brew uninstall jira-mcp-server
rm -rf /opt/homebrew/etc/jira-mcp-server
rm ~/Library/LaunchAgents/com.jira-mcp.proxy.plist
```

Then remove the extension from Firefox at `about:addons`.
