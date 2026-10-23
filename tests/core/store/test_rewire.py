"""v11 - the great rewire: every cog rides the store.

pins, per rewired service:
- round-trips through the json backend (same shape the migrator writes)
- the never-wipe guard: a failed load refuses to save for that session
- migrate_persistent --only top-ups touch ONLY the named namespaces
"""

import json
import os
import sys
import types
from pathlib import Path

# import order: cogs -> core.cog.context -> leech.leech -> config, so
# leech.leech must be imported FIRST (with stub env) to break the cycle.
os.environ.setdefault("BOT_ID", "1")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("AI_CHAIN", "")

import leech.leech  # noqa: E402, F401

from leech.core.store import StoreError
from leech.core.store.json_store import JsonStore


def _store(tmp_path):
    return JsonStore(tmp_path / "store")


# -- security ---------------------------------------------------------------------


def test_security_round_trip(tmp_path):
    from leech.core.security.runtime import Security

    s = Security([1], store=_store(tmp_path))
    s.set_grant(2, "command:todo.*", True)
    s.set_grant(2, "command:todo.discard", False)
    s.allow_delegation(2, "command:mono.play")
    s.deny_delegation(3, "command:todo.add")

    re = Security([1], store=_store(tmp_path))
    assert {(g.actor, g.scope, g.allow) for g in re.grants} == {
        ("user:2", "command:todo.*", True),
        ("user:2", "command:todo.discard", False),
    }
    assert re.decide_user(2, "command:todo.add").allowed
    assert not re.decide_user(2, "command:mono.play").allowed
    assert re.delegation_mode(2, "command:mono.play") == "allow"
    assert re.delegation_mode(3, "command:todo.add") == "deny"
    assert re.delegation_mode(4, "command:todo.add") == "ask"


def test_security_clear_grant_removes_the_row(tmp_path):
    from leech.core.security.runtime import Security

    s = Security([1], store=_store(tmp_path))
    s.set_grant(2, "command:todo.*", True)
    s.clear_grant(2, "command:todo.*")
    assert _store(tmp_path).items("security_grants") == {}

    re = Security([1], store=_store(tmp_path))
    assert not re.decide_user(2, "command:todo.add").allowed


def test_failed_load_never_writes(tmp_path):
    from leech.core.security.runtime import Security

    class DeadStore(JsonStore):
        def items(self, ns):
            raise StoreError("store down")

        def put(self, ns, key, value):
            raise AssertionError("a broken session must never write")

    s = Security([1], store=DeadStore(tmp_path / "dead"))
    assert s._broken
    s.set_grant(2, "command:todo.*", True)  # must neither raise nor write


# -- command templates ---------------------------------------------------------


def test_template_rows_match_template_codec(tmp_path):
    # one row per template keyed by name; the row round-trips through
    # the same codec the loader uses.
    from leech.cogs.command.cog import (
        InvokeAlias,
        NS_TEMPLATES,
        template_from_dict,
        template_to_dict,
    )
    from leech.core.provider.model import ProviderType

    alias = InvokeAlias(
        name="music.play",
        type=ProviderType.SLASH,
        bot=None,
        target=["music", "play"],
        arguments=[],
        defaults={},
    )
    store = _store(tmp_path)
    store.put(NS_TEMPLATES, alias.name, template_to_dict(alias))

    loaded = template_from_dict(store.get(NS_TEMPLATES, "music.play"))
    assert loaded.name == "music.play"
    assert loaded.type == ProviderType.SLASH
    assert loaded.target == ["music", "play"]


def test_command_cog_save_diffs_the_rows(tmp_path):
    # _save_templates puts every current template and deletes store rows
    # that are no longer wanted - through the real method.
    from leech.cogs.command.cog import (
        CommandCog,
        InvokeAlias,
        NS_TEMPLATES,
        template_to_dict,
    )
    from leech.core.provider.model import ProviderType

    def alias(name):
        return InvokeAlias(
            name=name,
            type=ProviderType.SELF,
            bot=None,
            target=[name],
            arguments=[],
            defaults={},
        )

    store = _store(tmp_path)
    store.put(NS_TEMPLATES, "gone", template_to_dict(alias("gone")))

    fake = types.SimpleNamespace(
        invoke_aliases={},
        _store=store,
        _templates_broken=False,
    )
    CommandCog._save_templates(fake)
    assert store.items(NS_TEMPLATES) == {}

    fake.invoke_aliases["a.b"] = alias("a.b")
    CommandCog._save_templates(fake)
    assert set(store.items(NS_TEMPLATES)) == {"a.b"}


# -- ai config / personas / audit log ------------------------------------------


def test_ai_service_config_and_personas_round_trip(tmp_path):
    from leech.core.ai.service import AiService

    store = _store(tmp_path)
    ai = AiService(store=store, backend="manager")
    ai.set_persona(55, "you are terse")
    ai.activate(55)
    ai.mute(99)
    ai.set_window_budget(9_000)

    re = AiService(store=store, backend="manager")
    assert re.personas[55] == "you are terse"
    assert 55 in re.active
    assert 99 in re.muted
    assert re._window_budget == 9_000
    assert store.get_doc("ai_config") is not None


def test_ai_persona_delete_propagates(tmp_path):
    from leech.core.ai.service import AiService

    store = _store(tmp_path)
    ai = AiService(store=store, backend="manager")
    ai.set_persona(55, "you are terse")
    assert store.items("ai_personas") == {"55": "you are terse"}
    ai.clear_persona(55)
    assert store.items("ai_personas") == {}


