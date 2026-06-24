"""
Proxy connectivity test for 9proxy / niceproxy.io

Usage:
    python test_proxy.py "http://user:pass@niceproxy.io:17522"
    python test_proxy.py  (uses the proxy from env var PROXY_URL)
"""
import sys
import os
import urllib.parse
import urllib.request
import socket

PROXY_URL = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get("PROXY_URL", "")
)

if not PROXY_URL:
    print("Usage: python test_proxy.py \"http://user:pass@host:port\"")
    sys.exit(1)


def parse_proxy(proxy: str) -> dict:
    parsed = urllib.parse.urlparse(proxy)
    scheme   = parsed.scheme or "http"
    host     = parsed.hostname or ""
    port     = parsed.port
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    server   = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
    cfg: dict = {"server": server, "host": host, "port": port or 80}
    if username:
        cfg["username"] = username
    if password:
        cfg["password"] = password
    return cfg


def tcp_check(host, port, timeout=10):
    """Check if we can TCP-connect to the proxy server at all."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True, None
    except Exception as e:
        return False, str(e)


def http_check_via_proxy(proxy_url, target="http://ipinfo.io/json", timeout=20):
    """Use urllib (no browser) to check IP through proxy."""
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        })
        # Build opener with auth
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(
            None,
            f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            urllib.parse.unquote(parsed.username or ""),
            urllib.parse.unquote(parsed.password or ""),
        )
        auth_handler = urllib.request.ProxyBasicAuthHandler(password_mgr)
        opener = urllib.request.build_opener(proxy_handler, auth_handler)
        with opener.open(target, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, str(e)


def main():
    print("=" * 60)
    print("  Proxy Connectivity Test")
    print(f"  URL: {PROXY_URL}")
    print("=" * 60)

    cfg = parse_proxy(PROXY_URL)
    print(f"\nParsed:")
    print(f"  server   : {cfg['server']}")
    print(f"  username : {cfg.get('username', '(none)')}")
    print(f"  password : {'*' * len(cfg.get('password', ''))}")
    print(f"  host     : {cfg['host']}")
    print(f"  port     : {cfg['port']}")

    # Test 1: TCP connectivity
    print(f"\n[1/3] TCP connection to {cfg['host']}:{cfg['port']}...")
    ok, err = tcp_check(cfg["host"], cfg["port"])
    if ok:
        print("  OK - proxy server is reachable")
    else:
        print(f"  FAIL - cannot reach proxy: {err}")
        print("  Check: host/port correct? VPS can reach niceproxy.io?")
        return

    # Test 2: HTTP request through proxy (no browser)
    print("\n[2/3] HTTP request via urllib (no browser)...")
    status, body = http_check_via_proxy(PROXY_URL, "http://ipinfo.io/json")
    if status == 200:
        print(f"  OK - status {status}")
        try:
            import json
            data = json.loads(body)
            print(f"  Proxy IP  : {data.get('ip')}")
            print(f"  Country   : {data.get('country')}")
            print(f"  Org       : {data.get('org')}")
        except Exception:
            print(f"  Body: {body[:200]}")
    else:
        print(f"  FAIL - status={status}, response: {body[:300]}")
        print("  Possible causes:")
        print("  - Wrong username or password")
        print("  - Session ID (ssid) expired")
        print("  - IP not whitelisted on proxy dashboard")

    # Test 3: Browser through proxy
    print("\n[3/3] Browser (patchright) through proxy...")
    try:
        from patchright.sync_api import sync_playwright
        proxy_cfg = {
            "server": cfg["server"],
        }
        if cfg.get("username"):
            proxy_cfg["username"] = cfg["username"]
        if cfg.get("password"):
            proxy_cfg["password"] = cfg["password"]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy=proxy_cfg,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto("http://ipinfo.io/json", wait_until="domcontentloaded", timeout=30000)
                body = page.locator("body").inner_text()
                import json
                data = json.loads(body)
                print(f"  OK - Proxy IP: {data.get('ip')} ({data.get('country')})")
                print(f"  Org: {data.get('org')}")
            except Exception as e:
                print(f"  FAIL (browser): {e}")
            browser.close()
    except Exception as e:
        print(f"  FAIL (launch): {e}")

    print("\n" + "=" * 60)
    print("  Done")
    print("=" * 60)


if __name__ == "__main__":
    main()
