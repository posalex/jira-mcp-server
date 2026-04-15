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

**"Error: No such native application"**
The native messaging host manifest isn't installed or has the wrong path. Run:
```bash
make install
cat ~/Library/Application\ Support/Mozilla/NativeMessagingHosts/jira.cookie.bridge.json
```
Verify `"path"` points to the actual location of `jira_cookie_bridge.py`.

**Cookies not syncing**
- Open the Browser Console (`Cmd+Shift+J`) and check for `[Jira Cookie Bridge]` log messages.
- Make sure you're on the correct domain (derived from `JIRA_URL` in `.env.local`).
- Verify the extension is loaded at `about:debugging#/runtime/this-firefox`.

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

## Uninstalling

```bash
make uninstall   # Remove native messaging host manifest
make clean       # Remove build artifacts
```

Then remove the extension from Firefox at `about:addons`.
