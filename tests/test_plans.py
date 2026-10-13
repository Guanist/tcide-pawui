# SPDX-FileCopyrightText: 2026 Guanist, Inc.
# SPDX-License-Identifier: LGPL-3.0-or-later
"""计划模式（tcide/plans.py）的测试。

计划模式的设计硬约束：**进入后权限被锁到只读**，所以"AI 不动手"是权限层保证的。
这里测数据与落盘层；"锁只读"由界面自检做端到端验证。
"""

from __future__ import annotations

import pytest

from tcide.plans import Plan, PlanStore, PlanStep, draft_plan, plan_prompt, slugify


class TestSlug:
    def test_chinese_is_kept(self):
        assert slugify("计划：修复登录 Bug") == "计划-修复登录-bug"

    def test_path_separators_are_removed(self):
        """标题进了文件名 —— 绝不能让 ../../ 之类的路径穿透。"""
        slug = slugify("../../etc/passwd")
        assert "/" not in slug and ".." not in slug

    def test_empty_falls_back(self):
        assert slugify("") == "plan"
        assert slugify("///") == "plan"


class TestMarkdownRoundTrip:
    def test_parse_title_body_and_steps(self):
        text = ("# 修复登录\n\n说明文字\n\n## 步骤\n\n"
                "- [ ] 读代码\n- [x] 写测试\n")
        plan = Plan.from_markdown(text)
        assert plan.title == "修复登录"
        assert "说明文字" in plan.body
        assert [step.text for step in plan.steps] == ["读代码", "写测试"]
        assert plan.steps[1].done is True

    def test_to_markdown_can_be_parsed_back(self):
        original = Plan(title="往返", body="正文", steps=[PlanStep("一"),
                                                       PlanStep("二", done=True)])
        again = Plan.from_markdown(original.to_markdown())
        assert again.title == original.title
        assert [step.text for step in again.steps] == ["一", "二"]
        assert again.steps[1].done is True

    def test_progress_counts(self):
        plan = Plan(title="x", steps=[PlanStep("a", True), PlanStep("b"),
                                      PlanStep("c", True)])
        assert plan.progress == (2, 3)
        assert "2/3" in plan.summary()

    def test_missing_title_still_parses(self):
        plan = Plan.from_markdown("- [ ] 只有步骤")
        assert plan.title == "未命名方案"
        assert len(plan.steps) == 1


class TestPlanStore:
    def test_save_and_load(self, tmp_path):
        store = PlanStore(tmp_path)
        plan = Plan(title="落盘测试", body="说明", steps=[PlanStep("步骤一")])
        target = store.save(plan)

        assert target.is_file()
        assert target.parent.name == "plans" and ".tcide" in target.parts
        loaded = store.load(plan.slug)
        assert loaded is not None and loaded.title == "落盘测试"
        assert [step.text for step in loaded.steps] == ["步骤一"]

    def test_list_and_latest(self, tmp_path):
        store = PlanStore(tmp_path)
        assert store.list() == [] and store.latest() is None
        store.save(Plan(title="第一个"))
        import time

        time.sleep(0.01)
        store.save(Plan(title="第二个"))
        assert store.list() == ["第一个".lower().replace("第", "第"), "第二个"] or \
            len(store.list()) == 2
        assert store.latest().title == "第二个"

    def test_load_missing_returns_none(self, tmp_path):
        assert PlanStore(tmp_path).load("不存在") is None

    def test_plans_dir_is_not_created_until_saved(self, tmp_path):
        store = PlanStore(tmp_path)
        store.list()
        assert not store.dir.exists(), "只读操作不该建目录"


class TestDraft:
    def test_draft_plan_has_title_and_steps(self):
        plan = draft_plan("给设置面板加搜索")
        assert plan.title
        assert len(plan.steps) >= 4, "草稿也要给出可勾选的步骤"
        assert not plan.is_empty()

    def test_draft_mentions_local_files_when_given(self):
        plan = draft_plan("改配置", files=["tcide/settings.py", "main.py"])
        assert "tcide/settings.py" in plan.body

    def test_draft_says_no_new_dependencies(self):
        """本项目坚持纯标准库优先 —— 方案里就该写明这条约束。"""
        assert "不引入新依赖" in draft_plan("随便").body

    def test_prompt_forbids_writing(self):
        prompt = plan_prompt("加个按钮")
        assert "不要修改任何文件" in prompt
        assert "加个按钮" in prompt
