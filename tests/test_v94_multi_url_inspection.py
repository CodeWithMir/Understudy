"""9.4: several target URLs per session, the automation-aware inspection (plan the checks from the steps, open each page
once, reload only when needed), and the complexity assessment handed to the build / heal agent."""
import io
import json
import os
import unittest
import zipfile
from unittest import mock

from tests.wizard_helpers import LiveServer, browser_available  # noqa: F401  (sets the isolated test environment)

from core.wizard import (build as build_mod, build_agent, complexity, executor, exporter, governance, inspection,
                         page_probe, templates, watch)
from core.wizard.frameworks import CATALOG, FrameworkSpec
from core.wizard.guardrails import parse_url_list, screen_url

HAS_BROWSER = browser_available()


def spec(tid="playwright-python"):
    return FrameworkSpec.from_dict(CATALOG[tid].to_dict())


STEPS = [
    {"order": 1, "action": "navigate", "value": ""},
    {"order": 2, "action": "fill", "label": "Username", "param_key": "username"},
    {"order": 3, "action": "fill", "label": "Password", "param_key": "password"},
    {"order": 4, "action": "click", "label": "Sign in"},
    {"order": 5, "action": "navigate", "value": "https://app.example.com/upload"},
    {"order": 6, "action": "fill", "label": "Attach invoice", "param_key": "invoice_file"},
    {"order": 7, "action": "click", "label": "Delete draft"},
]
URLS = ["https://login.example.com/", "https://app.example.com/upload"]


