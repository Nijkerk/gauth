# gauth

Automates the daily `gcloud auth application-default login` flow using Opera via Chrome DevTools Protocol (CDP). Designed for Solvinity engineers using **Workforce Identity Federation via Entra ID OIDC**, where the ADC token expires every 24 hours and SSO runs through the browser.

## What it does

1. Starts `gcloud auth application-default login --no-launch-browser`
2. Opens the authorization URL in a new Opera tab via CDP
3. Solvinity SSO completes automatically (uses your active Opera session)
4. Extracts the verification code from the redirect page
5. Submits the code back to gcloud
6. Runs `gcloud auth application-default set-quota-project solvinity-ai-usage-workplace`

Optionally (with `--login`): repeats steps 1–5 for `gcloud auth login` (needed for some gcloud CLI operations).

## Prerequisites

- **Opera** running with `--remote-debugging-port=9222`
  ```
  opera --remote-debugging-port=9222
  ```
- **Active Solvinity SSO session** in Opera (log in once manually if needed)
- **gcloud CLI** installed and on `PATH`
- Python 3.10+

## Install

```bash
pip install -r requirements.txt
```

## One-time setup

Before using `gauth` for the first time, run the setup to configure Workforce Identity Federation:

```bash
python gauth.py --setup
```

This runs:
1. `gcloud iam workforce-pools create-login-config` — generates `~/login-config.json`
2. `gcloud config set auth/login_config_file ~/login-config.json` — points gcloud at it

You only need to do this once per machine.

## Usage

### Daily auth (ADC only — the default)

```bash
python gauth.py
```

### Daily auth + gcloud login (for full gcloud CLI access)

```bash
python gauth.py --login
```

### First-time setup + auth in one go

```bash
python gauth.py --setup
```

### Optional: install as `gauth` command

```bash
# Add to ~/.local/bin or any directory on your PATH:
ln -s /home/erik/Programming/python/gauth/gauth.py ~/.local/bin/gauth
chmod +x ~/.local/bin/gauth
```

Then just run `gauth` from anywhere.

## Flags

| Flag | Description |
|------|-------------|
| *(none)* | Run ADC flow only (default, covers most use cases) |
| `--login` | Also run `gcloud auth login` after ADC |
| `--setup` | Run one-time workforce pool initialization before auth |

## Error messages

| Message | Fix |
|---------|-----|
| `Opera is not running with --remote-debugging-port=9222` | Start Opera with the flag above |
| `SSO session may have expired` | Manually open a Google auth URL in Opera to re-establish the session |
| `Timed out after 90s` | SSO took too long or failed; check Opera's active session |
| `gcloud not found in PATH` | Install gcloud CLI |
