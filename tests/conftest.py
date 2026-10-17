"""Shared test configuration.

Since 6.3 self-healing asks the user to approve every fix by default (admin setting "ask"). The suites written for
the automatic loop (self-healing, e2e, telemetry, recall, ...) run with the documented operator override
UNDERSTUDY_SELF_HEAL_APPROVAL=automatic; tests/test_heal_approval.py covers the "ask" mode explicitly.
"""
import os

os.environ.setdefault("UNDERSTUDY_SELF_HEAL_APPROVAL", "automatic")

# 9.7.1: the backend generates the Mission Control password on its first start and saves it in .env. Tests run with
# the documented fallback credentials and must never write the project's .env.
os.environ.setdefault("UNDERSTUDY_ADMIN_PASSWORD_GENERATE", "off")

# Since 7.0 every Studio call needs a user login (accounts are created by the admin). The older suites exercise the
# pipeline itself and run with the documented single-user switch UNDERSTUDY_USER_LOGIN=off;
# tests/test_users_projects_history.py covers login, ownership and the project hierarchy with login required.
os.environ.setdefault("UNDERSTUDY_USER_LOGIN", "off")

# Since 7.2 a fresh installation has no approved tools (the admin adds them with the AI setup). The suites written
# before that start from the catalog tools that have offline templates - the documented operator setting
# UNDERSTUDY_DEFAULT_TOOLS=catalog; tests/test_tool_setup.py covers the empty default and the AI setup flow.
os.environ.setdefault("UNDERSTUDY_DEFAULT_TOOLS", "catalog")
# 9.9: the downloadable runtime versions come from the official sources - tests use the built-in list (no network)
os.environ.setdefault("UNDERSTUDY_RUNTIME_CATALOG", "offline")

# 8.1: email (OTP codes, account notices). Tests never send real mail: the outbox file lives in a temporary folder,
# and tests/test_email_accounts.py captures messages with mailer.set_transport.
import tempfile  # noqa: E402
os.environ.setdefault("UNDERSTUDY_MAIL_MODE", "outbox")
os.environ.setdefault("UNDERSTUDY_MAIL_OUTBOX", os.path.join(tempfile.mkdtemp(prefix="understudy-mail-"), "outbox.jsonl"))
