"""8.5: the setup repair agent - Gemini stays on a failed tool setup (read-only checks, a pre-checked fix, the admin
approves, Understudy applies and re-runs, the agent continues with the full history) instead of one guess per failure."""
import json
import os
import unittest
from unittest import mock

from tests.test_v83_fixes import REQ_STACK_PYTEST, SMOKE, TestAutomaticFixInTheSetup
from tests.wizard_helpers import FakeLLM
from core.wizard import admin_policy as ap, setup_agent, tool_setup
from core.wizard.frameworks import FrameworkSpec, Package
from core.wizard.sandbox.base import ExecResult

GOOD_TEST = ("import os\nimport requests\nr = requests.get(os.environ['SMOKE_API_URL'] + '/ping.json')\n"
             "assert r.json()\nprint('UNDERSTUDY_SMOKE_OK')\n")
ODD = ExecResult("python run_api.py", 1, "Traceback (most recent call last):\nKeyError: 'pong'")
OK = ExecResult("python run_api.py", 0, "UNDERSTUDY_SMOKE_OK")


def steps(*replies):
    """FakeLLM responder that returns the agent's replies in order (the last one repeats)."""
    state = {"i": 0}

    def reply(system, user, media):
        r = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return r(user) if callable(r) else r
    return reply


CHECK = {"thought": "Let me see what is installed.", "action": "run_checks", "checks": ["installed_packages"]}
FIX = {"thought": "The test reads a key the API doesn't return.", "action": "propose_fix",
       "diagnosis": "KeyError: 'pong' - the ping endpoint has no 'pong' key.",
       "fix": {"summary": "Rewrote the test to check the JSON body instead of a 'pong' key.", "confidence": "high",
               "smoke_files": [{"path": "run_api.py", "content": GOOD_TEST}]}}
OBS = [{"check": "installed_packages", "label": "python -m pip", "ok": True, "exit_code": 0, "output": "requests==2.32.3"}]


class AgentBase(TestAutomaticFixInTheSetup):
    def fail_first(self, rid, results):
        with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(r, {}) for r in results]):
            self.admin.post(f"/api/admin/tool-requests/{rid}/smoke/approve?wait=true")
        return ap.get_request(rid)

    def fake(self, agent):
        return FakeLLM({"TOOL_STACK_SUGGEST": REQ_STACK_PYTEST, "TOOL_SMOKE_TEST": SMOKE,
                        "TOOL_SETUP_PLAN": RuntimeError("down"), "TOOL_SETUP_CLARIFY": RuntimeError("down"),
                        "TOOL_SETUP_RESOLVE": RuntimeError("down"), "SETUP_AGENT_STEP": agent})


