"""7.2: when the payload breaks the SOP, Step 3 shows the problem with two remedies - "Fix payload" (change the value)
or "Update SOP instead" (change the rule). Both must really work, for an SOP written in any language:

* offline, the built-in parser reads the common rule phrases of English, Spanish, French, German, Portuguese, Italian
  and Hindi (numerals in any script, 5.000,00 amounts, 31.12.2026 dates); with AI, GOVERN_EXTRACT_RULES reads any
  language (here: Japanese through the AI path);
* an SOP Understudy can't read (another language, no AI) never passes silently: it is a blocking problem until rules
  are added by hand or the SOP is changed.
The flow is exercised through the same HTTP endpoints the Step 3 buttons call.
"""
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.wizard_helpers import FakeLLM, fake_mp4, install_fake
from tests.test_wizard_media_to_payload import TARGET, fixture_inventory
from core.wizard import admin_policy as ap, governance
from core.wizard import session as wz

# The same three rules in seven languages: approved vendors, spend limit 5000, invoices dated in 2026.
SOPS = {
    "en": ("Approved vendors: Acme Industrial Supplies, Globex Corp.\nThe spend limit per invoice is $5,000.\n"
           "Invoices must be dated between 2026-01-01 and 2026-12-31.",
           "The spend limit per invoice is $10,000."),
    "es": ("Proveedores aprobados: Acme Industrial Supplies y Globex Corp.\nEl límite de gasto por factura es de 5.000,00 EUR.\n"
           "Las facturas deben tener fecha entre 01/01/2026 y 31/12/2026.",
           "El límite de gasto por factura es de 10.000,00 EUR."),
    "fr": ("Fournisseurs approuvés : Acme Industrial Supplies et Globex Corp.\nLe plafond de dépenses par facture est de 5000 €.\n"
           "Les factures doivent être datées entre le 01/01/2026 et le 31/12/2026.",
           "Le plafond de dépenses par facture est de 10000 €."),
    "de": ("Zugelassene Lieferanten: Acme Industrial Supplies und Globex Corp.\nDer Betrag darf 5.000 EUR nicht überschreiten.\n"
           "Rechnungen müssen datiert sein zwischen 01.01.2026 und 31.12.2026.",
           "Der Betrag darf 10.000 EUR nicht überschreiten."),
    "pt": ("Fornecedores aprovados: Acme Industrial Supplies, Globex Corp.\nO limite de gastos por fatura é de 5000.",
           "O limite de gastos por fatura é de 10000."),
    "it": ("Fornitori approvati: Acme Industrial Supplies, Globex Corp.\nIl limite di spesa per fattura è di 5000.",
           "Il limite di spesa per fattura è di 10000."),
    "hi": ("स्वीकृत विक्रेता: Acme Industrial Supplies, Globex Corp।\nप्रति चालान खर्च सीमा ५००० रुपये है।\n"
           "चालान की तारीख 2026-01-01 से 2026-12-31 के बीच होनी चाहिए।",
           "प्रति चालान खर्च सीमा १०००० रुपये है।"),
}
JAPANESE = "承認済みの仕入先: Acme Industrial Supplies、Globex Corp。\n請求書1件あたりの支出上限は5000ドルです。"


class SopBase(unittest.TestCase):
    def setUp(self):
        import server
        ap.reset_to_defaults()
        install_fake(None)                                   # offline: the built-in parser reads the SOP
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "dry-run"
        self.p = mock.patch("core.wizard.watch.dom_discovery.discover", side_effect=fixture_inventory)
        self.p.start()
        self.c = TestClient(server.app)

    def tearDown(self):
        self.p.stop()
        install_fake(None)
        os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)

    def built_session(self, sop: str, amount="9000", vendor="Globex Corp", date="2026-05-04") -> str:
        s = wz.create_session(TARGET, sop, fake_mp4(), "rec.mp4", background=False)
        sid = s["session_id"]
        wz.select_framework(sid, "playwright-python", background=False)
        self.set_values(sid, total_amount=amount, vendor_name=vendor, invoice_date=date)
        return sid                          # 9.9: Govern (Step 2) comes before Build - nothing is built yet

    def set_values(self, sid, **values):
        params = [dict(p) for p in wz.get(sid)["parameters"]]
        for p in params:
            if p["key"] in values:
                p["value"] = values[p["key"]]
        r = self.c.put(f"/api/wizard/sessions/{sid}/parameters", json={"parameters": params})   # "Fix payload"
        self.assertEqual(r.status_code, 200, r.text)

    def confirm_and_check(self, sid):
        rules = self.c.get(f"/api/wizard/sessions/{sid}").json()["session"]["rules"]
        r = self.c.put(f"/api/wizard/sessions/{sid}/govern/rules", json={"rules": rules, "confirmed": True})
        self.assertEqual(r.status_code, 200, r.text)
        return self.c.post(f"/api/wizard/sessions/{sid}/govern/check").json()["session"]


