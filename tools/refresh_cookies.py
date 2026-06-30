"""
Cookie grabber — you log in manually, the script captures cookies the
instant the auth_token appears.

Usage:
  python tools\\refresh_cookies.py qojjox         # refresh an existing account
  python tools\\refresh_cookies.py --new my_label # add a brand-new account
  python tools\\refresh_cookies.py --list         # show all accounts
  python tools\\refresh_cookies.py --all          # refresh every account in turn

Flow:
  1. A Chromium window opens at https://x.com/login. (Persistent profile
     per label, so X remembers the device across runs.)
  2. You log in by hand. Solve any 2FA / CAPTCHA / phone / email check
     that X throws.
  3. As soon as auth_token + ct0 cookies exist, the script:
        - reads ALL x.com cookies from the browser
        - writes them into accounts.json (cookie + csrf)
        - drops a revive signal so the running watch process swaps to the
          new cookies within ~5 seconds
        - closes the browser

The bearer token is the same for all accounts on Twitter's web client,
so for --new it is copied from an existing entry. If accounts.json is empty,
you must paste a bearer once when prompted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_PATH = ROOT / "accounts.json"
REVIVE_FILE = ROOT / "data" / "revive_accounts.txt"
LOGIN_URL = "https://x.com/i/flow/login"

# Max time to wait for the human to finish login
HUMAN_TIMEOUT_S = 900.0
# How often to poll cookies while the browser is open
POLL_INTERVAL_S = 1.0


# ── accounts.json helpers ────────────────────────────────────────────────────

def _load_accounts() -> list[dict]:
    if not ACCOUNTS_PATH.exists():
        return []
    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _save_accounts(accounts: list[dict]) -> None:
    tmp = ACCOUNTS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)
    os.replace(tmp, ACCOUNTS_PATH)


def _signal_revive(label: str) -> None:
    try:
        REVIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REVIVE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{label}\n")
        print(f"  wrote revive signal -> {REVIVE_FILE}")
    except Exception as e:
        print(f"  warning: could not write revive signal: {e}", file=sys.stderr)


# ── cookie helpers ───────────────────────────────────────────────────────────

def _cookies_to_header(cookies: list[dict]) -> str:
    """Convert Playwright's cookie list to the 'a=1; b=2' header string accounts.json wants."""
    twitter = [
        c for c in cookies
        if c.get("domain", "").endswith("x.com") or c.get("domain", "").endswith("twitter.com")
    ]
    parts = []
    for c in twitter:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _extract_named(cookies: list[dict], name: str) -> str:
    for c in cookies:
        if c.get("name") == name:
            return c.get("value", "") or ""
    return ""


def _twid_from_cookies(cookies: list[dict]) -> str:
    raw = _extract_named(cookies, "twid")
    if not raw:
        return ""
    val = urllib.parse.unquote(raw)
    return val[2:] if val.startswith("u=") else ""


def _twid_from_header(cookie_header: str) -> str:
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("twid="):
            v = urllib.parse.unquote(part.split("=", 1)[1])
            if v.startswith("u="):
                return v[2:]
    return ""


# ── browser session ──────────────────────────────────────────────────────────

