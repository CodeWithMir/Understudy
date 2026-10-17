"""Check Understudy 9.0's dynamic-page handling against the REAL rpachallenge.com (or any shifting form).

    python scripts/check_rpachallenge.py                      # https://rpachallenge.com/
    python scripts/check_rpachallenge.py http://localhost:8000/portal3
    python scripts/check_rpachallenge.py --headed             # watch it in a browser window

What it does - exactly what Understudy does before it writes code, then a real run:
1. PROBE   - loads the page twice (page_probe.probe): which ids / names change, whether fields move, each field's label
             and the label-anchored locator Understudy will use.
2. VERIFY  - checks every chosen locator on 2 more fresh loads (page_probe.verify_locators): exactly one visible
             field, the right label. Also shows that an id-based locator from the first load FAILS on the next load.
3. PLAN    - builds the offline plan from the visible labels (page_probe.plan_from_profile).
4. RUN     - clicks Start (if there is one) and submits all 10 rounds with the plan's locators, printing the field
             order of every round (it changes) and the site's final message (rpachallenge: "Congratulations ...
             Your success rate is 100% ...").

Needs: pip install playwright && python -m playwright install chromium (Understudy's own requirements).
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.wizard import page_probe  # noqa: E402

ROWS = [  # same columns as rpachallenge's challenge.xlsx (made-up people)
    {"First Name": "John", "Last Name": "Smith", "Company Name": "IT Solutions", "Role in Company": "Analyst",
     "Address": "98 North Road", "Email": "jsmith@itsolutions.co.uk", "Phone Number": "40716543298"},
    {"First Name": "Jane", "Last Name": "Dorsey", "Company Name": "MediCare", "Role in Company": "Medical Engineer",
     "Address": "11 Crown Street", "Email": "jdorsey@mc.com", "Phone Number": "40791345621"},
    {"First Name": "Albert", "Last Name": "Kipling", "Company Name": "Waterfront", "Role in Company": "Accountant",
     "Address": "22 Guild Street", "Email": "kipling@waterfront.com", "Phone Number": "40735416854"},
    {"First Name": "Michael", "Last Name": "Robertson", "Company Name": "MediCare", "Role in Company": "IT Specialist",
     "Address": "17 Farburn Terrace", "Email": "mrobertson@mc.com", "Phone Number": "40733652145"},
    {"First Name": "Doug", "Last Name": "Derrick", "Company Name": "Timepath Inc.", "Role in Company": "Analyst",
     "Address": "99 Shire Oak Road", "Email": "dderrick@timepath.co.uk", "Phone Number": "40799885412"},
    {"First Name": "Jessie", "Last Name": "Marlowe", "Company Name": "Aperture Inc.", "Role in Company": "Scientist",
     "Address": "27 Cheshire Street", "Email": "jmarlowe@aperture.us", "Phone Number": "40733154268"},
    {"First Name": "Stan", "Last Name": "Hamm", "Company Name": "Sugarwell", "Role in Company": "Advisor",
     "Address": "10 Dam Road", "Email": "shamm@sugarwell.org", "Phone Number": "40712462257"},
    {"First Name": "Michelle", "Last Name": "Norton", "Company Name": "Aperture Inc.", "Role in Company": "Scientist",
     "Address": "13 White Rabbit Street", "Email": "mnorton@aperture.us", "Phone Number": "40731254562"},
    {"First Name": "Stacy", "Last Name": "Shelby", "Company Name": "TechDev", "Role in Company": "HR Manager",
     "Address": "19 Pineapple Boulevard", "Email": "sshelby@techdev.com", "Phone Number": "40741785214"},
    {"First Name": "Lara", "Last Name": "Palmer", "Company Name": "Timepath Inc.", "Role in Company": "Programmer",
     "Address": "87 Orange Street", "Email": "lpalmer@timepath.co.uk", "Phone Number": "40731653845"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="https://rpachallenge.com/")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--rounds", type=int, default=10)
    a = ap.parse_args()

    print(f"1. PROBE {a.url} (2 fresh loads)")
    prof = page_probe.probe(a.url)
    if prof.get("renderer") != "chromium":
        sys.exit(f"   could not load the page: {prof.get('error')}")
    sig = prof["signals"]
    print(f"   dynamic ids: {sig.get('dynamic_ids')} · dynamic names: {sig.get('dynamic_names')} · fields move: "
          f"{sig.get('shuffled_fields')} · framework: {sig.get('framework') or '-'} · fields: {sig.get('field_count')}")
    print("   " + page_probe.strategy_summary(prof))
    fields = [f for f in prof["fields"] if f["kind"] == "field"]
    for f in fields:
        r = f.get("recommended") or {}
        print(f"   - {f['label'] or '(no label)':<16} -> {r.get('strategy')}: {r.get('value')}   unstable: {f.get('unstable')}")

    print("\n2. VERIFY the chosen locators on 2 more fresh loads")
    locs = [{"id": f["label"], "strategy": f["recommended"]["strategy"], "value": f["recommended"]["value"],
             "action": "fill", "expect_label": f["label"]} for f in fields if f.get("recommended")]
    res = page_probe.verify_locators(a.url, locs, loads=2)
    for r in res["results"]:
        print(f"   {'OK  ' if r['ok'] else 'FAIL'} {r['id']:<16} {'; '.join(r['problems'][:1])}")
    ok_all = res["ok"]

    # what a naive recorder would have used: the id seen on the first load
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        pg.goto(a.url, wait_until="networkidle")
        first_id = pg.eval_on_selector("input:not([type=submit]):not([type=button])", "e => e.id")
        b.close()
    if first_id:
        naive = page_probe.verify_locators(a.url, [{"id": "naive", "strategy": "css", "value": f"[id='{first_id}']",
                                                    "action": "fill"}], loads=1)
        print(f"   id-based locator [id='{first_id}'] from one load, on the next load: "
              f"{'still works' if naive['ok'] else 'BROKEN (as expected)'}")

    print("\n3. PLAN from the visible labels")
    plan = page_probe.plan_from_profile(prof)
    for st in plan["steps"][1:]:
        print(f"   {st['order']}. {st['action']} '{st['label']}' ({st['param_key']})")

    print(f"\n4. RUN {a.rounds} round(s) with those locators")
    by_label = {st["label"]: st["selector"] for st in plan["steps"] if st.get("label")}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=not a.headed, slow_mo=150 if a.headed else 0)
        pg = b.new_page()
        pg.goto(a.url, wait_until="networkidle")
        start = pg.locator("button:has-text('Start')")
        if start.count():
            start.first.click()
        for n, row in enumerate(ROWS[: a.rounds], 1):
            order = pg.eval_on_selector_all("label", "ls => ls.map(l => l.innerText.trim()).filter(Boolean)")
            for label, sel in by_label.items():
                pg.locator(sel).fill(row.get(label, ""), timeout=5000)
            pg.locator("input[type=submit], button[type=submit]").first.click()
            print(f"   round {n}: OK  - field order was {order}")
            time.sleep(0.3)
        msg = pg.locator(".message2, .congratulations, #message, body").first.inner_text()[:300]
        print("\n   page says:", " ".join(msg.split())[:300])
        b.close()
    print("\nRESULT:", "all locators held and every round was submitted" if ok_all else "some locators did not hold")


if __name__ == "__main__":
    main()