class TestAgentLoop(AgentBase):
    def test_investigates_proposes_waits_for_approval_and_finishes(self):
        fake = self.fake(steps(CHECK, FIX))
        rid = self._to_review(fake)
        with mock.patch.object(setup_agent, "run_checks", side_effect=[OBS, []]) as rc:
            r = self.fail_first(rid, [ODD])
        self.assertEqual(r["status"], "awaiting_admin")                    # nothing applied by itself
        q = r["clarification"]
        self.assertEqual(q["category"], "agent")
        first = q["options"][0]
        self.assertEqual(first["id"], "apply_agent_fix")
        self.assertEqual(first["changes"]["smoke_files"]["run_api.py"], GOOD_TEST)
        self.assertEqual(q["agent"]["steps"][0]["kind"], "checks")         # it looked before it proposed
        self.assertEqual(rc.call_args_list[0][0][0], ["installed_packages"])
        self.assertIn("requests==2.32.3", fake.called("SETUP_AGENT_STEP")[1]["user"])
        self.assertIn("KeyError: 'pong'", fake.called("SETUP_AGENT_STEP")[0]["user"])
        self.assertTrue(any(m.get("kind") == "agent_step" for m in r["conversation"]))
        with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(OK, {})]) as run:
            tool_setup.answer(rid, "boss", option="apply_agent_fix", background=False)
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "approved", r.get("clarification"))
        self.assertEqual(run.call_args[0][2]["run_api.py"], GOOD_TEST)       # exactly the test the admin saw
        self.assertEqual(r["setup"]["smoke_review"]["approved_by"], "boss")
        turn = r["setup"]["agent"]["turns"][-1]
        self.assertEqual((turn["outcome"], turn["result"]), ("applied", "the setup passed"))
        lessons = ap.setup_lessons()
        self.assertEqual(len(lessons), 1)
        self.assertIn("JSON body", lessons[0]["summary"])

    def test_a_fix_that_fails_the_precheck_goes_back_to_the_agent(self):
        bad = json.loads(json.dumps(FIX))
        bad["fix"]["smoke_files"] = [{"path": "run_api.py", "content": "print('no url used')\n"}]
        fake = self.fake(steps(bad, FIX))
        rid = self._to_review(fake)
        with mock.patch.object(setup_agent, "run_checks", return_value=[]):
            r = self.fail_first(rid, [ODD])
        calls = fake.called("SETUP_AGENT_STEP")
        self.assertEqual(len(calls), 2)
        self.assertIn("REJECTED", calls[1]["user"])
        self.assertIn("SMOKE_URL", calls[1]["user"])
        self.assertEqual(r["clarification"]["options"][0]["changes"]["smoke_files"]["run_api.py"], GOOD_TEST)

    def test_still_failing_after_the_fix_the_agent_continues_with_history(self):
        fake = self.fake(steps(FIX, FIX, lambda user: {"thought": "x", "action": "ask_admin", "question": "Q?"}))
        rid = self._to_review(fake)
        with mock.patch.object(setup_agent, "run_checks", return_value=[]):
            self.fail_first(rid, [ODD])
            with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(ODD, {})]), \
                    mock.patch.object(setup_agent, "_call", wraps=setup_agent._call) as call:
                tool_setup.answer(rid, "boss", option="apply_agent_fix", background=False)
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "awaiting_admin")
        turns = r["setup"]["agent"]["turns"]
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["outcome"], "applied")
        self.assertIn("failed again", turns[0]["result"])
        self.assertTrue(turns[1]["repeated"])                             # same error after the fix
        self.assertEqual({c[0][1] for c in call.call_args_list}, {None})   # 8.5.1: the model is NOT switched by itself
        last_prompt = fake.called("SETUP_AGENT_STEP")[-1]["user"]
        self.assertIn("REPEATED: yes", last_prompt)
        self.assertIn("agent turn 1", last_prompt)                         # it sees what it already tried
        self.assertIn("NEVER propose a fix that was already tried", fake.called("SETUP_AGENT_STEP")[-1]["system"])
        offer = next(o for o in r["clarification"]["options"] if o["id"] == "agent_retry_stronger")
        self.assertIn("gemini-pro-latest", offer["label"])               # ...it is offered to the admin
        with mock.patch.object(setup_agent, "run_checks", return_value=[]), \
                mock.patch.object(setup_agent, "_call", wraps=setup_agent._call) as call:
            tool_setup.answer(rid, "boss", option="agent_retry_stronger", background=False)
        self.assertEqual({c[0][1] for c in call.call_args_list}, {"gemini-pro-latest"})   # the admin's choice
        r = ap.get_request(rid)
        self.assertEqual(r["setup"]["agent"]["turns"][-1]["requested_model"], "gemini-pro-latest")
        self.assertFalse(any(o["id"] == "agent_retry_stronger" for o in r["clarification"]["options"]))

    def test_look_again_with_the_admins_hint(self):
        fake = self.fake(steps(FIX, FIX))
        rid = self._to_review(fake)
        with mock.patch.object(setup_agent, "run_checks", return_value=[]):
            self.fail_first(rid, [ODD])
            tool_setup.answer(rid, "boss", text="the endpoint is /api/ping.json", option="agent_retry", background=False)
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "awaiting_admin")
        turns = r["setup"]["agent"]["turns"]
        self.assertEqual(turns[0]["outcome"], "rejected by the admin")
        self.assertIn("the endpoint is /api/ping.json", fake.called("SETUP_AGENT_STEP")[-1]["user"])
        self.assertEqual(r["setup"]["round"], 1)                          # looking again isn't a new round

    def test_stop_is_respected(self):
        rid = self._to_review(self.fake(steps(FIX)))
        with mock.patch.object(setup_agent, "run_checks", return_value=[]):
            self.fail_first(rid, [ODD])
        tool_setup.answer(rid, "boss", option="stop", background=False)
        self.assertEqual(ap.get_request(rid)["status"], "setup_failed")


