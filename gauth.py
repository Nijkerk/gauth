#!/usr/bin/env python3
"""
gauth - Automate gcloud ADC + login auth via Opera CDP (Solvinity SSO).
"""

import asyncio
import json
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import quote

import httpx
import websockets
from rich.console import Console
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
    """Open a new tab with the given URL, return its websocket debugger URL."""
    new_tab_url = f"http://{CDP_HOST}:{CDP_PORT}/json/new?{quote(url, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(new_tab_url)
            resp.raise_for_status()
            tab = resp.json()
            return tab.get("webSocketDebuggerUrl")
    except Exception as e:
        raise RuntimeError(f"Failed to open new tab: {e}") from e


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
    close_url = f"http://{CDP_HOST}:{CDP_PORT}/json/close/{tab_id}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.get(close_url)
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
            console.print(f"  [dim]{output.strip()}[/dim]")
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
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    total_steps = 7  # visual steps (each flow uses 3 sub-steps + misc)
    console.print()
    console.rule("[bold blue]gauth[/bold blue]")
    console.print()

    # Step 1: Check Opera
    step(1, 7, "Checking Opera on port 9222")
    tabs = await get_tabs()
    if not tabs:
        fail("")
        console.print(
            "[red]Opera is not running with --remote-debugging-port=9222.[/red]\n"
            "Start Opera with:\n"
            "  [bold]opera --remote-debugging-port=9222[/bold]"
        )
        return 1
    ok(f"{len(tabs)} tab(s) open")

    # Step 2: Check gcloud
    step(2, 7, "Checking gcloud")
    if not check_gcloud():
        fail("")
        console.print("[red]gcloud not found in PATH.[/red]")
        return 1
    ok()

    # Steps 3–4: ADC flow
    console.print()
    console.rule("[dim]Application Default Credentials[/dim]")
    adc_ok = await run_auth_flow(
        step_num=3,
        total_steps=7,
        label="ADC",
        gcloud_args=["auth", "application-default", "login"],
        expected_url_fragment=AUTH_CODE_URL_ADC,
    )
    if not adc_ok:
        return 1

    # Set quota project
    step(5, 7, "Setting ADC quota project")
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

    # Steps 6–7: gcloud auth login flow
    console.print()
    console.rule("[dim]gcloud auth login[/dim]")
    login_ok = await run_auth_flow(
        step_num=6,
        total_steps=7,
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