class TestUrlList(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(parse_url_list('["https://a.example/x", "https://b.example/y", "https://a.example/x"]'),
                         ["https://a.example/x", "https://b.example/y"])
        self.assertEqual(parse_url_list("https://a.example/x\nhttps://b.example/y\n\n"),
                         ["https://a.example/x", "https://b.example/y"])
        self.assertEqual(parse_url_list("https://a.example/x?q=1,2"), ["https://a.example/x?q=1,2"])
        self.assertEqual(parse_url_list(None), [])
        self.assertEqual(parse_url_list(["", " https://a.example "]), ["https://a.example"])

    def test_same_host_accepts_any_listed_host(self):
        ok = screen_url("https://app.example.com/upload", resolve=False, same_host_as=URLS)
        self.assertNotIn("differs from the target host", " ".join(ok.reasons))
        bad = screen_url("https://evil.example.net/", resolve=False, same_host_as=URLS)
        self.assertFalse(bad.is_safe)
        self.assertIn("differs from the target host", " ".join(bad.reasons))

    def test_navigate_to_a_listed_page_survives_normalisation(self):
        raw = {"steps": [{"action": "navigate", "value": "https://app.example.com/upload"}], "parameters": []}
        with mock.patch.object(watch, "screen_url", side_effect=lambda u, **k: screen_url(u, resolve=False, **k)):
            one, _ = watch.normalize_analysis(raw, URLS[0])
            two, _ = watch.normalize_analysis(raw, URLS[0], URLS[1:])
        self.assertEqual(one["steps"][0]["value"], "")                         # another host, not listed: dropped
        self.assertEqual(two["steps"][0]["value"], "https://app.example.com/upload")

    def test_prompt_lists_pages_in_order(self):
        self.assertEqual(watch.urls_for_prompt(URLS[0]), URLS[0])
        txt = watch.urls_for_prompt(URLS[0], URLS[1:])
        self.assertIn("1. https://login.example.com/  (start page)", txt)
        self.assertIn("2. https://app.example.com/upload", txt)


class TestRunContract(unittest.TestCase):
    def test_page_env(self):
        env = executor.page_url_env(URLS[0], URLS[1:])
        self.assertEqual(env["TARGET_URL"], URLS[0])
        self.assertEqual(env["TARGET_URL_1"], URLS[0])
        self.assertEqual(env["TARGET_URL_2"], URLS[1])
        self.assertEqual(env["CYPRESS_TARGET_URL_2"], URLS[1])
        self.assertEqual(env["TARGET_URLS"].split(" "), URLS)

    def test_scan_allows_every_listed_host_only(self):
        files = {"a.py": 'X = "https://app.example.com/upload"\nY = "https://evil.example.net/"\n'}
        kinds = [(f["kind"], f["message"]) for f in governance.scan_suite(files, [], URLS)]
        self.assertEqual(len([k for k in kinds if k[0] == "UNEXPECTED_HOST"]), 1)
        self.assertIn("evil.example.net", kinds[0][1])

    def test_templates_read_other_pages_from_the_environment(self):
        steps = [dict(s) for s in STEPS if s["action"] in ("navigate", "click")]
        for tid in ("playwright-python", "selenium-python", "playwright-js", "puppeteer", "cypress", "robot-framework"):
            sp = spec(tid)
            files, entry = templates.render(sp.template, steps, [], None, sp, page_urls=URLS)
            blob = "\n".join(files.values())
            self.assertIn("TARGET_URL_2", blob, tid)
            self.assertNotIn("app.example.com", blob, tid)
            self.assertEqual(build_mod._syntax_check(files, sp), [], tid)
        self.assertEqual(STEPS[4]["value"], "https://app.example.com/upload")        # the plan itself is unchanged

    def test_export_documents_the_pages(self):
        s = {"target_url": URLS[0], "target_urls": URLS}
        md = exporter._pages_md(s)
        self.assertIn("`TARGET_URL_2` | `https://app.example.com/upload`", md)
        self.assertEqual(exporter._pages_md({"target_url": URLS[0]}), "")


class TestInspectionPlan(unittest.TestCase):
    def test_pages_of_steps(self):
        self.assertEqual(inspection.page_of_steps(STEPS, URLS), {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 2, 7: 2})

    def test_rules_choose_checks_from_what_the_automation_does(self):
        p = inspection.rule_plan(STEPS, [{"key": "password", "type": "secret"}], "", URLS)
        first, second = p["pages"]
        self.assertIn("login", first["checks"])
        self.assertIn("file_upload", second["checks"])
        self.assertIn("dialogs", second["checks"])             # 'Delete draft' may pop up a confirm
        self.assertNotIn("file_upload", first["checks"])
        self.assertNotIn("repeated_rows", first["checks"])
        for pg in p["pages"]:
            for e in inspection.ESSENTIAL:
                self.assertIn(e, pg["checks"])
        self.assertEqual(p["stability"], "if_suspicious")      # no reload unless the first load looks generated

    def test_rows_in_the_sop_mean_reload_and_compare(self):
        p = inspection.rule_plan(STEPS[:4], [], "Repeat this for each row of the Excel file", URLS[:1])
        self.assertEqual(p["stability"], "always")
        self.assertIn("repeated_rows", p["pages"][0]["checks"])

    def test_ai_plan_is_filtered_to_the_menu_and_keeps_the_essentials(self):
        res = mock.Mock(ok=True, agent_source="ACTUAL_AI_API", model="fake", data={
            "pages": [{"index": 1, "checks": [{"id": "login", "why": "step 3 types a password"},
                                              {"id": "rm -rf", "why": "x"}, {"id": "stability", "why": "x"}]},
                      {"index": 9, "checks": [{"id": "frames"}]}],
            "stability": "never", "stability_why": "only stable buttons", "focus": ["sign-in first"]})
        s = {"analysis": {"steps": STEPS}, "parameters": [], "sop_text": ""}
        with mock.patch.object(inspection, "generate_json", return_value=res):
            p = inspection.plan(s, URLS)
        c1 = p["pages"][0]["checks"]
        self.assertEqual(set(c1), {"login", *inspection.ESSENTIAL})
        self.assertEqual(c1["login"], "step 3 types a password")
        self.assertEqual(len(p["pages"]), 2)                   # page 9 does not exist
        self.assertEqual((p["stability"], p["focus"], p["agent_source"]), ("never", ["sign-in first"], "ACTUAL_AI_API"))

    def test_without_ai_the_rules_answer(self):
        with mock.patch.object(inspection, "generate_json", return_value=mock.Mock(ok=False, data=None)):
            p = inspection.plan({"analysis": {"steps": STEPS}, "parameters": [], "sop_text": ""}, URLS)
        self.assertEqual(p["agent_source"], "LOCAL_HEURISTIC")


class TestComplexityForBuild(unittest.TestCase):
    PROF = {"renderer": "chromium", "signals": {"pages": 2, "hosts": 2, "needs_login_first": True, "dialogs": 1}}

    def test_multi_page_factors(self):
        b = complexity.baseline(STEPS, [], "", self.PROF)
        names = [f["name"] for f in b["factors"]]
        for n in ("Several pages", "More than one site", "A page needs a sign-in first", "Pop-up dialogs"):
            self.assertIn(n, names)
        self.assertGreaterEqual(b["score"], 5)

    def test_irrelevant_page_features_do_not_count(self):
        # the inspection only reports an iframe when a step's element is inside it
        b = complexity.baseline(STEPS[:2], [], "", {"renderer": "chromium", "signals": {"pages": 1}})
        self.assertEqual(b["score"], 1)

    def test_for_prompt(self):
        c = dict(complexity.baseline(STEPS, [], "", self.PROF), summary="Two sites.", tips=["use a test account"])
        txt = complexity.for_prompt(c)
        self.assertIn("CHALLENGES (handle each one):", txt)
        self.assertIn("More than one site", txt)
        self.assertIn("use a test account", txt)
        self.assertEqual(complexity.for_prompt(None), "(not assessed)")

    def test_build_agent_hands_complexity_and_pages_to_the_code_generator(self):
        seen = {}

        def fake_json(prompt_id, variables, **kw):
            seen.update(variables)
            return mock.Mock(ok=False, error="offline", data=None, recall=None)
        c = dict(complexity.baseline(STEPS, [], "", self.PROF), summary="Two sites.", tips=[])
        with mock.patch.object(build_mod, "generate_json", side_effect=fake_json):
            out = build_agent.generate(spec(), [dict(s) for s in STEPS if s["action"] in ("navigate", "click")], [],
                                       None, URLS, [], profile=None, complexity=c)
        self.assertIn("More than one site", seen["complexity"])
        self.assertIn("TARGET_URL_2", seen["pages"])
        self.assertIn("${TARGET_URL_2}", json.dumps(seen["steps"]))
        self.assertEqual(out["generator"], "template")

    def test_locators_checked_once_per_page_with_the_profile(self):
        calls = []

        def verify(url, locs, loads=1, profile=None):  # noqa: E306
            calls.append((url, loads, profile))
            return {"checked": 1, "loads": 2, "ok": True, "static_checks": True, "results": [
                {"id": "a", "strategy": "xpath", "value": "v", "ok": True, "problems": []}]}
        loc = {"id": "a", "strategy": "xpath", "value": "//label[.='A']/following::input[1]", "kind": "fill"}
        out = {"files": {"run_automation.py": "page.locator(\"xpath=//label[.='A']/following::input[1]\")"},
               "entry_file": "run_automation.py", "generator": "ai", "agent_source": "ACTUAL_AI_API", "model": "f",
               "notes": [], "findings": [], "locators": [loc]}
        prof = {"renderer": "chromium", "signals": {}, "fields": [], "verify_loads": {1: 2, 2: 1}}
        with mock.patch.object(build_mod, "generate_suite", return_value=out), \
                mock.patch.object(page_probe, "verify_locators", side_effect=verify):
            res = build_agent.generate(spec(), [], [], None, URLS, [], profile=prof,
                                       complexity={"score": 6, "level": "Complex", "factors": []})
        self.assertEqual(calls, [(URLS, {1: 2, 2: 1}, prof)])          # the loads the reload agent decided
        self.assertEqual(res["agent"]["complexity"], {"score": 6, "level": "Complex"})


class TestStaticLocatorChecks(unittest.TestCase):
    PROF = {"renderer": "chromium", "loads": 2, "signals": {"shuffled_fields": True},
            "fields": [{"label": "First Name", "unstable": ["id"], "unstable_values": {"id": "x7k2m9"}}]}

    def test_known_unstable_id(self):
        self.assertTrue(page_probe.static_problems({"value": "#x7k2m9"}, self.PROF))
        self.assertTrue(page_probe.static_problems({"value": "input >> nth=2"}, self.PROF))
        self.assertEqual(page_probe.static_problems(
            {"value": "//label[normalize-space()='First Name']/following::input[1]"}, self.PROF), [])
        self.assertEqual(page_probe.static_problems({"value": "#x7k2m9"}, None), [])

    def test_positional(self):
        for v in ("input >> nth=0", "form input:nth-child(3)", "(//input)[2]", "//form/div[3]/input"):
            self.assertTrue(page_probe.is_positional(v), v)
        for v in ("//label[normalize-space()='Email']/following::input[1]", "#email", "//input[@name='q']"):
            self.assertFalse(page_probe.is_positional(v), v)


@unittest.skipUnless(HAS_BROWSER, "Playwright Chromium not available")
class TestInspectLivePages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = LiveServer.get().base

    def test_each_page_once_and_reload_only_the_shifting_one(self):
        urls = [self.base + "/portal3", self.base + "/portal"]
        steps = [{"order": 1, "action": "navigate", "value": ""},
                 {"order": 2, "action": "fill", "label": "First Name"},
                 {"order": 3, "action": "click", "label": "Submit"},
                 {"order": 4, "action": "navigate", "value": urls[1]},
                 {"order": 5, "action": "fill", "label": "Vendor Name"}]
        prof = inspection.inspect(urls, inspection.rule_plan(steps, [], "", urls), steps)
        self.assertEqual(prof["renderer"], "chromium", prof.get("error"))
        pages = {p["index"]: p for p in prof["pages"]}
        self.assertEqual(pages[1]["loads"], 2)                 # generated-looking ids on the fields the steps use
        self.assertIn("generated-looking", pages[1]["reload_why"])
        self.assertEqual(pages[2]["loads"], 1)                 # a stable page is opened once
        self.assertEqual(prof["loads"], 3)
        self.assertTrue(prof["signals"]["dynamic_ids"] or prof["signals"]["dynamic_names"])
        targets = [c for c in prof["checks"] if c["id"] == "targets"]
        self.assertEqual([c["missing"] for c in targets], [0, 0])
        # one fresh load per page; the id the inspection saw change is rejected without another load
        first = next(f for f in prof["fields"] if f["label"] == "First Name")
        vendor = next(f for f in prof["fields"] if f.get("page") == 2 and f["label"] == "Vendor Name")
        res = page_probe.verify_locators(urls, [
            {"id": "label", "strategy": "xpath", "value": first["recommended"]["value"], "kind": "fill",
             "expect_label": "First Name"},
            {"id": "id", "strategy": "css", "value": "#" + first["unstable_values"]["id"], "kind": "fill"},
            {"id": "vendor", "strategy": vendor["recommended"]["strategy"], "value": vendor["recommended"]["value"],
             "kind": "fill", "expect_label": "Vendor Name", "page": 2}], profile=prof)
        by = {r["id"]: r for r in res["results"]}
        self.assertTrue(by["label"]["ok"], by["label"])
        self.assertTrue(by["vendor"]["ok"], by["vendor"])
        self.assertFalse(by["id"]["ok"])
        self.assertIn("changed when the page was reloaded", by["id"]["problems"][0])
        self.assertEqual(res["loads"], 2)

    def test_the_agent_decides_how_many_times_each_page_is_reloaded(self):
        urls = [self.base + "/portal3", self.base + "/portal"]
        steps = [{"order": 1, "action": "navigate", "value": ""},
                 {"order": 2, "action": "fill", "label": "First Name"},
                 {"order": 3, "action": "navigate", "value": urls[1]},
                 {"order": 4, "action": "fill", "label": "Vendor Name"}]
        seen = {}

        def agent(prompt_id, variables, **kw):
            if prompt_id != "INSPECTION_RELOAD":
                return mock.Mock(ok=False, data=None)
            seen["evidence"] = json.loads(variables["evidence"])
            seen["baseline"] = json.loads(variables["baseline"])
            return mock.Mock(ok=True, agent_source="ACTUAL_AI_API", model="fake", data={"pages": [
                {"page": 1, "reloads": 3, "why": "only 7 fields - the order may repeat by chance", "verify_loads": 2,
                 "verify_why": "ids change between loads"},
                {"page": 2, "reloads": 9, "why": "testing the cap", "verify_loads": 0}]})
        with mock.patch.object(inspection, "generate_json", side_effect=agent):
            prof = inspection.inspect(urls, inspection.rule_plan(steps, [], "", urls), steps)
        ev = {e["page"]: e for e in seen["evidence"]}
        self.assertTrue(ev[1]["generated_looking_ids_on_step_fields"])      # the agent saw the first load
        self.assertEqual(ev[2]["generated_looking_ids_on_step_fields"], [])
        self.assertEqual((seen["baseline"]["1"]["reloads"], seen["baseline"]["2"]["reloads"]), (1, 0))
        pages = {p["index"]: p for p in prof["pages"]}
        self.assertEqual(pages[1]["loads"], 4)                              # 1 + the 3 reloads it asked for
        self.assertEqual(pages[2]["loads"], 1 + inspection.MAX_RELOADS)     # clamped to the limit
        self.assertEqual(pages[1]["reload_decided_by"], "ACTUAL_AI_API")
        self.assertEqual(prof["verify_loads"], {1: 2, 2: 1})
        st = next(c for c in prof["checks"] if c["id"] == "stability" and c["page"] == 1)
        self.assertIn("the agent decided", st["why"])
        self.assertIn("reloaded 3 times", st["finding"])

    def test_the_agent_may_decide_not_to_reload(self):
        urls = [self.base + "/portal3"]
        steps = [{"order": 1, "action": "navigate", "value": ""}, {"order": 2, "action": "fill", "label": "First Name"}]
        res = mock.Mock(ok=True, agent_source="ACTUAL_AI_API", model="fake",
                        data={"pages": [{"page": 1, "reloads": 0, "why": "labels are used anyway"}]})
        with mock.patch.object(inspection, "generate_json", return_value=res):
            prof = inspection.inspect(urls, inspection.rule_plan(steps, [], "", urls), steps)
        self.assertEqual(prof["loads"], 1)
        st = next(c for c in prof["checks"] if c["id"] == "stability")
        self.assertIn("the agent decided no reload: labels are used anyway", st["why"])

    def test_a_step_reached_by_a_click_is_found_on_the_next_page(self):
        urls = [self.base + "/portal3", self.base + "/portal"]
        steps = [{"order": 1, "action": "navigate", "value": ""},
                 {"order": 2, "action": "fill", "label": "First Name"},
                 {"order": 3, "action": "click", "label": "Submit"},
                 {"order": 4, "action": "fill", "label": "Vendor Name"}]       # no navigate step: a click got there
        prof = inspection.inspect(urls, inspection.rule_plan(steps, [], "", urls), steps)
        t1 = next(c for c in prof["checks"] if c["id"] == "targets" and c["page"] == 1)
        self.assertEqual(t1["missing"], 0, t1)
        self.assertIn("later page", t1["finding"])
        self.assertEqual(prof["signals"]["missing_targets"], 0)
        self.assertIn(4, next(p for p in prof["pages"] if p["index"] == 2)["steps"])

    def test_two_page_offline_run_reads_page_two_from_the_environment(self):
        import requests
        from tests.wizard_helpers import fake_mp4, install_fake
        install_fake(None)
        B = self.base
        api = lambda m, p, **k: requests.request(m, B + "/api/wizard" + p, timeout=300, **k)  # noqa: E731
        requests.post(B + "/api/erp/portal3/reset")
        urls = [B + "/portal3", B + "/portal"]
        sop = ("Fill the form with First Name Ada, Last Name Lovelace, Company Name Analytical Engines, Role in Company "
               "Engineer, Address 1 Main St, Email ada@example.com, Phone Number 5551234 and submit.")
        with mock.patch.dict(os.environ, {"UNDERSTUDY_PAGE_PROBE": "on"}), open(fake_mp4(), "rb") as fh:
            r = api("POST", "/sessions?wait=true", data={"target_urls": json.dumps(urls), "sop_text": sop},
                    files={"media": ("rec.mp4", fh, "video/mp4")})
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()["session"]
        sid = s["session_id"]
        self.assertEqual(s["target_urls"], urls)
        self.assertIn(urls[1], [st.get("value") for st in s["analysis"]["steps"]])
        self.assertIn("Several pages", [f["name"] for f in s["complexity"]["factors"]])
        self.assertEqual([p["loads"] for p in s["page_profile"]["pages"]], [2, 1])
        s = api("POST", f"/sessions/{sid}/framework?wait=true", json={"framework_id": "playwright-python"}).json()["session"]
        if s["dependencies"]["status"] not in ("READY",):
            self.skipTest("Playwright dependencies not installable here: " + s["dependencies"]["status"])
        api("PUT", f"/sessions/{sid}/govern/rules", json={"rules": s["rules"], "confirmed": True})
        api("POST", f"/sessions/{sid}/govern/check")
        s = api("POST", f"/sessions/{sid}/build?wait=true").json()["session"]
        code = "\n".join(s["suite"]["files"].values())
        self.assertIn("${TARGET_URL_2}", code)
        self.assertNotIn(urls[1] + '"', code)
        s = api("POST", f"/sessions/{sid}/execute?wait=true").json()["session"]
        self.assertEqual(s["pillars"]["BUILD-EXECUTE"]["status"], "PASS", s["execution"].get("failure_reason"))
        sub = requests.get(B + "/api/erp/portal3/submissions").json()["submissions"][-1]
        self.assertEqual(sub["First Name"], "Ada")
        z = zipfile.ZipFile(io.BytesIO(requests.get(B + f"/api/wizard/sessions/{sid}/export").content))
        env = z.read(next(n for n in z.namelist() if n.endswith(".env.example"))).decode()
        self.assertIn(f"TARGET_URL_2={urls[1]}", env)

    def test_an_unsafe_second_url_is_refused_with_its_index(self):
        import requests
        from tests.wizard_helpers import fake_mp4
        with open(fake_mp4(), "rb") as fh:
            r = requests.post(self.base + "/api/wizard/sessions", data={
                "target_urls": json.dumps([self.base + "/portal", "http://169.254.169.254/latest/meta-data/"])},
                files={"media": ("rec.mp4", fh, "video/mp4")}, timeout=60)
        self.assertEqual(r.status_code, 400, r.text)
        d = r.json()["detail"]
        self.assertEqual((d["details"]["field"], d["details"]["index"]), ("target_urls", 1))
        self.assertIn("Page address 2", d["message"])


if __name__ == "__main__":
    unittest.main()
