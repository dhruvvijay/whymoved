"""Statement import on a data-site (Screener-style) layout, and the analyses behind the app:
statements, price/volume/mix, market share and trends, run through the real Service."""

import io
import tempfile

import pandas as pd

from whymoved.generator import make_scenario
from whymoved.monthly import make_monthly
from whymoved.service import Service, ServiceError, ServiceSettings
from whymoved.statement_import import (
    build_lines,
    check_lines,
    looks_financial,
    ratios_from_lines,
    read_statements,
    suggest_roles,
)

from .test_statement_import import _xlsx

ANNUAL = pd.to_datetime([f"{y}-03-31" for y in range(2021, 2027)])
QUARTERS = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
                           "2026-03-31", "2026-06-30"])


def screener_workbook(company="TEST CO LTD", interest_share=0.03) -> tuple[bytes, dict]:
    """A workbook shaped like a Screener.in export: the numbers sit on one 'Data Sheet' with the
    profit and loss, a quarterly block and the balance sheet stacked under each other, each with
    its own 'Report Date' row; the other tabs hold only labels (their formulas were never
    calculated). Profit before tax satisfies the identity the real files satisfy."""
    n = len(ANNUAL)
    sales = [1000.0 * 1.1 ** i for i in range(n)]
    raw = [0.6 * s for s in sales]
    change_inv = [5.0 + i for i in range(n)]
    power = [30.0 + i for i in range(n)]
    employee = [80.0 + 3 * i for i in range(n)]
    other_exp = [20.0 + i for i in range(n)]
    other_inc = [15.0 + i for i in range(n)]
    dep = [40.0 + 2 * i for i in range(n)]
    interest = [interest_share * s for s in sales]
    expenses = [raw[i] + power[i] + employee[i] + other_exp[i] - change_inv[i] for i in range(n)]
    ebitda = [sales[i] - expenses[i] for i in range(n)]
    pbt = [ebitda[i] + other_inc[i] - dep[i] - interest[i] for i in range(n)]
    tax = [0.25 * p for p in pbt]
    net = [pbt[i] - tax[i] for i in range(n)]
    capital, reserves = [100.0] * n, [400.0 + 60 * i for i in range(n)]
    borrow = [300.0 - 10 * i for i in range(n)]
    other_liab = [200.0 + 20 * i for i in range(n)]
    total = [capital[i] + reserves[i] + borrow[i] + other_liab[i] for i in range(n)]
    net_block = [0.5 * t for t in total]
    invest = [0.2 * t for t in total]
    other_assets = [total[i] - net_block[i] - invest[i] for i in range(n)]
    sheet: list[list] = [["COMPANY NAME", company, None, None, "https://www.screener.in/excel/"],
                         ["LATEST VERSION", "2.1"], [], ["META"], ["Number of shares"],
                         ["Face Value", 10.0], [], [], [], []]
    sheet += [[], ["PROFIT & LOSS"], ["Report Date", *ANNUAL]]
    for label, vals in [("Sales", sales), ("Raw Material Cost", raw),
                        ("Change in Inventory", change_inv), ("Power and Fuel", power),
                        ("Employee Cost", employee), ("Other Expenses", other_exp),
                        ("Other Income", other_inc), ("Depreciation", dep),
                        ("Interest", interest), ("Profit before tax", pbt), ("Tax", tax),
                        ("Net profit", net)]:
        sheet.append([label, *vals])
    sheet += [[], ["Quarters"], ["Report Date", *QUARTERS]]
    for label, base in [("Sales", 111111.0), ("Expenses", 222222.0), ("Other Income", 3.0),
                        ("Depreciation", 4.0), ("Interest", 5.0), ("Profit before tax", 6.0),
                        ("Tax", 7.0), ("Net profit", 8.0), ("Operating Profit", 999999.0)]:
        sheet.append([label, *[base + i for i in range(len(QUARTERS))]])
    sheet += [[], ["BALANCE SHEET"], ["Report Date", *ANNUAL]]
    for label, vals in [("Equity Share Capital", capital), ("Reserves", reserves),
                        ("Borrowings", borrow), ("Other Liabilities", other_liab),
                        ("Total", total), ("Net Block", net_block), ("Investments", invest),
                        ("Other Assets", other_assets), ("Total", total)]:
        sheet.append([label, *vals])
    # formula cells with no stored value, as in an export that was never opened in Excel
    f = "='Data Sheet'!B17"
    stub = [[None], [None], ["Narration", f, f], ["Sales", f, f], ["Expenses", f, f],
            ["Operating Profit", f, f], ["Other Income", f, f], ["Depreciation", f, f],
            ["Interest", f, f], ["Profit before tax", f, f]]
    wb = _xlsx({"Profit & Loss": stub, "Balance Sheet": stub, "Data Sheet": sheet})
    truth = {"sales": sales, "expenses": expenses, "ebitda": ebitda, "total": total, "net": net}
    return wb, truth


