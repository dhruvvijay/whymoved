"""Drives the browser app in headless Chromium against the real service code.
Run: PYTHONPATH=src python web_tests/e2e.py   (needs `pip install playwright` + a Chromium)
"""
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from stub_server import make_server  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="wm_shots_"))
PASSWORD = "Correct-horse-9"
passed = []


def check(name, cond=True):
    assert cond, name
    passed.append(name)
    print("ok -", name)


def main():
    srv, _ = make_server(0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium"
                                    if Path("/opt/pw-browsers/chromium").exists() else None)
        ctx = browser.new_context(viewport={"width": 1100, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.on("dialog", lambda d: d.accept())

        # ---- sign in screen, bad login, register
        page.goto(f"{base}/app/")
        expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
        page.fill("#email", "nobody@example.com")
        page.fill("#password", "wrong-password-1")
        page.click("button[type=submit]")
        expect(page.locator(".notice.error")).to_be_visible()
        check("bad login shows an error")
        page.click("text=Create an account")
        page.fill("#email", "dhruv@example.com")
        page.fill("#password", "short")
        page.click("button[type=submit]")
        expect(page.locator(".notice.error")).to_contain_text("password")
        check("weak password is refused with the server's message")
        page.fill("#password", PASSWORD)
        page.fill("#wsname", "Dhruv's team")
        page.click("button[type=submit]")
        expect(page.get_by_role("heading", name="Find where your numbers moved")).to_be_visible()
        check("register lands on the data page")

        # ---- sample data, analysis
        page.click("text=Try sample data")
        expect(page.locator("h1")).to_contain_text("Demo sales")
        expect(page.locator("text=Planted problem")).to_be_visible()
        check("sample data creates a dataset with detected roles")
        planted = page.locator(".notice.info").inner_text().split("(the answer): ")[1].split(": conversion")[0]
        page.click("text=Scan every number column")
        expect(page.locator("text=Where should I look first?")).to_be_visible()
        expect(page.locator("table", has_text="Largest segment").locator("tbody tr").first).to_be_visible(timeout=60000)
        check("scan ranks the number columns", page.locator("table", has_text="Largest segment").locator("tbody tr").count() >= 2)
        check("scan carries a no-cause caveat", "does not say why" in page.inner_text("main"))
        page.select_option("#metric", "conversion")
        page.click("text=Find where it moved")
        expect(page.locator(".verdict")).to_have_text("Clear driver found", timeout=30000)
        expect(page.locator("table tbody tr").first).to_contain_text(planted)
        check("planted driver is found and shown in the table")
        check("waterfall draws one bar per step", page.locator("svg.wf rect").count() == page.locator("table tbody tr").count() + 3)
        check("chart notes the zoomed axis", (page.locator("svg.wf").text_content() or "").count("does not start at zero") == 1)
        check("written summary is present", planted.split(" & ")[0].split("=")[1] in page.locator(".summary").inner_text())
        check("the 'not why' caveat is on the page", "not why" in page.inner_text("main"))
        page.fill("#notetext", "Private: ask finance about the discount code.")
        page.click("text=Add note")
        expect(page.locator(".note").first).to_contain_text("private")
        page.fill("#notetext", "Public: a promotion ran in that region.")
        page.check("#notepub")
        page.click("text=Add note")
        expect(page.locator(".note")).to_have_count(2)
        check("notes can be added, one private and one public", "on the share link" in page.locator(".note").nth(1).inner_text())
        with page.expect_download() as dl:
            page.click("text=Download report (.md)")
        report = Path(dl.value.path()).read_text(encoding="utf-8")
        check("report download has summary, caveat and only the public note",
              "## In words" in report and "not why" in report and "promotion ran" in report and "discount code" not in report)
        page.screenshot(path=str(OUT / "analysis_desktop.png"), full_page=True)

        # ---- share, open in a clean browser context, revoke
        page.click("text=Create share link")
        link = page.locator(".sharebox input").input_value()
        check("share link has the expected shape", "#/shared/" in link)
        anon = browser.new_context(viewport={"width": 1100, "height": 900}).new_page()
        anon.on("pageerror", lambda e: problems.append(str(e)))
        anon.goto(link)
        expect(anon.locator(".verdict")).to_have_text("Clear driver found")
        check("share page shows the result without signing in")
        shared_text = anon.inner_text("main")
        check("share page shows the public note, labelled, and hides the private one",
              "promotion ran" in shared_text and "Written by a person" in shared_text and "discount code" not in shared_text)
        check("share page has no share or delete controls", anon.locator("text=Revoke all links").count() == 0
              and anon.locator("text=Delete analysis").count() == 0)
        page.click("text=Revoke all links")
        expect(page.locator("text=Revoked 1 link")).to_be_visible()
        anon.reload()
        expect(anon.locator(".notice.error")).to_contain_text("does not exist")
        check("revoked link stops working")

        # ---- budget mode and a ratio-less additive metric
        page.click("text=Back to dataset")
        page.select_option("#mode", "budget")
        page.select_option("#metric", "revenue")
        page.fill("#currency", "Rs ")
        page.check("#indian")
        page.click("text=Find where it moved")
        expect(page.locator(".verdict")).to_be_visible(timeout=30000)
        check("budget analysis renders with Indian grouping and currency",
              "Rs " in page.inner_text("main") and "," in page.inner_text(".stats"))
        check("budget labels are used", "Budget" in page.inner_text(".stats"))

        # ---- upload: bad type, then a hostile-looking but valid CSV
        page.goto(f"{base}/app/#/")
        bad = OUT / "notes.txt"
        bad.write_text("hello")
        page.set_input_files("#file", str(bad))
        expect(page.locator(".notice.error")).to_be_visible()
        check("unsupported file type is refused with a message")
        evil = OUT / "<b>bold.csv"
        rows = ["date,region,orders"]
        for d in range(1, 29):
            for r in ("<img src=x onerror=window.__pwned=1>", "North"):
                rows.append(f"2026-02-{d:02d},{r},{100 + d}")
        evil.write_text("\n".join(rows))
        page.set_input_files("#file", str(evil))
        expect(page.locator("h1")).to_contain_text("<b>bold")
        check("dataset name is shown as text, not markup", "<b>bold" in page.locator("h1").inner_text())
        check("no markup injected from a file", page.evaluate("window.__pwned") is None
              and page.locator("main b").count() == 0)
        page.click("text=Find where it moved")
        expect(page.locator(".verdict")).to_be_visible(timeout=30000)
        check("hostile category values render as text", page.evaluate("window.__pwned") is None)

        # ---- isolation: a second user cannot open the first user's dataset
        did_url = page.url
        page.click("text=Back to dataset")
        owner_url = page.url
        other = browser.new_context().new_page()
        other.goto(f"{base}/app/")
        other.click("text=Create an account")
        other.fill("#email", "other@example.com")
        other.fill("#password", PASSWORD)
        other.click("button[type=submit]")
        expect(other.get_by_role("heading", name="Find where your numbers moved")).to_be_visible()
        other.goto(owner_url)
        other.reload()
        expect(other.locator(".notice.error")).to_contain_text("not found")
        check("another user's dataset link says not found")
        other.goto(f"{base}/app/#/")
        expect(other.locator("text=Nothing here yet")).to_be_visible()
        check("the other user's own list is empty")

        # ---- mobile layout and sign out
        page.set_viewport_size({"width": 390, "height": 800})
        page.goto(f"{base}/app/#/")
        page.screenshot(path=str(OUT / "home_mobile.png"), full_page=True)
        page.click(".dscard a.title >> nth=0")
        expect(page.locator("#metric")).to_be_visible()
        wide = page.evaluate("""() => [...document.querySelectorAll('body *')].filter(
            e => e.getBoundingClientRect().right > innerWidth + 1).map(e => e.tagName + '.' + e.className)""")
        print("elements wider than the phone:", wide, page.evaluate(
            "[document.documentElement.scrollWidth, innerWidth, document.body.scrollWidth]"))
        check("no sideways page scroll at phone width (dataset page)",
              page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
        page.select_option("#metric", "orders")
        page.click("text=Find where it moved")
        expect(page.locator(".verdict")).to_be_visible(timeout=30000)
        page.screenshot(path=str(OUT / "analysis_mobile.png"), full_page=True)
        check("no sideways page scroll at phone width (analysis page)",
              page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
        page.click("#logout")
        expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
        check("sign out returns to the sign-in page")
        page.goto(owner_url)
        expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
        check("a signed-out visit to a private page asks you to sign in")
        browser.close()

    print("screenshots:", OUT)
    # Deliberate failure cases (bad login, weak password, wrong file type, revoked link, another
    # user's id) make the browser log "Failed to load resource"; anything else is a real error.
    expected = ("Failed to load resource: the server responded with a status of 4",)
    real = [m for m in problems if not m.startswith(expected)]
    print("unexpected browser errors:", real or "none")
    assert not real, real
    print(f"{len(passed)} browser checks passed")


if __name__ == "__main__":
    main()
