"""Browser test for the statement, price/volume/mix, market-share and trend screens.

Run: PYTHONPATH=src python web_tests/e2e_finance.py [statement.xlsx ...]
With no arguments a synthetic data-site workbook is used; pass real workbooks to try them.
"""
import io
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))
from stub_server import make_server  # noqa: E402
from tests.test_analyses import screener_workbook  # noqa: E402

from whymoved.generator import make_scenario  # noqa: E402
from whymoved.monthly import make_monthly  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="wm_fin_"))
passed = []


def check(name, cond=True):
    assert cond, name
    passed.append(name)
    print("ok -", name)


def csv_bytes(df) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


def main(files):
    srv, _ = make_server(0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    problems = []
    statements = []
    if files:
        statements = [Path(f) for f in files]
    else:
        path = OUT / "TEST_CO.xlsx"
        path.write_bytes(screener_workbook()[0])
        statements = [path]
    pvm = OUT / "pvm.csv"
    pvm.write_bytes(csv_bytes(make_scenario("pvm", 5).data))
    monthly = OUT / "monthly.csv"
    monthly.write_bytes(csv_bytes(make_monthly(3, months=36)))
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium"
                                    if Path("/opt/pw-browsers/chromium").exists() else None)
        page = browser.new_context(viewport={"width": 1100, "height": 900}).new_page()
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{base}/app/")
        page.click("text=Create an account")
        page.fill("#email", "fin@example.com")
        page.fill("#password", "Correct-horse-9")
        page.click("button[type=submit]")
        expect(page.get_by_role("heading", name="Find where your numbers moved")).to_be_visible()

        # ---- statements
        for path in statements:
            page.goto(f"{base}/app/#/")
            page.set_input_files("#file", str(path))
            expect(page.get_by_role("heading", name="Financial statement analysis")).to_be_visible(timeout=30000)
            check(f"{path.name}: uploaded and recognised as a financial statement")
            check(f"{path.name}: revenue row is pre-selected",
                  page.locator("#role_revenue").input_value() != "")
            page.fill("#cur", "Rs ")
            page.check("#ind")
            page.click("text=Work out the ratios")
            expect(page.get_by_role("heading", name="In words")).to_be_visible(timeout=30000)
            body = page.inner_text("main")
            check(f"{path.name}: ratio table and caveat shown", "Net margin" in body and "not why" in body)
            check(f"{path.name}: written summary has no causal wording", " because " not in
                  page.inner_text(".summary").lower())
            check(f"{path.name}: ROE waterfall drawn", page.locator("svg.wf rect").count() == 5)
            page.screenshot(path=str(OUT / f"statements_{path.stem}.png"), full_page=True)
            page.click("text=Create share link")
            link = page.locator(".sharebox input").input_value()
            anon = browser.new_context().new_page()
            anon.on("pageerror", lambda e: problems.append(str(e)))
            anon.goto(link)
            expect(anon.get_by_role("heading", name="In words")).to_be_visible()
            check(f"{path.name}: share page renders the statement result")

        # ---- price / volume / mix, market share
        page.goto(f"{base}/app/#/")
        page.set_input_files("#file", str(pvm))
        expect(page.locator("#pvm_rev")).to_be_visible(timeout=30000)
        page.click("text=Split into price, volume and mix")
        expect(page.get_by_role("heading", name="In words")).to_be_visible(timeout=30000)
        check("a column named cost is picked up: profit bridge with six bars", page.locator("svg.wf rect").count() == 6)
        check("price/volume/mix segment table is present", page.locator("table tbody tr").count() >= 1)
        page.screenshot(path=str(OUT / "pvm.png"), full_page=True)
        page.click("text=Back to dataset")
        page.select_option("#pvm_cost", "")
        page.click("text=Split into price, volume and mix")
        expect(page.get_by_role("heading", name="In words")).to_be_visible(timeout=30000)
        check("without a cost column it is a five-bar revenue bridge", page.locator("svg.wf rect").count() == 5)

        # ---- trends on a file with no category column
        page.goto(f"{base}/app/#/")
        page.set_input_files("#file", str(monthly))
        expect(page.get_by_role("heading", name="Trend and seasonality")).to_be_visible(timeout=30000)
        page.click("text=Show the trend")
        expect(page.get_by_role("heading", name="Month by month")).to_be_visible(timeout=30000)
        check("trend chart is drawn", page.locator("svg.line path.ln").count() >= 1)
        check("seasonal pattern shown for 36 months", page.get_by_role("heading", name="Seasonal pattern").count() == 1)
        page.screenshot(path=str(OUT / "trends.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 800})
        page.reload()
        check("no sideways page scroll at phone width (trend result)",
              page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
        browser.close()
    real = [m for m in problems if not m.startswith("Failed to load resource: the server responded with a status of 4")]
    print("unexpected browser errors:", real or "none")
    print("screenshots:", OUT)
    assert not real, real
    print(f"{len(passed)} browser checks passed")


if __name__ == "__main__":
    main(sys.argv[1:])