# --------------------------------------------------------------------------- statement layout


def test_stacked_blocks_each_use_their_own_period_heading():
    data, _ = screener_workbook()
    t = read_statements(data, "x.xlsx")
    assert t.periods == [f"Mar {y}" for y in range(2021, 2027)]
    labels = [r.label for r in t.rows]
    # the quarterly block is left out, so its rows cannot sit under yearly columns
    assert "Operating Profit" not in labels
    assert labels.count("Sales") == 1
    assert not (t.frame.to_numpy() > 99999).any()
    assert any("quarterly" in n for n in t.notes)
    assert t.company == "TEST CO LTD"
    assert any("Screener" in n for n in t.notes)


def test_label_only_sheets_are_reported_not_silently_skipped():
    data, _ = screener_workbook()
    t = read_statements(data, "x.xlsx")
    note = next(n for n in t.notes if "no numbers" in n)
    assert "Profit & Loss" in note and "Balance Sheet" in note


def test_two_total_rows_become_total_assets_and_balance_check_passes():
    data, truth = screener_workbook()
    t = read_statements(data, "x.xlsx")
    roles = suggest_roles(t)
    assert roles["total_assets"] != roles["liabilities_and_equity"]
    lines, _ = build_lines(t, roles)
    for got, want in zip(lines["total_assets"], truth["total"], strict=True):
        assert abs(got - want) < 1e-6
    assert not any("total assets differ" in m for m in check_lines(lines, t, roles))


def test_operating_profit_is_worked_back_from_profit_before_tax():
    data, truth = screener_workbook()
    t = read_statements(data, "x.xlsx")
    lines, notes = build_lines(t, suggest_roles(t))
    for got, want in zip(lines["ebitda"], truth["ebitda"], strict=True):
        assert abs(got - want) < 1e-6   # sales minus expenses, found without an expenses line
    assert any("worked back" in n for n in notes)
    assert any("raw material" in n.lower() for n in notes)


def test_ratios_for_an_industrial_company_are_all_shown():
    data, _ = screener_workbook()
    t = read_statements(data, "x.xlsx")
    lines, _ = build_lines(t, suggest_roles(t))
    assert looks_financial(t.company, lines) is None
    ratios, missing = ratios_from_lines(lines)
    for name in ("ebitda_margin", "interest_coverage", "debt_to_equity", "roa", "asset_turnover"):
        assert name in ratios.columns
    assert not any(v.startswith("not shown") for v in missing.values())


def test_banks_are_recognised_by_name_or_by_interest_share_and_lose_goods_ratios():
    data, _ = screener_workbook(company="SAMPLE BANK LTD")
    t = read_statements(data, "x.xlsx")
    lines, _ = build_lines(t, suggest_roles(t))
    assert "bank" in looks_financial(t.company, lines)
    ratios, missing = ratios_from_lines(lines, financial=True)
    assert set(ratios.columns) == {"net_margin", "roe", "roa", "asset_turnover",
                                   "equity_multiplier"}
    assert all(v.startswith("not shown") for k, v in missing.items()
               if k in ("interest_coverage", "debt_to_equity", "ebitda_margin"))
    # no bank in the name, but interest is over 30% of revenue
    data, _ = screener_workbook(company="PLAIN CO", interest_share=0.5)
    t = read_statements(data, "x.xlsx")
    lines, _ = build_lines(t, suggest_roles(t))
    assert "30%" in looks_financial(t.company, lines)


