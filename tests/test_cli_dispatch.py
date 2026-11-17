"""Every documented subcommand must actually be dispatched."""
from __future__ import annotations

import sys

import pytest

import jobradar.__main__ as main_mod

COMMANDS = [
    ("run", []), ("loop", []), ("bot", []), ("service", []), ("stats", []),
    ("rescore", []), ("explain", ["some-job-id"]), ("test-telegram", []),
    ("probe", []), ("report", []), ("chat-id", []),
]


@pytest.mark.parametrize("name,args", COMMANDS)
def test_subcommand_is_dispatched(monkeypatch, name, args):
    called = []
    for cmd in ("cmd_run", "cmd_loop", "cmd_bot", "cmd_service", "cmd_stats", "cmd_rescore",
                "cmd_explain", "cmd_probe", "cmd_report", "cmd_test_telegram", "cmd_chat_id"):
        monkeypatch.setattr(main_mod, cmd, lambda *a, _cmd=cmd, **kw: called.append(_cmd))
    monkeypatch.setattr(main_mod, "load_settings", lambda *a, **kw: object())
    monkeypatch.setattr(sys, "argv", ["jobradar", name, *args])
    main_mod.main()
    assert called == ["cmd_" + name.replace("-", "_")]
