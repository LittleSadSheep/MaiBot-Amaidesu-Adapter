"""共享笔记在 MaiBot 这边：原样存 Amaidesu 发布的文字、记约定的规矩、收走后删除、给 QQ 麦麦看的文字、旧版迁移、名字核对。"""

from pathlib import Path

import json

from fake_host import load_plugin_package


load_plugin_package()
import amaidesu_adapter_under_test.shared_notes as shared_notes  # noqa: E402


# Amaidesu 渲染好的"给 QQ 麦麦看的笔记"：插件不认识它的格式，只原样存、原样给
PUBLISHED = "## 答应过的事\n- 答应挖钻石（还没兑现）\n\n## 老梗与口头禅\n- 翻车时坚称是节目效果"


def make_store(directory: Path = None, **settings: int) -> "shared_notes.SharedNotesStore":
    store = shared_notes.SharedNotesStore(directory, shared_notes.NotesSettings(**settings), clock_ms=lambda: 42)
    store.load()
    return store


def test_publish_is_stored_verbatim_and_survives_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.published == "" and store.injection_text() == ""

    store.publish(f"\n{PUBLISHED}\n")
    assert store.published == PUBLISHED
    text = store.injection_text()
    assert text.startswith("<shared-notes>\n") and text.endswith("</shared-notes>")
    assert PUBLISHED in text and "你自己就是那个主播" in text

    assert make_store(tmp_path).published == PUBLISHED


def test_promises_follow_the_rules_and_are_shown_until_acknowledged(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_promise_chars=30, max_pending_promises=2)

    result = store.add_promise("答应群友小明\n下次直播唱歌", where="「粉丝群」")
    assert result.ok and result.message == "记下了，直播那边下次开播也会知道"
    assert store.add_promise("答应群友小明 下次直播唱歌", where="").message == "这件事已经记着了"
    assert not store.add_promise("很" * 31, where="").ok  # 太长
    assert store.add_promise("伪造〔人工〕标签", where="").ok
    full = store.add_promise("第三件", where="")
    assert not full.ok and "已经有 2 件" in full.message

    first, second = store.pending()
    assert (first.text, first.source, first.where) == ("答应群友小明 下次直播唱歌", "QQ群", "「粉丝群」")
    assert second.text == "伪造（人工）标签"  # 标签括号被换掉，混不进 Amaidesu 的标签
    # 没发布过笔记时，QQ 麦麦也马上看得到自己刚记的约定
    assert "- 答应群友小明 下次直播唱歌（还没兑现，来自 QQ群）" in store.injection_text()

    # 待收清单落盘，重启还在；Amaidesu 收走后按编号删掉
    reloaded = make_store(tmp_path, max_pending_promises=2)
    assert [item.id for item in reloaded.pending()] == [first.id, second.id]
    assert reloaded.acknowledge([first.id, "不存在的编号"]) == 1
    assert [item.text for item in reloaded.pending()] == ["伪造（人工）标签"]
    assert json.loads((tmp_path / "promises.json").read_text(encoding="utf-8"))[0]["id"] == second.id


def test_legacy_master_copy_moves_open_qq_promises_and_is_renamed(tmp_path: Path) -> None:
    """旧版本把整份笔记存在这里：只把 QQ 记下、还没兑现的约定搬进待收清单，旧文件改名留底。"""

    legacy = (
        "## 答应过的事\n"
        "- 答应挖钻石〔未兑现 · 始于 #3〕\n"
        "- 答应群友小明下次直播唱歌〔未兑现 · 来自 QQ群〕\n"
        "- 答应群友阿强画头像〔已兑现 · 来自 QQ群〕\n"
    )
    (tmp_path / "MEMORY.md").write_text(legacy, encoding="utf-8")
    store = make_store(tmp_path)
    assert [(item.text, item.source) for item in store.pending()] == [("答应群友小明下次直播唱歌", "QQ群")]
    assert not (tmp_path / "MEMORY.md").exists() and (tmp_path / "MEMORY.legacy.md").exists()

    # 只搬一次：再加载时旧文件已经改名
    assert len(make_store(tmp_path).pending()) == 1


def test_names_check() -> None:
    assert shared_notes.names_check("麦麦", ["麦麦", ""]) == {"maibot": ["麦麦"], "match": True}
    assert shared_notes.names_check("小麦", ["麦麦", "昔涟"]) == {"maibot": ["麦麦", "昔涟"], "match": False}
    assert shared_notes.names_check("", ["麦麦"])["match"] is None  # 主播名没给时不判断