def test_action_log_rides_the_store(tmp_path):
    from leech.core.ai.audit import ActionLog

    log = ActionLog(store=_store(tmp_path))
    log.write(kind="run", principal=1, command="todo.add")
    log.write(kind="denied", principal=2, command="sudo.pull")

    lines = (tmp_path / "store" / "ai_log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "run"
    assert first["via"] == "ai"
    assert first["principal"] == 1


# -- mono / lrc / ytdlp --------------------------------------------------------


def test_mono_prefs_round_trip(tmp_path):
    from leech.cogs.mono.service import MonoService

    svc = MonoService(store=_store(tmp_path))
    svc.set_config(7, "album_zip", "off")

    re = MonoService(store=_store(tmp_path))
    assert re.config(7)["album_zip"] == "off"
    assert re.config(8)["album_zip"] == "on"  # default untouched


def test_lrc_prefs_round_trip(tmp_path):
    from leech.cogs.lrc.cog import _LrcConfig

    cfg = _LrcConfig(store=_store(tmp_path))
    assert cfg.album_zip(7) is True
    cfg.set_album_zip(7, "off")

    re = _LrcConfig(store=_store(tmp_path))
    assert re.album_zip(7) is False
    assert re.album_zip(8) is True


def test_ytdlp_prefs_round_trip(tmp_path):
    from leech.cogs.ytdlp.cog import NS_YT_PREFS, _PrefStore

    prefs = _PrefStore(store=_store(tmp_path))
    assert prefs.get("lane") == ""
    prefs.set("lane", "hub")

    assert prefs.get("lane") == "hub"
    assert _store(tmp_path).get_doc(NS_YT_PREFS) == {"lane": "hub"}


# -- migrator -------------------------------------------------------------------


def _migrator():
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "scripts" / "migrate_persistent.py"
    spec = importlib.util.spec_from_file_location("migrate_persistent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_apply_only_filters_and_reports_typos():
    mod = _migrator()
    kv = {"todo": {"1": {}}, "command_templates": {"a": {}}}
    docs = {"ai_config": {}, "lrc_config": {}}
    logs = [{"x": 1}]

    kv2, docs2, logs2, unknown = mod.apply_only(
        kv, docs, logs, "command_templates,lrc_config"
    )
    assert kv2 == {"command_templates": {"a": {}}}
    assert docs2 == {"lrc_config": {}}
    assert logs2 == []
    assert unknown == []

    _, _, _, unknown = mod.apply_only(kv, docs, logs, "command_templates,typo")
    assert unknown == ["typo"]

    kv2, docs2, logs2, _ = mod.apply_only(kv, docs, logs, "ai_log")
    assert kv2 == {} and docs2 == {} and logs2 == logs

    kv2, docs2, logs2, _ = mod.apply_only(kv, docs, logs, None)
    assert kv2 is kv and docs2 is docs and logs2 is logs


def test_migrator_only_top_up_never_touches_live_rows(tmp_path, monkeypatch):
    # THE v11 scenario: todo already lives on the store (and moved ahead
    # of v1), a command was added to v1, and the audit log already rode
    # over. re-running with --only must import the new template and
    # nothing else.
    mod = _migrator()

    v1 = tmp_path / "v1"
    (v1 / "commands").mkdir(parents=True)
    (v1 / "todo").mkdir(parents=True)
    (v1 / "ai").mkdir(parents=True)
    (v1 / "commands" / "templates.json").write_text(
        json.dumps({"templates": [{"name": "old", "type": "self"}]})
    )
    (v1 / "todo" / "todo.json").write_text(json.dumps({"1": {"todo": {}}}))
    (v1 / "ai" / "actions.jsonl").write_text(json.dumps({"kind": "run"}) + "\n")

    target = tmp_path / "target"
    monkeypatch.setenv("LEECH_STORE", "json")
    monkeypatch.setenv("LEECH_STORE_DIR", str(target))
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "")
    monkeypatch.setenv("SUPABASE_KEY", "")

    monkeypatch.setattr(sys, "argv", ["migrate", "--from", str(v1)])
    assert mod.main() == 0

    store = JsonStore(target)
    assert set(store.items("command_templates")) == {"old"}
    assert (target / "ai_log.jsonl").exists()

    # the bot goes live on the store; todo moves ahead of v1:
    store.put("todo", "1", {"todo": {}, "live": True})
    # ...and a command was added to v1 after the first migration:
    (v1 / "commands" / "templates.json").write_text(
        json.dumps(
            {
                "templates": [
                    {"name": "old", "type": "self"},
                    {"name": "newcmd", "type": "self"},
                ]
            }
        )
    )
    log_lines_before = len((target / "ai_log.jsonl").read_text().splitlines())

    monkeypatch.setattr(
        sys, "argv", ["migrate", "--from", str(v1), "--only", "command_templates"]
    )
    assert mod.main() == 0

    assert set(store.items("command_templates")) == {"old", "newcmd"}
    assert store.get("todo", "1") == {"todo": {}, "live": True}
    assert len((target / "ai_log.jsonl").read_text().splitlines()) == log_lines_before


def test_migrator_collects_ytdlp_prefs(tmp_path):
    mod = _migrator()
    v1 = tmp_path / "v1"
    (v1 / "ytdlp").mkdir(parents=True)
    (v1 / "ytdlp" / "prefs.json").write_text(json.dumps({"lane": "hub"}))

    kv, docs, logs = mod.collect(v1)
    assert docs == {"ytdlp_prefs": {"lane": "hub"}}
    assert kv == {} and logs == []