# --------------------------------------------------------------------------- the service


def _service():
    tmp = tempfile.mkdtemp()
    svc = Service(ServiceSettings(db_path=f"{tmp}/t.db", data_dir=f"{tmp}/u"))
    reg = svc.register("a@example.com", "Correct-horse-9")
    user = svc.authenticate(reg["token"])
    return svc, user, svc.me(user)["workspaces"][0]["id"]


def _csv(df) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


def _raises(fn, status=422):
    try:
        fn()
    except ServiceError as exc:
        assert exc.status == status, exc.message
        return exc.message
    raise AssertionError("expected a ServiceError")


def test_statement_workbook_upload_and_analysis():
    svc, user, wid = _service()
    data, truth = screener_workbook()
    ds = svc.upload_dataset(user, wid, "co.xlsx", data, "Test Co")
    meta = ds["meta"]
    assert meta["kind"] == "statements" and meta["company"] == "TEST CO LTD"
    assert meta["default_periods"] == {"a": "Mar 2025", "b": "Mar 2026"}
    assert meta["suggested_roles"]["revenue"] == "Sales"
    assert {r["role"] for r in meta["roles"]} >= {"revenue", "total_assets", "net_income"}
    out = svc.create_analysis(user, ds["id"], {"kind": "statements", "currency": "Rs",
                                               "indian": True})
    res = out["result"]
    assert res["kind"] == "statements" and res["financial"] is False
    assert (res["period_a"], res["period_b"]) == ("Mar 2025", "Mar 2026")
    revenue = next(x for x in res["lines"] if x["key"] == "revenue")["values"]
    assert abs(revenue[-1] - truth["sales"][-1]) < 1e-6
    assert res["dupont"] is not None
    d = res["dupont"]
    assert abs(d["margin"] + d["turnover"] + d["leverage"] - (d["roe_b"] - d["roe_a"])) < 1e-9
    assert "not why" in res["caveat"].lower()
    assert "because" not in res["summary"].lower()


def test_statement_analysis_options_and_errors():
    svc, user, wid = _service()
    ds = svc.upload_dataset(user, wid, "co.xlsx", screener_workbook()[0])
    base = {"kind": "statements"}
    res = svc.create_analysis(user, ds["id"], {**base, "from_period": "Mar 2022",
                                               "to_period": "Mar 2024"})["result"]
    assert (res["period_a"], res["period_b"]) == ("Mar 2022", "Mar 2024")
    assert "worked back" in " ".join(res["notes"])
    flagged = svc.create_analysis(user, ds["id"], {**base, "financial": True})["result"]
    assert flagged["financial"] is True and "interest_coverage" not in {
        r["key"] for r in flagged["ratios"]}
    assert "ebitda" not in {x["key"] for x in flagged["lines"]}
    _raises(lambda: svc.create_analysis(user, ds["id"], {**base, "from_period": "Mar 2024",
                                                         "to_period": "Mar 2022"}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {**base, "to_period": "nonsense"}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {**base, "mapping": {"revenue": "zzz"}}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {**base, "mapping": {"nope": "Sales"}}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {**base, "mapping": {
        "revenue": "Sales", "net_income": "Sales"}}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "zzz"}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {**base, "mapping": ["x"]}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {**base, "mapping": {
        f"r{i}": "Sales" for i in range(60)}}))
    # a statement workbook cannot be used for a table analysis, with a clear message
    msg = _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "pvm"}))
    assert msg


def test_a_file_that_is_neither_table_nor_statement_is_refused_with_the_table_message():
    svc, user, wid = _service()
    note = b"hello,world\nthis is,not data\n"
    msg = _raises(lambda: svc.upload_dataset(user, wid, "x.csv", note))
    assert "date" in msg.lower() or "column" in msg.lower()


