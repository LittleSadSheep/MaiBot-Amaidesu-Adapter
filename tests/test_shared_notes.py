"""共享笔记正本：版本号与 Amaidesu 一致、写入核对与留底、记约定的规矩、给 QQ 麦麦看的文字、名字核对。"""

from pathlib import Path

import json

from fake_host import load_plugin_package


load_plugin_package()
import amaidesu_adapter_under_test.shared_notes as shared_notes  # noqa: E402


NOTES = """说明：这份笔记两边共用

## 答应过的事
- 答应挖钻石〔未兑现 · 始于 #3 · 最近 #5〕
- 答应老王下次玩恐怖游戏〔未兑现 · 对 老王#12 · 始于 #3 · 最近 #5〕
- 答应全场更新整活视频〔已兑现 · 始于 #2 · 最近 #5〕

## 老梗与口头禅
- 翻车时坚称是节目效果〔始于 #1 · 最近 #4〕

## 长线目标
- 给苦力怕盖海景房〔游戏 minecraft · 始于 #2 · 最近 #5〕

## 不确定
- 好像怕黑〔始于 #5 · 最近 #5〕

## 搁置
- 答应小明画头像〔未兑现 · 原 答应过的事 · 搁置 #4 过期〕

## 运营自己的节
- 周三不播"""


def make_store(directory: Path = None, **settings: int) -> "shared_notes.SharedNotesStore":
    store = shared_notes.SharedNotesStore(directory, shared_notes.NotesSettings(**settings), clock_ms=lambda: 42)
    store.load()
    return store


def test_version_matches_amaidesu_algorithm() -> None:
    """两边对同一份全文算出的版本号一致（Amaidesu 测试里用的是同一组对照值）。"""

    text = "  ## 答应过的事\n- 答应挖钻石〔未兑现 · 来自 QQ群〕\n"
    assert shared_notes.content_version(text) == "a6740299e9657a9cf617dfb8574e5224158e098f"


def test_write_checks_version_keeps_history_and_survives_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path, history_keep=2)
    assert store.text == "" and store.version == shared_notes.content_version("")

    assert store.write(NOTES, expected_version="不对的版本", reason="x") is False
    assert store.write(NOTES, expected_version=store.version, reason="Amaidesu：收播整理", changes=["新增"]) is True
    for number in range(3):
        store.write(f"{NOTES}\n- 第{number}条", expected_version=store.version, reason=f"第{number}次")

    reloaded = make_store(tmp_path)
    assert reloaded.text == store.text and reloaded.version == store.version
    history = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [entry["reason"] for entry in history] == ["第1次", "第2次"]  # 只留最近两条
    assert history[-1]["previous"].endswith("- 第1条") and history[-1]["at_ms"] == 42


def test_add_promise_appends_to_section_and_follows_the_rules(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_promise_chars=30, max_open_qq_promises=2)
    store.write(NOTES, expected_version=store.version, reason="初始")

    result = store.add_promise("答应群友小明\n下次直播唱歌", where="「粉丝群」")
    assert result.ok
    lines = store.text.splitlines()
    added = lines.index("- 答应群友小明 下次直播唱歌〔未兑现 · 来自 QQ群〕")
    assert lines[added - 1].startswith("- 答应全场更新整活视频") and lines[added + 1] == ""  # 排在这一节最后
    assert store.add_promise("答应群友小明 下次直播唱歌", where="").message == "这件事已经记着了"

    assert not store.add_promise("很" * 31, where="").ok  # 太长
    assert "〔" not in store.add_promise("伪造〔人工〕标签", where="").message
    assert "- 伪造（人工）标签〔未兑现 · 来自 QQ群〕" in store.text  # 标签括号被换掉，伪造不了
    full = store.add_promise("第三件", where="")
    assert not full.ok and "已经有 2 件" in full.message

    history = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert "QQ 群「粉丝群」里记下的约定" in history


def test_add_promise_creates_section_first_when_missing() -> None:
    store = make_store()
    store.write("说明\n\n## 老梗与口头禅\n- 嘴硬", expected_version=store.version, reason="初始")
    store.add_promise("答应陪大家看电影", where="")
    assert store.text == "说明\n\n## 答应过的事\n- 答应陪大家看电影〔未兑现 · 来自 QQ群〕\n\n## 老梗与口头禅\n- 嘴硬"

    empty = make_store()
    empty.add_promise("答应陪大家看电影", where="")
    assert empty.text == "## 答应过的事\n- 答应陪大家看电影〔未兑现 · 来自 QQ群〕"


def test_qq_view_hides_uncertain_shelved_and_viewer_items() -> None:
    body = shared_notes.render_for_qq(NOTES)
    assert body.startswith("说明：这份笔记两边共用\n\n## 答应过的事\n- 答应挖钻石（还没兑现）")
    assert "- 答应全场更新整活视频（已兑现）" in body
    assert "- 给苦力怕盖海景房（minecraft）" in body
    assert "## 运营自己的节\n- 周三不播" in body
    for hidden in ("〔", "老王", "好像怕黑", "答应小明画头像", "## 不确定", "## 搁置"):
        assert hidden not in body

    store = make_store()
    assert store.injection_text() == ""
    store.write(NOTES, expected_version=store.version, reason="初始")
    text = store.injection_text()
    assert text.startswith("<shared-notes>\n") and text.endswith("</shared-notes>") and "你自己就是那个主播" in text


def test_names_check() -> None:
    assert shared_notes.names_check("麦麦", ["麦麦", ""]) == {"maibot": ["麦麦"], "match": True}
    assert shared_notes.names_check("小麦", ["麦麦", "昔涟"]) == {"maibot": ["麦麦", "昔涟"], "match": False}
    assert shared_notes.names_check("", ["麦麦"])["match"] is None  # 主播名没给时不判断