class TestBothRemediesInEveryLanguage(SopBase):
    def run_language(self, lang):
        sop, relaxed = SOPS[lang]
        sid = self.built_session(sop)
        s = wz.get(sid)
        kinds = {(r["field"], r["rule_type"]) for r in s["rules"]}
        self.assertIn(("vendor_name", "allowed_values"), kinds, (lang, s["rules"]))
        self.assertIn(("total_amount", "max"), kinds, (lang, s["rules"]))
        limit = next(r for r in s["rules"] if r["rule_type"] == "max")["limit"]
        self.assertEqual(limit, 5000, lang)
        # 9000 breaks the 5000 limit: Step 3 shows it, with a fix hint, and EXECUTE is blocked
        s = self.confirm_and_check(sid)
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "FAIL")
        v = next(x for x in s["compliance"]["violations"] if x["field"] == "total_amount")
        self.assertTrue(v["message"] and v["fix_hint"] and v["rule"], v)
        self.assertEqual(self.c.post(f"/api/wizard/sessions/{sid}/execute?wait=true").status_code, 409)

        # Remedy 1 - "Fix payload": change the value, check again -> PASS
        self.set_values(sid, total_amount="1234.56")
        s = self.c.post(f"/api/wizard/sessions/{sid}/govern/check").json()["session"]
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "PASS", s["compliance"])

        # back to the breaking value ...
        self.set_values(sid, total_amount="9000")
        s = self.c.post(f"/api/wizard/sessions/{sid}/govern/check").json()["session"]
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "FAIL")

        # Remedy 2 - "Update SOP instead": the new rule (same language) is re-read and must be confirmed first
        new_sop = sop.replace(sop.split("\n")[1], relaxed)
        r = self.c.post(f"/api/wizard/sessions/{sid}/govern/sop", json={"sop_text": new_sop})
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()["session"]
        self.assertFalse(s["rules_confirmed"])
        self.assertIsNone(s.get("compliance"))
        self.assertEqual(next(x for x in s["rules"] if x["rule_type"] == "max")["limit"], 10000, lang)
        self.assertEqual(self.c.post(f"/api/wizard/sessions/{sid}/govern/check").status_code, 409)   # not confirmed
        s = self.confirm_and_check(sid)
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "PASS", (lang, s["compliance"]))
        self.assertEqual(s["sop_text"], new_sop)                                                       # kept as written

    def test_english(self):
        self.run_language("en")

    def test_spanish(self):
        self.run_language("es")

    def test_french(self):
        self.run_language("fr")

    def test_german(self):
        self.run_language("de")

    def test_portuguese(self):
        self.run_language("pt")

    def test_italian(self):
        self.run_language("it")

    def test_hindi(self):
        self.run_language("hi")

    def test_vendor_rule_both_remedies_spanish(self):
        sid = self.built_session(SOPS["es"][0], amount="100", vendor="Umbrella Corporation")
        s = self.confirm_and_check(sid)
        v = next(x for x in s["compliance"]["violations"] if x["field"] == "vendor_name")
        self.assertIn("Umbrella Corporation", v["message"])
        self.set_values(sid, vendor_name="Globex Corp")                                    # fix payload
        self.assertEqual(self.c.post(f"/api/wizard/sessions/{sid}/govern/check").json()["session"]["pillars"]
                         ["GOVERN"]["status"], "PASS")
        self.set_values(sid, vendor_name="Umbrella Corporation")
        sop = SOPS["es"][0].replace("Globex Corp.", "Globex Corp, Umbrella Corporation.")   # update SOP
        self.c.post(f"/api/wizard/sessions/{sid}/govern/sop", json={"sop_text": sop})
        self.assertEqual(self.confirm_and_check(sid)["pillars"]["GOVERN"]["status"], "PASS")


