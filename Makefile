# =============================================================================
# Jira Cookie Bridge — Build & Install
# =============================================================================
# Reads .env.local and builds the Firefox extension + native messaging host.
#
# Usage:
#   make build       — generate extension files from templates
#   make install     — register the native messaging host with Firefox
#   make uninstall   — remove the native messaging host registration
#   make clean       — remove generated files
#   make xpi         — package the extension as .xpi for installation
#   make all         — build + install + xpi
# =============================================================================

SHELL := /bin/bash

# Load configuration from .env.local (optional — `make configure` creates it)
-include .env.local

# Derive JIRA_DOMAIN from JIRA_URL (strip scheme)
JIRA_DOMAIN := $(shell echo '$(JIRA_URL)' | sed -E 's|^https?://||')

# Resolve COOKIE_FILE (expand $(HOME) to actual home dir)
COOKIE_FILE_RESOLVED := $(subst $$(HOME),$(HOME),$(COOKIE_FILE))

# Resolve NATIVE_HOST_PATH (use pwd -L to preserve opt symlink in brew installs)
NATIVE_HOST_PATH_RESOLVED := $(shell pwd -L)/native-host/jira_cookie_bridge.py

# Proxy settings
PROXY_PORT       ?= 9778
PROXY_PLIST_LABEL := com.jira-mcp.proxy
PROXY_PLIST_DIR   := $(HOME)/Library/LaunchAgents
PROXY_PLIST       := $(PROXY_PLIST_DIR)/$(PROXY_PLIST_LABEL).plist
PROXY_LOG_DIR     := $(HOME)/Library/Logs/jira-mcp
# Use local .venv if it exists (source checkout), otherwise libexec venv (brew)
PYTHON_PATH       := $(shell test -x $(CURDIR)/.venv/bin/python3 && echo $(CURDIR)/.venv/bin/python3 || echo $(CURDIR)/bin/python3)

# Directories
EXT_SRC     := firefox-extension
EXT_BUILD   := build/extension
NATIVE_DIR  := native-host
BUILD_DIR   := build

# macOS native messaging host manifest location
NATIVE_MANIFEST_DIR := $(HOME)/Library/Application Support/Mozilla/NativeMessagingHosts
NATIVE_MANIFEST     := $(NATIVE_MANIFEST_DIR)/$(NATIVE_HOST_NAME).json

# All placeholder tokens used in templates
TOKENS = \
	-e 's|@@EXTENSION_ID@@|$(EXTENSION_ID)|g' \
	-e 's|@@JIRA_DOMAIN@@|$(JIRA_DOMAIN)|g' \
	-e 's|@@JIRA_URL@@|$(JIRA_URL)|g' \
	-e 's|@@COOKIE_SESSION_NAME@@|$(COOKIE_SESSION_NAME)|g' \
	-e 's|@@COOKIE_XSRF_NAME@@|$(COOKIE_XSRF_NAME)|g' \
	-e 's|@@NATIVE_HOST_NAME@@|$(NATIVE_HOST_NAME)|g' \
	-e 's|@@VERSION@@|$(VERSION)|g'

# =============================================================================
# Targets
# =============================================================================

.PHONY: all configure build install uninstall clean xpi check-env \
       proxy proxy-install proxy-uninstall proxy-start proxy-stop proxy-restart proxy-status proxy-logs

all: configure build install xpi
	@echo ""
	@echo "✓ Done! Next steps:"
	@echo "  1. Install the .xpi in Firefox Developer Edition:"
	@echo "     about:config → set xpinstall.signatures.required to false"
	@echo "     about:addons → gear icon → Install Add-on From File..."
	@echo "     Select: $$(pwd -L)/$(BUILD_DIR)/jira-cookie-bridge.xpi"
	@echo "  2. Visit $(JIRA_URL) — cookies will sync automatically."
	@echo ""

## configure: Create .env.local if it doesn't exist (prompts for JIRA_URL)
configure: .env.local

.env.local:
	@read -p "Enter your Jira URL (e.g. https://jira.example.com): " jira_url; \
	sed "s|https://jira.example.com|$$jira_url|g" .env.local.example > .env.local; \
	echo "✓ Created .env.local with JIRA_URL=$$jira_url"

## build: Generate extension files from templates
build: $(EXT_BUILD)/manifest.json $(EXT_BUILD)/background.js $(EXT_BUILD)/icons/icon-48.svg
	@echo "✓ Extension built in $(EXT_BUILD)/"

$(EXT_BUILD):
	mkdir -p $(EXT_BUILD)/icons

$(EXT_BUILD)/manifest.json: $(EXT_SRC)/manifest.json.template .env.local | $(EXT_BUILD)
	sed $(TOKENS) $< > $@

$(EXT_BUILD)/background.js: $(EXT_SRC)/background.js.template .env.local | $(EXT_BUILD)
	sed $(TOKENS) $< > $@

$(EXT_BUILD)/icons/icon-48.svg: $(EXT_SRC)/icons/icon-48.svg | $(EXT_BUILD)
	cp $< $@