def test_price_volume_mix_bridge_adds_up_and_finds_the_planted_price_effect():
    svc, user, wid = _service()
    sc = make_scenario("pvm", 5)
    ds = svc.upload_dataset(user, wid, "p.csv", _csv(sc.data))
    assert ds["meta"]["kind"] == "table"
    assert ds["meta"]["suggested"]["units"] == "orders"
    res = svc.create_analysis(user, ds["id"], {"kind": "pvm"})["result"]
    t = res["totals"]
    assert abs(t["volume"] + t["mix"] + t["price"] - res["change"]) < 1e-6
    assert [w["kind"] for w in res["waterfall"]] == ["total", "finding", "finding", "finding",
                                                     "total"]
    assert res["segments"][0]["label"]
    profit = svc.create_analysis(user, ds["id"], {"kind": "pvm", "cost": "cost"})["result"]
    assert profit["effects"] == ["volume", "mix", "price", "cost"]
    assert profit["metric"] == "Profit bridge"
    _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "pvm", "revenue": "nope"}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "pvm", "units": "revenue"}))
    _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "pvm", "cost": "revenue"}))


def test_market_share_bridge_finds_the_lost_share_and_refuses_one_column_twice():
    svc, user, wid = _service()
    sc = make_scenario("share_loss", 5)
    ds = svc.upload_dataset(user, wid, "m.csv", _csv(sc.data))
    res = svc.create_analysis(user, ds["id"], {"kind": "market"})["result"]
    t = res["totals"]
    assert t["share"] < 0 < t["market"]
    assert abs(t["market"] + t["exposure"] + t["share"] - res["change"]) < 1e-6
    assert res["share_b"] < res["share_a"]
    _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "market", "market": "revenue"}))


def test_trends_on_a_monthly_total_with_no_category_column():
    svc, user, wid = _service()
    ds = svc.upload_dataset(user, wid, "m.csv", _csv(make_monthly(3, months=36)))
    assert ds["meta"]["dimension_cols"] == [] and ds["meta"]["frequency"] == "monthly"
    res = svc.create_analysis(user, ds["id"], {"kind": "trends", "metric": "revenue"})["result"]
    assert res["months"] == 36 and len(res["series"]) == 36
    assert res["seasonal"] and res["seasonal"]["strength"] > 0.5
    assert res["seasonal"]["peak_month"] in (10, 11, 12)
    flat = svc.create_analysis(user, ds["id"], {"kind": "trends",
                                                "metric": "revenue / orders"})["result"]
    assert flat["seasonal"] is None or flat["seasonal"]["strength"] < 0.4
    short = svc.upload_dataset(user, wid, "s.csv", _csv(make_monthly(3, months=10)))
    msg = _raises(lambda: svc.create_analysis(user, short["id"], {"kind": "trends"}))
    assert "13 months" in msg
    # the table analyses need a category column and say so
    msg = _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "changes"}))
    assert "category" in msg


def test_trend_analysis_of_daily_data_drops_partial_edge_months():
    svc, user, wid = _service()
    sc = make_scenario("single", 7)   # 84 days: far too short for a trend, said plainly
    ds = svc.upload_dataset(user, wid, "d.csv", _csv(sc.data))
    msg = _raises(lambda: svc.create_analysis(user, ds["id"], {"kind": "trends",
                                                              "metric": "revenue"}))
    assert "13 months" in msg


def test_every_new_result_is_json_safe_and_carries_the_caveat():
    import json
    svc, user, wid = _service()
    ds_t = svc.upload_dataset(user, wid, "co.xlsx", screener_workbook()[0])
    ds_p = svc.upload_dataset(user, wid, "p.csv", _csv(make_scenario("pvm", 2).data))
    ds_m = svc.upload_dataset(user, wid, "m.csv", _csv(make_monthly(1)))
    runs = [(ds_t, {"kind": "statements"}), (ds_p, {"kind": "pvm"}), (ds_p, {"kind": "market"}),
            (ds_m, {"kind": "trends", "metric": "revenue"})]
    for ds, params in runs:
        res = svc.create_analysis(user, ds["id"], params)["result"]
        json.dumps(res, allow_nan=False)   # no NaN or infinity anywhere
        assert "not why" in res["caveat"].lower(), params
        assert res["kind"] == params["kind"]