class TestAnyLanguage(SopBase):
    def test_unreadable_language_offline_never_passes_silently(self):
        sid = self.built_session(JAPANESE)
        s = wz.get(sid)
        self.assertEqual(s["rules"], [])
        self.assertTrue(s["rules_meta"]["language_warning"])
        self.assertTrue(any("could not read any rule" in n for n in s["rules_meta"]["notes"]))
        s = self.confirm_and_check(sid)
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "FAIL")
        self.assertEqual(s["compliance"]["violations"][0]["kind"], "SOP_NOT_UNDERSTOOD")
        # remedy: add the rule by hand -> it is checked (and the payload's 9000 breaks it)
        rule = {"field": "total_amount", "rule_type": "max", "limit": 5000, "plain_english": "max 5000",
                "source_text": "請求書1件あたりの支出上限は5000ドルです。"}
        self.c.put(f"/api/wizard/sessions/{sid}/govern/rules", json={"rules": [rule], "confirmed": True})
        s = self.c.post(f"/api/wizard/sessions/{sid}/govern/check").json()["session"]
        self.assertEqual([v["field"] for v in s["compliance"]["violations"]], ["total_amount"])
        self.set_values(sid, total_amount="4000")
        s = self.c.post(f"/api/wizard/sessions/{sid}/govern/check").json()["session"]
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "PASS")

    def test_with_ai_any_language_is_read_and_names_stay_as_written(self):
        fake = FakeLLM({"GOVERN_EXTRACT_RULES": {"rules": [
            {"field": "vendor_name", "rule_type": "allowed_values", "values": ["Acme Industrial Supplies", "Globex Corp"],
             "source_text": "承認済みの仕入先: Acme Industrial Supplies、Globex Corp。", "plain_english": "Approved vendors"},
            {"field": "total_amount", "rule_type": "max", "limit": 5000,
             "source_text": "請求書1件あたりの支出上限は5000ドルです。", "plain_english": "Spend limit 5000"}],
            "security_flags": []}})
        sid = self.built_session(JAPANESE)
        install_fake(fake)
        s = wz.extract_rules(sid)
        prompt = fake.called("GOVERN_EXTRACT_RULES")[-1]
        self.assertIn("ANY language", prompt["system"])
        self.assertIn("請求書1件あたりの支出上限は5000ドルです", prompt["user"])
        self.assertEqual({r["rule_type"] for r in s["rules"]}, {"allowed_values", "max"})
        self.assertIn("Globex Corp", next(r for r in s["rules"] if r["rule_type"] == "allowed_values")["values"])
        s = self.confirm_and_check(sid)
        self.assertEqual([v["field"] for v in s["compliance"]["violations"]], ["total_amount"])

    def test_language_detection_and_numerals(self):
        self.assertEqual(governance.sop_language(SOPS["en"][0]), "en")
        for lang in ("es", "fr", "de", "hi"):
            self.assertEqual(governance.sop_language(SOPS[lang][0]), lang)
        self.assertEqual(governance.sop_language(JAPANESE), "other")
        text, n = governance.to_english_phrases("प्रति चालान खर्च सीमा ५००० रुपये है।")
        self.assertIn("5000", text)
        self.assertGreater(n, 0)
        text, _ = governance.to_english_phrases("Der Betrag darf 1.234,50 EUR nicht überschreiten. Zwischen 01.01.2026 und 31.12.2026.")
        self.assertIn("1234.50", text)
        self.assertIn("between 01.01.2026 and 31.12.2026", text)                   # dates keep their dots
        # Spanish with articles ("entre el ... y el ..."), as in the demo video's SOP
        params = [{"key": "invoice_date", "label": "Invoice Date", "type": "date", "value": "2026-09-23"}]
        rules = governance.parse_sop_rules("Las facturas deben tener fecha entre el 01.01.2026 y el 31.12.2026.", params)
        self.assertEqual([(r["rule_type"], r.get("start_date"), r.get("end_date")) for r in rules],
                         [("date_range", "2026-01-01", "2026-12-31")])


if __name__ == "__main__":
    unittest.main()
