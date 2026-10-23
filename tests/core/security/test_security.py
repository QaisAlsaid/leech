from leech.core.security.runtime import Security
from leech.core.store.json_store import JsonStore


def security(tmp_path, owners=(1,), grants=(), delegations=None):
    s = Security(list(owners), store=JsonStore(tmp_path / "store"))
    s.grants = list(grants)
    s.delegations = dict(delegations or {})
    return s


def test_owner_passes_everything(tmp_path):
    assert security(tmp_path).decide_user(1, "command:todo.discard").allowed


def test_stranger_denied_by_default(tmp_path):
    assert not security(tmp_path).decide_user(2, "command:todo.add").allowed


def test_grant_prefix_scope(tmp_path):
    s = security(tmp_path)
    s.set_grant(2, "command:todo.*", True)
    assert s.decide_user(2, "command:todo.add").allowed
    assert not s.decide_user(2, "command:userphone.call").allowed


def test_deny_beats_allow_on_tie(tmp_path):
    s = security(tmp_path)
    s.set_grant(2, "command:todo.add", True)
    s.set_grant(2, "command:todo.add", False)
    assert not s.decide_user(2, "command:todo.add").allowed


def test_ai_never_exceeds_user(tmp_path):
    s = security(tmp_path, owners=())
    s.set_grant(2, "command:todo.*", True)
    assert s.decide_ai(2, "command:todo.add").allowed
    assert not s.decide_ai(2, "command:userphone.call").allowed


def test_delegation_deny_scopes(tmp_path):
    s = security(tmp_path, owners=())
    s.set_grant(2, "command:todo.*", True)
    s.deny_delegation(2, "command:todo.discard")
    assert not s.decide_ai(2, "command:todo.discard").allowed
    assert s.decide_ai(2, "command:todo.add").allowed


def test_ai_cannot_touch_auth_or_sudo_even_as_owner(tmp_path):
    s = security(tmp_path)  # principal is the OWNER
    assert not s.decide_ai(1, "command:auth.allow").allowed
    assert not s.decide_ai(1, "command:sudo").allowed


def test_message_gate_scope(tmp_path):
    s = security(tmp_path)
    s.set_grant(2, "message:*", True)
    assert s.decide_user(2, "message:*").allowed
    assert not s.decide_user(2, "command:todo.add").allowed  # chat != commands