def _open_browser_and_wait(label: str, expected_uid: str | None) -> dict | None:
    """
    Opens a Chromium window with a per-label persistent profile, navigates to
    x.com/login, waits until auth_token appears. Returns {cookie, csrf, twid}
    or None on timeout / cancel.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed.", file=sys.stderr)
        print("  pip install playwright && playwright install chromium", file=sys.stderr)
        return None

    profile_dir = ROOT / "data" / "chrome_profiles" / label
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Cookie grabber for '{label}' ===")
    print(f"  profile: {profile_dir}")
    print(f"  opening x.com/login — log in manually in the browser.")
    print(f"  you have {int(HUMAN_TIMEOUT_S/60)} minutes.")
    if expected_uid:
        print(f"  expected user_id: {expected_uid}")
    print()

    with sync_playwright() as pw:
        # Try installed Chrome first (better fingerprint), fall back to bundled.
        kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        try:
            ctx = pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception:
            ctx = pw.chromium.launch_persistent_context(**kwargs)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  navigation warning: {e}")

        deadline = time.time() + HUMAN_TIMEOUT_S
        last_url = ""
        result: dict | None = None
        try:
            while time.time() < deadline:
                # Check if the browser window was closed by the user
                try:
                    if page.is_closed():
                        print("  [info] browser closed manually before login completed.")
                        break
                except Exception:
                    break

                try:
                    cookies = ctx.cookies()
                except Exception:
                    cookies = []

                auth_token = _extract_named(cookies, "auth_token")
                ct0 = _extract_named(cookies, "ct0")

                # Progress hint: show URL changes
                try:
                    cur = page.url
                    if cur and cur != last_url:
                        print(f"  [page] {cur[:100]}")
                        last_url = cur
                except Exception:
                    pass

                if auth_token and ct0:
                    cookie_header = _cookies_to_header(cookies)
                    twid = _twid_from_cookies(cookies)
                    print(f"  ✓ cookies captured  (auth_token={auth_token[:10]}...  twid={twid})")

                    if expected_uid and twid and twid != expected_uid:
                        print(f"  ! captured twid={twid} but expected {expected_uid}")
                        ok = input("  save anyway? [y/N] ").strip().lower()
                        if ok != "y":
                            break

                    result = {"cookie": cookie_header, "csrf": ct0, "twid": twid}
                    break

                time.sleep(POLL_INTERVAL_S)
            else:
                print(f"  TIMEOUT after {int(HUMAN_TIMEOUT_S/60)} minutes")
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    return result


# ── CLI commands ─────────────────────────────────────────────────────────────

def cmd_refresh(label: str) -> int:
    accounts = _load_accounts()
    target = next((a for a in accounts if a.get("label") == label), None)
    if target is None:
        print(f"ERROR: no account '{label}' in {ACCOUNTS_PATH}", file=sys.stderr)
        print(f"Existing: {[a.get('label') for a in accounts]}", file=sys.stderr)
        print(f"To add it: python tools/refresh_cookies.py --new {label}", file=sys.stderr)
        return 1

    expected_uid = _twid_from_header(target.get("cookie", "")) or None
    new = _open_browser_and_wait(label=label, expected_uid=expected_uid)
    if new is None:
        return 1

    target["cookie"] = new["cookie"]
    target["csrf"] = new["csrf"]
    _save_accounts(accounts)
    print(f"  updated '{label}' in accounts.json")
    _signal_revive(label)
    return 0


def cmd_new(label: str) -> int:
    accounts = _load_accounts()
    if any(a.get("label") == label for a in accounts):
        print(f"ERROR: label '{label}' already exists. Use refresh instead.", file=sys.stderr)
        return 1

    # Reuse bearer from existing entry — it's the same web-client bearer for everyone.
    bearer = ""
    if accounts:
        bearer = accounts[0].get("bearer", "")
    if not bearer:
        print("No existing account to copy bearer from.")
        print("Paste the Twitter web client bearer token (starts with 'Bearer AAAA...'):")
        try:
            bearer = input("> ").strip()
        except KeyboardInterrupt:
            return 1
        if not bearer.lower().startswith("bearer "):
            bearer = f"Bearer {bearer}"

    new = _open_browser_and_wait(label=label, expected_uid=None)
    if new is None:
        return 1

    accounts.append({
        "label": label,
        "bearer": bearer,
        "cookie": new["cookie"],
        "csrf": new["csrf"],
    })
    _save_accounts(accounts)
    print(f"  added '{label}' to accounts.json")
    _signal_revive(label)
    return 0


def cmd_list() -> int:
    accounts = _load_accounts()
    if not accounts:
        print("(no accounts in accounts.json)")
        return 0
    print(f"{'LABEL':<25} {'USER_ID':<22} {'COOKIE_LEN':>10}  {'AUTH_TOKEN':<14}")
    print("-" * 75)
    for a in accounts:
        uid = _twid_from_header(a.get("cookie", ""))
        cl = len(a.get("cookie", ""))
        # show first chars of auth_token as a quick "is it filled" check
        auth = ""
        for part in a.get("cookie", "").split(";"):
            part = part.strip()
            if part.startswith("auth_token="):
                auth = part.split("=", 1)[1][:12] + "..."
                break
        print(f"{a.get('label',''):<25} {uid:<22} {cl:>10}  {auth:<14}")
    return 0


def cmd_all() -> int:
    accounts = _load_accounts()
    rc_total = 0
    for a in accounts:
        rc = cmd_refresh(a.get("label", ""))
        if rc != 0:
            rc_total = rc
    return rc_total


def main() -> int:
    p = argparse.ArgumentParser(
        description="Open a browser; you log in; we grab the cookies."
    )
    p.add_argument("label", nargs="?", help="Account label to refresh.")
    p.add_argument("--new", metavar="LABEL", help="Add a NEW account with this label.")
    p.add_argument("--all", action="store_true", help="Refresh every account in turn.")
    p.add_argument("--list", action="store_true", help="List existing accounts.")
    args = p.parse_args()

    if args.list:
        return cmd_list()
    if args.new:
        return cmd_new(args.new)
    if args.all:
        return cmd_all()
    if not args.label:
        p.print_help()
        return 1
    return cmd_refresh(args.label)


if __name__ == "__main__":
    sys.exit(main())
