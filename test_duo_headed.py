import sys
import io
import random
import string

# Force UTF-8 output so special characters don't crash on Windows cp1252 console
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Configure here ─────────────────────────────────────────────────────────
TEST_EMAIL    = "testduo_{}@qw.blaizesmp.net".format(
    "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
)
TEST_PASSWORD = "Bakso2025Super!"   # must be >=8 chars, 1 upper, 1 digit/symbol
REFERRAL_CODE = ""                  # optional — leave empty to skip redeem step
PROXY         = ""                  # optional — e.g. "http://user:pass@host:port"
# ───────────────────────────────────────────────────────────────────────────

sys.path.insert(0, ".")

from platforms.duolingo.browser_register import DuolingoBrowserRegister  # noqa: E402


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


print(f"\n{'='*60}")
print(f"  Duolingo HEADED Registration Test")
print(f"  Email    : {TEST_EMAIL}")
print(f"  Password : {TEST_PASSWORD}")
print(f"  Referral : {REFERRAL_CODE or '(none)'}")
print(f"  Proxy    : {PROXY or '(none)'}")
print(f"{'='*60}\n")
print("  Chromium will open — watch the browser window on your desktop.")
print("  Press Ctrl+C at any time to stop.\n")

worker = DuolingoBrowserRegister(
    headless=False,      # <-- HEADED: visible browser window
    proxy=PROXY or None,
    log_fn=log,
)

try:
    result = worker.register(
        email=TEST_EMAIL,
        password=TEST_PASSWORD,
        referral_code=REFERRAL_CODE or None,
    )
    print(f"\n{'='*60}")
    print(f"  SUCCESS!")
    print(f"  Email    : {result['email']}")
    print(f"  Status   : {result['status']}")
    if result.get("redemption"):
        print(f"  Redeem   : {result['redemption']}")
    print(f"{'='*60}\n")
except KeyboardInterrupt:
    print("\n  Stopped by user.")
except Exception as exc:
    print(f"\n{'='*60}")
    print(f"  FAILED: {exc}")
    print(f"{'='*60}\n")
    sys.exit(1)
