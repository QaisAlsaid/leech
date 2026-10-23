from leech.core.security.runtime import Security


def security(tmp_path, owners=(1,)):
    return Security(list(owners), store=tmp_path / "opsec.json")


def test_default_delegation_is_ask(tmp_path):
    s = security(tmp_path)
    assert s.delegation_mode(1, "command:todo.add") == "ask"
    assert s.delegation_mode(2, "command:todo.add") == "ask"


def test_allow_is_explicit_and_persisted(tmp_path):
    s = security(tmp_path)
    s.allow_delegation(2, "command:todo.add")
    assert s.delegation_mode(2, "command:todo.add") == "allow"
    reloaded = Security([1], store=tmp_path / "opsec.json")
    assert reloaded.delegation_mode(2, "command:todo.add") == "allow"


def test_deny_beats_ask(tmp_path):
    s = security(tmp_path)
    s.deny_delegation(2, "command:todo.add")
    assert s.delegation_mode(2, "command:todo.add") == "deny"


def test_ask_resets_the_rule(tmp_path):
    s = security(tmp_path)
    s.allow_delegation(2, "command:todo.add")
    s.ask_delegation(2, "command:todo.add")
    assert s.delegation_mode(2, "command:todo.add") == "ask"
    assert "command:todo.add" not in s.delegations_of(2)


def test_ask_still_passes_the_dispatch_gate(tmp_path):
    # decide_ai gates at the dispatcher: ask must pass (the ai cog does
    # the confirming), only deny blocks.
    s = security(tmp_path)
    assert s.decide_ai(1, "command:todo.add").allowed
    s.deny_delegation(1, "command:todo.add")
    assert not s.decide_ai(1, "command:todo.add").allowed


def test_wildcard_delegation_mode(tmp_path):
    s = security(tmp_path)
    s.allow_delegation(2, "command:todo.*")
    assert s.delegation_mode(2, "command:todo.add") == "allow"
    assert s.delegation_mode(2, "command:mono.play") == "ask"


def test_legacy_false_rows_still_deny(tmp_path):
    # rows written before v10.9 only ever stored False (allow was the
    # implicit default) - they must keep meaning deny.
    s = security(tmp_path)
    s.delegations = {"user:2": {"command:todo.discard": False}}
    assert s.delegation_mode(2, "command:todo.discard") == "deny"