## install: Register the native messaging host with Firefox (macOS)
install: build
	@echo "Installing native messaging host manifest..."
	mkdir -p "$(NATIVE_MANIFEST_DIR)"
	@echo '{'                                                          >  "$(NATIVE_MANIFEST)"
	@echo '  "name": "$(NATIVE_HOST_NAME)",'                          >> "$(NATIVE_MANIFEST)"
	@echo '  "description": "Jira Cookie Bridge for MCP server",'     >> "$(NATIVE_MANIFEST)"
	@echo '  "path": "$(NATIVE_HOST_PATH_RESOLVED)",'                 >> "$(NATIVE_MANIFEST)"
	@echo '  "type": "stdio",'                                        >> "$(NATIVE_MANIFEST)"
	@echo '  "allowed_extensions": ["$(EXTENSION_ID)"]'                >> "$(NATIVE_MANIFEST)"
	@echo '}'                                                          >> "$(NATIVE_MANIFEST)"
	chmod +x "$(NATIVE_HOST_PATH_RESOLVED)"
	@echo "✓ Native host manifest installed at:"
	@echo "  $(NATIVE_MANIFEST)"

## uninstall: Remove the native messaging host registration
uninstall:
	rm -f "$(NATIVE_MANIFEST)"
	@echo "✓ Native host manifest removed."

## xpi: Package the extension as an .xpi file (just a zip)
xpi: build
	cd $(EXT_BUILD) && zip -r -FS $(CURDIR)/$(BUILD_DIR)/jira-cookie-bridge.xpi . -x '*.DS_Store'
	@echo "✓ Extension packaged: $(BUILD_DIR)/jira-cookie-bridge.xpi"

## clean: Remove all generated files
clean:
	rm -rf $(BUILD_DIR)
	@echo "✓ Build artifacts removed."

# =============================================================================
# Proxy targets
# =============================================================================

## proxy: Run the HTTP proxy in the foreground (for development)
proxy:
	$(PYTHON_PATH) proxy.py

## proxy-install: Install launchd plist for the HTTP proxy
proxy-install:
	mkdir -p "$(PROXY_LOG_DIR)"
	mkdir -p "$(PROXY_PLIST_DIR)"
	sed -e 's|@@PYTHON_PATH@@|$(PYTHON_PATH)|g' \
	    -e 's|@@PROXY_SCRIPT_PATH@@|$(CURDIR)/proxy.py|g' \
	    -e 's|@@ENV_FILE_PATH@@|$(CURDIR)/.env.local|g' \
	    -e 's|@@LOG_DIR@@|$(PROXY_LOG_DIR)|g' \
	    launchd/com.jira-mcp.proxy.plist.template > "$(PROXY_PLIST)"
	@echo "✓ Plist installed at $(PROXY_PLIST)"

## proxy-start: Load and start the proxy service
proxy-start: proxy-install
	launchctl load "$(PROXY_PLIST)"
	@echo "✓ Proxy started on http://localhost:$(PROXY_PORT)"

## proxy-stop: Stop and unload the proxy service
proxy-stop:
	-launchctl unload "$(PROXY_PLIST)" 2>/dev/null
	@echo "✓ Proxy stopped"

## proxy-restart: Restart the proxy service
proxy-restart: proxy-stop proxy-start

## proxy-uninstall: Remove the launchd service
proxy-uninstall: proxy-stop
	rm -f "$(PROXY_PLIST)"
	@echo "✓ Proxy plist removed"

## proxy-status: Check if the proxy is running
proxy-status:
	@launchctl list 2>/dev/null | grep $(PROXY_PLIST_LABEL) && echo "✓ Proxy is running" || echo "✗ Proxy is not running"

## proxy-logs: Tail the proxy logs
proxy-logs:
	tail -f "$(PROXY_LOG_DIR)/jira-proxy.log" "$(PROXY_LOG_DIR)/jira-proxy.err"

## check-env: Print resolved configuration (useful for debugging)
check-env:
	@echo "JIRA_URL              = $(JIRA_URL)"
	@echo "JIRA_DOMAIN           = $(JIRA_DOMAIN)"
	@echo "COOKIE_FILE           = $(COOKIE_FILE_RESOLVED)"
	@echo "COOKIE_SESSION_NAME   = $(COOKIE_SESSION_NAME)"
	@echo "COOKIE_XSRF_NAME     = $(COOKIE_XSRF_NAME)"
	@echo "WEB_PORT              = $(WEB_PORT)"
	@echo "EXTENSION_ID          = $(EXTENSION_ID)"
	@echo "NATIVE_HOST_NAME      = $(NATIVE_HOST_NAME)"
	@echo "NATIVE_HOST_PATH      = $(NATIVE_HOST_PATH_RESOLVED)"
	@echo "NATIVE_MANIFEST       = $(NATIVE_MANIFEST)"
	@echo "PROXY_PORT            = $(PROXY_PORT)"
	@echo "PROXY_SSL_VERIFY      = $(PROXY_SSL_VERIFY)"
