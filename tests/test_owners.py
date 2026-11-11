# -*- coding: utf-8 -*-
"""owners 名单：环境变量 + owners.json 取并集，改完即时生效。

对应 2026-11-11 那次断链：环境变量只在进程启动时读一次，注册表里补进去的值
要等下一次重启才认得，中间整天的写入全被吞掉。这里锁住兜底行为。
"""
import json
import os
import sys

import pytest

pytest.importorskip("astrbot.api")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("JJ_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("JJ_MEMORY_OWNERS", "123:FriendMessage:1")
    sys.path.insert(0, PLUGIN_DIR)
    sys.modules.pop("main", None)
    import main

    yield main, tmp_path
    sys.modules.pop("main", None)


def test_env_only_when_no_file(mod):
    main, _ = mod
    assert main._load_owners() == frozenset({"123:FriendMessage:1"})


def test_file_merges_with_env(mod):
    main, tmp = mod
    (tmp / "owners.json").write_text(
        json.dumps({"owners": ["123:FriendMessage:2", " 123:GroupMessage:9 "]}),
        encoding="utf-8")
    assert main._load_owners() == frozenset({
        "123:FriendMessage:1", "123:FriendMessage:2", "123:GroupMessage:9"})


def test_file_edit_takes_effect_without_restart(mod):
    main, tmp = mod
    f = tmp / "owners.json"
    f.write_text(json.dumps(["123:FriendMessage:2"]), encoding="utf-8")
    assert "123:FriendMessage:2" in main._load_owners()
    f.write_text(json.dumps(["123:FriendMessage:3"]), encoding="utf-8")
    owners = main._load_owners()
    assert "123:FriendMessage:3" in owners
    assert "123:FriendMessage:2" not in owners


@pytest.mark.parametrize("payload", ["{bad json", json.dumps({"owners": "nope"})])
def test_broken_file_falls_back_to_env(mod, payload):
    main, tmp = mod
    (tmp / "owners.json").write_text(payload, encoding="utf-8")
    assert main._load_owners() == frozenset({"123:FriendMessage:1"})


def test_is_owner_by_umo(mod):
    main, tmp = mod

    class E:
        unified_msg_origin = "123:FriendMessage:1"

    class Other:
        unified_msg_origin = "123:GroupMessage:148529996"

    assert main._is_owner(E()) is True
    assert main._is_owner(Other()) is False
