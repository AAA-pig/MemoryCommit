# -*- coding: utf-8 -*-
"""C5：README 版本口径不漂移。

README 是给新用户看的唯一入口，里面写死的版本号一旦和代码分家，
就是最贵的文档债（C5 之前它写着结构 v6 / 备份格式 v4，实际已是 v8 / v6）。
这两条只做一件事：README 里的数字必须等于代码里的常量、两处清单里的版本必须一致。
"""

import os
import re
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)


def _read(name):
    with open(os.path.join(PLUGIN_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def test_readme_version_numbers_match_code():
    import ledger

    text = _read("README.md")
    assert "SCHEMA_VERSION = %d" % ledger.SCHEMA_VERSION in text
    assert "DUMP_VERSION = %d" % ledger.DUMP_VERSION in text
    assert "CORE_API_VERSION = %d" % ledger.CORE_API_VERSION in text
    assert "当前是 **v%d**" % ledger.SCHEMA_VERSION in text


def test_plugin_version_consistent_everywhere():
    pyproject = _read("pyproject.toml")
    metadata = _read("metadata.yaml")
    readme = _read("README.md")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert m, "pyproject.toml 里找不到 version"
    ver = m.group(1)
    m2 = re.search(r"^version:\s*(\S+)", metadata, re.M)
    assert m2, "metadata.yaml 里找不到 version"
    assert m2.group(1).strip().strip('"') == ver, "pyproject 与 metadata 版本不一致"
    assert "插件版本 `%s`" % ver in readme, "README 没写当前插件版本"