class TestAgentSettings(AgentBase):
    def test_disabled_falls_back_to_the_one_shot_question(self):
        ap.set_setup_agent(enabled=False)
        fake = self.fake(steps(FIX))
        rid = self._to_review(fake)
        r = self.fail_first(rid, [ODD])
        self.assertEqual(r["status"], "awaiting_admin")
        self.assertEqual(fake.called("SETUP_AGENT_STEP"), [])
        self.assertNotEqual(r["clarification"].get("category"), "agent")

    def test_known_rule_first_then_agent(self):
        fake = self.fake(steps(FIX))
        rid = self._to_review(fake)
        r = self.fail_first(rid, [ExecResult("python run_api.py", 1, "ModuleNotFoundError: No module named 'pytest'")])
        self.assertEqual(r["clarification"]["options"][0]["id"], "apply_ai_fix")    # Understudy's certain fix
        self.assertEqual(fake.called("SETUP_AGENT_STEP"), [])

    def test_automatic_mode_applies_package_fixes_but_never_a_new_test(self):
        ap.set_setup_fix_approval("automatic")
        pkg = {"thought": "needs urllib3", "action": "propose_fix", "fix": {"summary": "add urllib3",
               "changes": {"add_packages": [{"name": "urllib3", "version": ""}]}}}
        rid = self._to_review(self.fake(steps(pkg)))
        with mock.patch.object(setup_agent, "run_checks", return_value=[]):
            r = self.fail_first(rid, [ODD, OK])
        self.assertEqual(r["status"], "approved", r.get("clarification"))
        self.assertEqual(r["setup"]["auto_fixes"][-1]["source"], "agent")
        rid2 = self._to_review(self.fake(steps(FIX)))
        with mock.patch.object(setup_agent, "run_checks", return_value=[]):
            r2 = self.fail_first(rid2, [ODD])
        self.assertEqual(r2["status"], "awaiting_admin")                    # a new test needs the admin

    def test_settings_api_and_escalation_model(self):
        r = self.admin.put("/api/admin/setup-agent", json={"escalation_model": "gemini-2.5-pro"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["settings"]["setup_agent"]["escalation_model"], "gemini-2.5-pro")
        self.assertEqual(setup_agent.escalation_model(), "gemini-2.5-pro")
        self.assertIsNone(setup_agent._model_for(True))                  # never automatic
        self.assertEqual(setup_agent._model_for(True, "gemini-2.5-pro"), "gemini-2.5-pro")
        ap.set_setup_agent(escalation_model="")
        self.assertIsNone(setup_agent.escalation_model())
        self.assertEqual(self.admin.put("/api/admin/setup-agent", json={"escalation_model": "x; rm"}).status_code, 400)


class TestChecks(unittest.TestCase):
    def spec(self, eco="pip", entry="smoke.robot", run=("robot", "{entry}")):
        return FrameworkSpec(id="t", name="T", language="python", ecosystem=eco, packages=[Package("robotframework", "7")],
                             run_command=list(run), entry_file=entry)

    def test_check_commands_are_fixed_and_validated(self):
        files = {"smoke.robot": "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Fill Text    a    b\n"}
        runs, _ = setup_agent._argv_for("test_dry_run", self.spec(), files)
        self.assertIn("--dryrun", runs[0][1])
        runs, _ = setup_agent._argv_for("robot_keywords", self.spec(), files)
        self.assertEqual(runs[0][1][3], "Browser")
        self.assertIn("fill text", runs[0][1])
        for bad in ("signature:os;rm -rf /", "import:__import__('os').system('x')", "exports:../evil", "rm -rf /"):
            runs, problem = setup_agent._argv_for(bad, self.spec(), files)
            self.assertIsNone(runs, bad)
            self.assertTrue(problem)

    def test_signature_check_really_runs(self):
        spec = FrameworkSpec(id="requests-api", name="Requests", language="python", ecosystem="pip",
                             packages=[Package("requests", "")], run_command=["python", "{entry}"], entry_file="a.py")
        with mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "dry-run"}):
            got = setup_agent.run_checks(["signature:json.dumps", "test_dry_run"], spec,
                                         {"env_vars": {}, "path_prepend": []}, {"a.py": "print(1)\n"}, "T1")
        sig = next(g for g in got if g["check"].startswith("signature"))
        self.assertIn("json.dumps(obj", sig["output"])
        dry = next(g for g in got if g["check"] == "test_dry_run")
        self.assertTrue(dry["ok"], dry)

    def test_signature_of_a_failure(self):
        a = {"stage": "smoke_test", "output": "Keyword 'Browser.Fill Text' expected 2 to 3 arguments, got 0."}
        b = {"stage": "smoke_test", "output": "x\nKeyword 'Browser.Fill Text' expected 2 to 3 arguments, got 1."}
        self.assertEqual(setup_agent.signature(a), setup_agent.signature(b))
        self.assertIn("got 0", setup_agent.error_line(a))


if __name__ == "__main__":
    unittest.main()
