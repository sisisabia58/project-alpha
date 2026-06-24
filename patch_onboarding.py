"""
Patch: replace the rigid onboarding steps in browser_register.py with
a state machine that handles whatever Duolingo screens appear.
"""
import re
import pathlib

TARGET = pathlib.Path(r"c:\Users\wisnu\ZCodeProject\any-auto-register\platforms\duolingo\browser_register.py")
src = TARGET.read_text(encoding="utf-8")

# ── Locate the region to replace ──────────────────────────────────────────
# Start marker: right after the GET STARTED click block
START_MARKER = '        page.wait_for_timeout(2000)\n\n        # \u2500\u2500 Step 3: Language card selection'
# End marker: end of the old age-form block
END_MARKER   = '        self.log("Age continue clicked.")\n'

start_idx = src.find(START_MARKER)
end_idx   = src.find(END_MARKER)

if start_idx == -1:
    # Try CRLF variant
    START_MARKER = START_MARKER.replace('\n', '\r\n')
    start_idx = src.find(START_MARKER)

if end_idx == -1:
    END_MARKER = END_MARKER.replace('\n', '\r\n')
    end_idx   = src.find(END_MARKER)
    if end_idx != -1:
        end_idx += len(END_MARKER)
else:
    end_idx += len(END_MARKER)

print(f"start_idx={start_idx}, end_idx={end_idx}")
assert start_idx != -1, "START_MARKER not found"
assert end_idx   != -1, "END_MARKER not found"
assert end_idx > start_idx

NEW_BLOCK = r"""        page.wait_for_timeout(2000)

        self.log("Advancing through onboarding (state machine)...")
        _close_clicked = False
        for _sn in range(20):
            page.wait_for_timeout(1200)

            # Terminal: age form
            if page.locator('[data-test="age-input"]').is_visible():
                self.log(f"[{_sn}] Reached age form")
                break
            # Terminal: registration form (rare)
            if page.locator('[data-test="full-name-input"]').is_visible():
                self.log(f"[{_sn}] Already at registration form")
                break

            _acted = False

            # P1: BUAT PROFIL (creates jwt_token)
            if not _acted:
                _bp = page.locator('[data-test="create-profile-juicy"]').first
                if _bp.is_visible():
                    self.log(f"[{_sn}] BUAT PROFIL -> click")
                    _bp.click()
                    page.wait_for_timeout(1500)
                    _acted = True

            # P2: Language card
            if not _acted:
                _lc = page.locator('[data-test*="language-card"]').first
                if _lc.is_visible():
                    self.log(f"[{_sn}] Language card -> click")
                    _lc.click()
                    page.wait_for_timeout(1500)
                    _acted = True

            # P3: Funboarding continue
            if not _acted:
                _fc = page.locator('[data-test="funboarding-continue-button"]').first
                if _fc.is_visible() and _fc.is_enabled():
                    self.log(f"[{_sn}] Funboarding continue -> click")
                    _fc.click()
                    page.wait_for_timeout(1500)
                    _acted = True
                elif _fc.is_visible() and not _fc.is_enabled():
                    # Disabled continue: need to select a goal/reason card first
                    for _opt_sel in [
                        '[data-test*="reason"]', '[data-test*="goal"]',
                        '[data-test*="motivation"]', '[data-test*="option"]',
                    ]:
                        _opt = page.locator(_opt_sel).first
                        if _opt.is_visible():
                            self.log(f"[{_sn}] Option card (enables continue) -> click")
                            _opt.click()
                            page.wait_for_timeout(800)
                            _acted = True
                            break

            # P4: Close button (only once)
            if not _acted and not _close_clicked:
                _cl = page.locator('[data-test="close-button"]').first
                if _cl.is_visible():
                    self.log(f"[{_sn}] Close modal -> click")
                    _cl.click()
                    page.wait_for_timeout(1500)
                    _close_clicked = True
                    _acted = True

            # P5: Generic continue/next by button text
            if not _acted:
                for _txt in ["lanjutkan", "continue", "next", "selanjutnya", "berikutnya"]:
                    _gb = page.get_by_role("button", name=re.compile(_txt, re.I)).first
                    if _gb.is_visible() and _gb.is_enabled():
                        self.log(f"[{_sn}] '{_txt}' button -> click")
                        _gb.click()
                        page.wait_for_timeout(1500)
                        _acted = True
                        break

            if not _acted:
                try:
                    _vis = [b.inner_text()[:30] for b in page.locator("button").all()
                            if b.is_visible()]
                    self.log(f"[{_sn}] Stuck. Buttons: {_vis} | URL: {page.url}")
                except Exception:
                    pass
        else:
            raise RuntimeError("Could not reach age form after 20 onboarding steps")

        # Log JWT status
        try:
            _jwt_c = {c["name"]: c["value"][:40] for c in context.cookies()
                      if c["name"] in ("jwt_token", "duo_jwt", "logged_out_uuid")}
            self.log(f"Session cookies at age form: {_jwt_c}")
        except Exception:
            pass

        # Age form
        self.log("Filling age form...")
        _age = page.locator('[data-test="age-input"]')
        _age_val = str(random.randint(22, 35))
        _age.fill(_age_val)
        self.log(f"Age: {_age_val}")

        _cont = page.locator('[data-test="continue-button"]')
        for _ in range(20):
            if _cont.is_enabled():
                break
            page.wait_for_timeout(300)
        _cont.click(timeout=8000)
        self.log("Age continue clicked.")
"""

patched = src[:start_idx] + NEW_BLOCK + src[end_idx:]
TARGET.write_text(patched, encoding="utf-8")
print("Patch applied successfully.")
print(f"Lines in patched file: {patched.count(chr(10))}")
