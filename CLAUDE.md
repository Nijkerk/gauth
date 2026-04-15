# gauth — Agent Context

## What this project does
Automates the daily `gcloud auth application-default login` + `gcloud auth login` flow for Solvinity engineers. Solvinity uses Google Vertex AI as the Claude backend (`solvinity-ai-usage-workplace` project). Both gcloud tokens expire every 24 hours by policy — no refresh token workaround is possible.

The script uses Opera browser via Chrome DevTools Protocol (CDP) to complete the OAuth flow hands-free. Solvinity SSO (Entra ID) handles authentication automatically because Opera already has an active SSO session.

## Flow
1. Start `gcloud auth application-default login --no-launch-browser` → gcloud outputs an auth URL
2. Open that URL in a new Opera tab via CDP
3. Google shows "Continue to Google Cloud SDK as Erik Granneman" page with a **Next** button → auto-clicked via CDP
4. Solvinity Entra ID SSO completes automatically (no user interaction needed)
5. Browser lands on `sdk.cloud.google/applicationdefaultauthcode.html` → extract verification code via CDP
6. Submit code to gcloud stdin → credentials saved
7. Run `gcloud auth application-default set-quota-project solvinity-ai-usage-workplace`
8. Repeat steps 1–6 for `gcloud auth login` (lands on `sdk.cloud.google/authcode.html`)

## Opera CDP quirks — IMPORTANT
Opera's CDP implementation differs from Chrome:

| Endpoint | Chrome | Opera |
|----------|--------|-------|
| `/json/new` | ✓ opens new tab | ✗ returns 405 |
| `/json/close/{id}` | ✓ closes tab | ✗ returns 405 |
| `Target.createTarget` (WS) | ✓ | ✓ use this |
| `Target.closeTarget` (WS) | ✓ | ✓ use this |

**Always use CDP WebSocket commands (`Target.createTarget`, `Target.closeTarget`) — never the HTTP `/json/new` or `/json/close` endpoints.**

To use `Target.createTarget`, you need an existing page tab's WebSocket URL as entry point. Get one from `/json/list`.

## Architecture
- `get_tabs()` — HTTP GET `/json/list`, returns all open tabs
- `open_tab(url)` — uses `Target.createTarget` via existing tab's WS
- `close_tab(tab_id)` — uses `Target.closeTarget` via any other tab's WS
- `cdp_send(ws_url, method, params)` — opens fresh WS connection per command (avoids stale state)
- `wait_for_auth_code(tab_ws, fragment)` — polls `window.location.href` every second, auto-clicks Next/Continue buttons, extracts `4/...` code via regex from `document.body.innerText`
- `run_gcloud_auth(args)` — `subprocess.Popen` with line-by-line stdout reading to grab URL before gcloud blocks
- `submit_auth_code(proc, code)` — writes code to gcloud stdin, reads remaining output

## Known issues / edge cases
- If Opera is not running, gauth automatically starts it with `opera --remote-debugging-port=9222 --remote-allow-origins=*` and polls up to 20 seconds until CDP is available. No manual Opera startup needed.
- If SSO session in Opera is expired, `wait_for_auth_code` will time out after 90s. User must manually open a Google URL in Opera to re-establish SSO.
- The `4/...` code regex in `extract_code_from_page()` matches standard Google OAuth codes. If Google changes the format, this is the place to fix.
- `rich` output: always use `escape()` from `rich.markup` when printing gcloud output — it contains paths like `[/home/...]` that rich misinterprets as markup tags.

## Environment
- Opera is auto-started if not already running (`--remote-debugging-port=9222 --remote-allow-origins=*`). Port 9222 protected by iptables (blocks non-loopback access).
- gcloud config: `~/.config/gcloud/application_default_credentials.json`
- Quota project: `solvinity-ai-usage-workplace`
- Python 3.10+, dependencies: `websockets`, `rich`, `httpx`

## Testing
```bash
# Run directly
python gauth.py

# Or via symlink
gauth
```

Prerequisites: active Solvinity SSO session in Opera (Opera is auto-started if needed).

## Integration
gauth is called automatically once per day by the `vpn` bash function (defined in `~/bashrc_programs/.bash_vpn`). The bash wrapper checks a daily marker at `~/.local/state/gauth/last_run` and skips re-running gauth if it already ran today. The marker file is managed by the bash wrapper, not by gauth itself.
