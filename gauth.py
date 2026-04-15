#!/usr/bin/env python3
"""
gauth - Automate gcloud ADC auth via Opera CDP (Solvinity Workforce Identity Federation).
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from typing import Optional
import httpx
import websockets
from rich.console import Console
from rich.markup import escape
from rich.text import Text

console = Console()

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
AUTH_CODE_URL_ADC = "sdk.cloud.google/applicationdefaultauthcode.html"
AUTH_CODE_URL_LOGIN = "sdk.cloud.google/authcode.html"
AUTH_TIMEOUT = 90  # seconds to wait for browser redirect
POLL_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------


async def get_tabs() -> list[dict]:
    url = f"http://{CDP_HOST}:{CDP_PORT}/json/list"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return []
    except Exception:
        return []


async def open_tab(url: str) -> Optional[str]:
    """Open a new tab with url via Target.createTarget CDP command."""
    tabs = await get_tabs()
    page_tabs = [t for t in tabs if t.get("type") == "page"]
    if not page_tabs:
        raise RuntimeError("No page tabs found in Opera to use as CDP entry point")

    any_ws = page_tabs[0]["webSocketDebuggerUrl"]
    result = await cdp_send(any_ws, "Target.createTarget", {"url": url})

    target_id = result.get("result", {}).get("targetId")
    if not target_id:
        raise RuntimeError(f"Target.createTarget did not return targetId: {result}")

    # Find the new tab's WS URL
    await asyncio.sleep(0.5)
    new_tabs = await get_tabs()
    for tab in new_tabs:
        if tab.get("id") == target_id:
            return tab.get("webSocketDebuggerUrl")

    raise RuntimeError(f"Could not find new tab with targetId {target_id}")


async def cdp_send(ws_url: str, method: str, params: dict) -> dict:
    """Send a single CDP command and return the result."""
    async with websockets.connect(
        ws_url,
        close_timeout=5,
        open_timeout=10,
        max_size=2 * 1024 * 1024,
    ) as ws:
        msg = {"id": 1, "method": method, "params": params}
        await ws.send(json.dumps(msg))
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CDP command timed out")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            resp = json.loads(raw)
            if resp.get("id") == 1:
                return resp


async def click_next_if_present(ws_url: str) -> bool:
    """Click the Next/Continue button if present on the page. Returns True if clicked."""
    result = await cdp_send(
        ws_url,
        "Runtime.evaluate",
        {
            "expression": """
                (function() {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const next = buttons.find(b =>
                        b.innerText.trim().toLowerCase() === 'next' ||
                        b.innerText.trim().toLowerCase() === 'continue'
                    );
                    if (next) { next.click(); return true; }
                    return false;
                })()
            """,
            "returnByValue": True,
        },
    )
    return result.get("result", {}).get("result", {}).get("value", False)


async def get_tab_url(ws_url: str) -> str:
    """Return the current URL of a tab via CDP."""
    result = await cdp_send(
        ws_url,
        "Runtime.evaluate",
        {
            "expression": "window.location.href",
            "returnByValue": True,
        },
    )
    return result.get("result", {}).get("result", {}).get("value", "")


async def get_page_text(ws_url: str) -> str:
    """Return document.body.innerText of a tab."""
    result = await cdp_send(
        ws_url,
        "Runtime.evaluate",
        {
            "expression": "document.body ? document.body.innerText : ''",
            "returnByValue": True,
        },
    )
    return result.get("result", {}).get("result", {}).get("value", "")


async def close_tab(tab_id: str) -> None:
    tabs = await get_tabs()
    page_tabs = [t for t in tabs if t.get("type") == "page" and t.get("id") != tab_id]
    if not page_tabs:
        return
    any_ws = page_tabs[0]["webSocketDebuggerUrl"]
    try:
        await cdp_send(any_ws, "Target.closeTarget", {"targetId": tab_id})
    except Exception:
        pass


async def find_tab_ws_by_id(tab_id: str) -> Optional[str]:
    tabs = await get_tabs()
    for tab in tabs:
        if tab.get("id") == tab_id:
            return tab.get("webSocketDebuggerUrl")
    return None


# ---------------------------------------------------------------------------
# gcloud subprocess helpers
# ---------------------------------------------------------------------------


def check_gcloud() -> bool:
    try:
        result = subprocess.run(
            ["gcloud", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run_gcloud_auth(args: list[str]) -> tuple[subprocess.Popen, str]:
    """
    Start gcloud auth with --no-launch-browser.
    Reads stdout until the authorization URL is found.
    Returns (process, url).
    """
    proc = subprocess.Popen(
        ["gcloud"] + args + ["--no-launch-browser"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url = None
    output_lines = []
    while True:
        line = proc.stdout.readline()
        if not line:
            # Process ended without giving us a URL
            break
        output_lines.append(line.rstrip())
        if "https://" in line and "authorize" in line.lower():
            # Extract the URL — it's typically the whole line or part of it
            for word in line.split():
                if word.startswith("https://"):
                    url = word.strip()
                    break
            if url:
                break

    if url is None:
        proc.kill()
        output = "\n".join(output_lines)
        raise RuntimeError(
            f"Could not find authorization URL in gcloud output.\n{output}"
        )

    return proc, url


def submit_auth_code(proc: subprocess.Popen, code: str) -> str:
    """Write code to gcloud stdin, wait for completion, return output."""
    proc.stdin.write(code + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    remaining_output = proc.stdout.read()
    proc.wait(timeout=30)

    return remaining_output


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------


async def wait_for_auth_code(tab_ws: str, expected_url_fragment: str) -> str:
    """
    Poll the tab until its URL matches expected_url_fragment,
    then extract the verification code from the page body.
    """
    deadline = time.monotonic() + AUTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            # Auto-click Next/Continue if present
            try:
                await click_next_if_present(tab_ws)
            except Exception:
                pass
            current_url = await get_tab_url(tab_ws)
            if expected_url_fragment in current_url:
                # Page has the auth code — extract it
                text = await get_page_text(tab_ws)
                code = extract_code_from_page(text)
                if code:
                    return code
                # Page loaded but code not yet visible — wait a moment
                await asyncio.sleep(0.5)
                text = await get_page_text(tab_ws)
                code = extract_code_from_page(text)
                if code:
                    return code
                raise RuntimeError(
                    f"Reached auth page but could not extract code.\n"
                    f"Page content:\n{text[:500]}"
                )
        except (websockets.exceptions.ConnectionClosed, OSError):
            # Tab may have navigated; re-fetch its WS URL
            pass
        await asyncio.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"Timed out after {AUTH_TIMEOUT}s waiting for browser redirect to "
        f"{expected_url_fragment}.\n"
        "SSO session may have expired. Please log in to Opera manually first."
    )


def extract_code_from_page(text: str) -> Optional[str]:
    """
    Extract the verification code from the Google auth confirmation page.
    The page typically shows a 4/xx-... style code or a token string.
    We look for the code in known patterns.
    """
    import re

    # Pattern 1: "Copy this code..." followed by the code on the next line
    # or inline: code is typically a long alphanumeric string starting with 4/
    match = re.search(r"\b(4/[A-Za-z0-9_\-]+)\b", text)
    if match:
        return match.group(1)

    # Pattern 2: Fallback — look for lines that look like standalone tokens
    # (long base64-ish strings, no spaces)
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 20 and " " not in line and "/" in line:
            return line

    return None


def step(n: int, total: int, label: str) -> None:
    console.print(f"[bold cyan][{n}/{total}][/bold cyan] {label}...", end=" ")


def ok(msg: str = "") -> None:
    mark = Text("✓", style="bold green")
    if msg:
        console.print(mark, Text(msg, style="green"))
    else:
        console.print(mark)


def fail(msg: str) -> None:
    console.print(Text("✗", style="bold red"), Text(msg, style="red"))


# ---------------------------------------------------------------------------
# Single auth flow (ADC or login)
# ---------------------------------------------------------------------------


async def run_auth_flow(
    step_num: int,
    total_steps: int,
    label: str,
    gcloud_args: list[str],
    expected_url_fragment: str,
) -> bool:
    """
    Run one complete gcloud auth flow:
      1. Start gcloud, get URL
      2. Open URL in Opera tab
      3. Wait for redirect to auth code page
      4. Extract code, submit to gcloud
    Returns True on success.
    """
    step(step_num, total_steps, f"{label} — starting gcloud")
    try:
        proc, auth_url = run_gcloud_auth(gcloud_args)
        ok("URL obtained")
    except RuntimeError as e:
        fail(str(e))
        return False

    step(step_num + 1, total_steps, f"{label} — opening URL in Opera")
    try:
        tab_ws = await open_tab(auth_url)
        if not tab_ws:
            fail("Failed to create tab — could not get WebSocket URL")
            proc.kill()
            return False

        # Resolve full tab info to get ID for cleanup
        tabs_after = await get_tabs()
        tab_id = None
        for tab in tabs_after:
            if tab.get("webSocketDebuggerUrl") == tab_ws:
                tab_id = tab.get("id")
                break

        ok(f"Waiting for auth code (up to {AUTH_TIMEOUT}s)")
    except Exception as e:
        fail(str(e))
        proc.kill()
        return False

    step(step_num + 1, total_steps, f"{label} — waiting for browser redirect")
    try:
        code = await wait_for_auth_code(tab_ws, expected_url_fragment)
        ok("Code received")
    except TimeoutError as e:
        fail(str(e))
        proc.kill()
        if tab_id:
            await close_tab(tab_id)
        return False
    except RuntimeError as e:
        fail(str(e))
        proc.kill()
        if tab_id:
            await close_tab(tab_id)
        return False

    step(step_num + 1, total_steps, f"{label} — submitting code to gcloud")
    try:
        output = submit_auth_code(proc, code)
        if tab_id:
            await close_tab(tab_id)
        ok()
        if output.strip():
            console.print(f"  [dim]{escape(output.strip())}[/dim]")
    except subprocess.TimeoutExpired:
        fail("gcloud did not complete within 30s")
        proc.kill()
        if tab_id:
            await close_tab(tab_id)
        return False
    except Exception as e:
        fail(str(e))
        proc.kill()
        if tab_id:
            await close_tab(tab_id)
        return False

    return True


# ---------------------------------------------------------------------------
# One-time setup
# ---------------------------------------------------------------------------


def run_setup(total_steps: int, step_offset: int) -> bool:
    """
    Run one-time Workforce Identity Federation initialization:
      1. Generate login config from workforce pool
      2. Configure gcloud to use it
    Returns True on success.
    """
    login_config_path = os.path.join(os.path.expanduser("~"), "login-config.json")
    workforce_pool = (
        "locations/global/workforcePools/solvinity-entra-id"
        "/providers/solvinity-entra-id-oidc"
    )

    step(step_offset, total_steps, "Setup — generating workforce pool login config")
    result = subprocess.run(
        [
            "gcloud",
            "iam",
            "workforce-pools",
            "create-login-config",
            workforce_pool,
            f"--output-file={login_config_path}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or "unknown error")
        return False
    ok(f"Saved to {login_config_path}")

    step(step_offset + 1, total_steps, "Setup — configuring gcloud login_config_file")
    result = subprocess.run(
        [
            "gcloud",
            "config",
            "set",
            "auth/login_config_file",
            login_config_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or "unknown error")
        return False
    ok()

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Automate gcloud Application Default Credentials (ADC) auth via Opera CDP. "
            "Designed for Solvinity engineers using Workforce Identity Federation (Entra ID OIDC). "
            "Requires Opera running with --remote-debugging-port=9222 and an active SSO session."
        )
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help=(
            "Run one-time initialization: generate the workforce pool login config "
            "and configure gcloud to use it. Run this once before using gauth regularly."
        ),
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "Also run gcloud auth login after ADC. Needed for some gcloud CLI operations "
            "that require a user account (not just ADC). Off by default."
        ),
    )
    args = parser.parse_args()

    # Calculate total steps based on flags
    # Base: 2 (opera check, gcloud check) + 3 (ADC flow) + 1 (quota project) = 6
    # --setup adds 2 steps
    # --login adds 3 steps
    setup_steps = 2 if args.setup else 0
    login_steps = 3 if args.login else 0
    total_steps = 2 + setup_steps + 3 + 1 + login_steps

    console.print()
    console.rule("[bold blue]gauth[/bold blue]")
    console.print()

    current_step = 1

    # Step 1: Check Opera (start if not running)
    step(current_step, total_steps, "Checking Opera on port 9222")
    tabs = await get_tabs()
    if not tabs:
        console.print("[yellow]not running, starting...[/yellow]")
        subprocess.Popen(
            ["opera", "--remote-debugging-port=9222", "--remote-allow-origins=*"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Poll until CDP is available
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            tabs = await get_tabs()
            if tabs:
                break
        if not tabs:
            fail("Opera did not start within 20s")
            return 1
    ok(f"{len(tabs)} tab(s) open")
    current_step += 1

    # Step 2: Check gcloud
    step(current_step, total_steps, "Checking gcloud")
    if not check_gcloud():
        fail("")
        console.print("[red]gcloud not found in PATH.[/red]")
        return 1
    ok()
    current_step += 1

    # Optional: one-time setup
    if args.setup:
        console.print()
        console.rule("[dim]One-time Setup[/dim]")
        if not run_setup(total_steps, current_step):
            return 1
        current_step += 2

    # ADC flow (3 sub-steps)
    console.print()
    console.rule("[dim]Application Default Credentials[/dim]")
    adc_ok = await run_auth_flow(
        step_num=current_step,
        total_steps=total_steps,
        label="ADC",
        gcloud_args=["auth", "application-default", "login"],
        expected_url_fragment=AUTH_CODE_URL_ADC,
    )
    if not adc_ok:
        return 1
    current_step += 3

    # Set quota project
    step(current_step, total_steps, "Setting ADC quota project")
    result = subprocess.run(
        [
            "gcloud",
            "auth",
            "application-default",
            "set-quota-project",
            "solvinity-ai-usage-workplace",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or "unknown error")
        return 1
    ok()
    current_step += 1

    # Optional: gcloud auth login flow (3 sub-steps)
    if args.login:
        console.print()
        console.rule("[dim]gcloud auth login[/dim]")
        login_ok = await run_auth_flow(
            step_num=current_step,
            total_steps=total_steps,
            label="Login",
            gcloud_args=["auth", "login"],
            expected_url_fragment=AUTH_CODE_URL_LOGIN,
        )
        if not login_ok:
            return 1

    console.print()
    console.rule("[bold green]Done. Claude is ready.[/bold green]")
    console.print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
