# gauth

Automates the daily `gcloud auth application-default login` + `gcloud auth login` flow using Opera via Chrome DevTools Protocol (CDP). Designed for Solvinity engineers where both tokens expire every 24 hours and SSO (Entra ID) runs through the browser.

## What it does

1. Starts `gcloud auth application-default login --no-launch-browser`
2. Opens the authorization URL in a new Opera tab via CDP
3. Solvinity SSO completes automatically (uses your active Opera session)
4. Extracts the verification code from the redirect page
5. Submits the code back to gcloud
6. Runs `gcloud auth application-default set-quota-project solvinity-ai-usage-workplace`
7. Repeats steps 1–5 for `gcloud auth login`

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

## Usage

```bash
python gauth.py
```

### Optional: install as `gauth` command

```bash
# Add to ~/.local/bin or any directory on your PATH:
ln -s /home/erik/Programming/python/gauth/gauth.py ~/.local/bin/gauth
chmod +x ~/.local/bin/gauth
```

Then just run `gauth` from anywhere.

## Error messages

| Message | Fix |
|---------|-----|
| `Opera is not running with --remote-debugging-port=9222` | Start Opera with the flag above |
| `SSO session may have expired` | Manually open a Google auth URL in Opera to re-establish the session |
| `Timed out after 90s` | SSO took too long or failed; check Opera's active session |
| `gcloud not found in PATH` | Install gcloud CLI |
