#!/usr/bin/env python3
"""
test_seedance_gen.py —— seedance_gen.py 纯函数单测（不调用任何 API，不发网络请求）。

只测 _extract_per_segment_texts 系列的分时段解析/裁剪逻辑（seedance_gen.py 里最容易
出 bug 的确定性纯函数），用 assert，跑完打印 PASS，不引入 pytest 依赖（对齐仓库其余
部分零外部测试框架依赖的风格）。

用法：
    python3 test_seedance_gen.py
"""

from __future__ import annotations

import seedance_gen

# 这份 fixture 原本直接读取一次真实批跑产出的 brief JSON
# ——一份真实批跑产出、把分时段内容揉进「动作与运动描述：」小节、没有独立
# 「分时段描述：」标题的失败案例。分段路由（segment-level routing）改动的验收
# 要求对同一个文件重跑 classify_llm.py --force + remix_brief.py，重新生成后的
# 新版 brief 已经有合规的「分时段描述：」小节，不再复现原失败模式；而 out/ 是
# 可重跑再生成的产物目录（.gitignore 忽略，不进仓库），不适合再当长期回归
# fixture 用。把当初观察到的失败文本形状（0-5s 子句是"手机屏幕特写展示 Cuddler
# 语音通话来电界面…"）固化成本文件内的静态字符串常量，不再依赖会被后续跑批
# 影响的 out/ 产物——测试意图不变（验证 fallback 能从"分段内容混进动作描述小节、
# 没有独立分时段标题"的真实失败形状里正确裁出子 prompt），只是不再是"活的"
# 文件快照。
SYNTHETIC_FAILING_PROMPT = (
    "主体与人物设定：年轻女性用户手持智能手机，展示 PolyBuzz 应用的角色语音通话"
    "界面；手机屏幕内呈现动漫风格角色立绘配合聊天界面、语音通话界面等 UI 元素。\n\n"
    "场景与环境：室内自然光场景，柔和日光从侧面打亮人物和手机屏幕，背景虚化突出"
    "前景主体。竖版 9:16 构图。\n\n"
    "动作与运动描述：0-5秒，手机屏幕特写展示 Cuddler 语音通话来电界面，动漫风格"
    "角色头像，来电提示动效；5-11秒，女性用户手持手机展示分支剧情选择界面，"
    "用户手指轻触屏幕做出选择手势；11-20秒，聊天界面特写展示角色立绘配语音"
    "波形动效，对话气泡逐条从下向上浮现。\n\n"
    "运镜语言：固定机位为主，无大幅度推拉摇移。\n\n"
    "音频设计：BGM 采用轻快电子流行乐，来电界面配提示音效。\n\n"
    "风格与氛围：现代科技感与二次元美学融合风格。"
)


def test_strict_returns_none_for_real_failing_brief() -> None:
    """这份（固化自真实批跑案例的）prompt 把分时段内容揉进了「动作与运动描述：」
    小节，没有独立的「分时段描述：」小节标题——strict 路径必须识别出这一点并返回
    None（不是抛异常，留给调用方决定是否走 fallback）。"""
    prompt = SYNTHETIC_FAILING_PROMPT
    assert "分时段描述" not in prompt, "测试前提不成立：这份 prompt 里竟然有「分时段描述」小节了"
    result = seedance_gen._extract_per_segment_texts_strict(prompt)
    assert result is None, f"strict 路径应返回 None，实际返回: {result}"


def test_fallback_extracts_0_5s_segment_from_real_failing_brief() -> None:
    """核心验收项：对这份没有「分时段描述：」标题的失败案例，fallback 必须能
    裁出 0-5s 的子 prompt，且子 prompt 不是全文、不是空字符串。"""
    prompt = SYNTHETIC_FAILING_PROMPT

    segments = seedance_gen._extract_per_segment_texts_fallback(prompt)
    assert segments, "fallback 未能从失败案例中提取出任何分时段"

    starts_ends = [(s, e) for s, e, _ in segments]
    assert (0.0, 5.0) in [(s, e) for s, e, _ in segments], (
        f"未找到 0-5s 分段，实际提取到的区间: {starts_ends}"
    )

    zero_to_five_text = next(t for s, e, t in segments if s == 0.0 and e == 5.0)
    assert zero_to_five_text, "0-5s 子 prompt 为空"
    assert zero_to_five_text != prompt, "0-5s 子 prompt 不应等于全文"
    assert len(zero_to_five_text) < len(prompt), "0-5s 子 prompt 应明显短于全文"
    # 语义校验：0-5s 子 prompt 应包含这一段实际描述的关键内容，不是随便截了一段无关文本。
    assert "语音通话" in zero_to_five_text or "来电" in zero_to_five_text, (
        f"0-5s 子 prompt 内容看起来不对: {zero_to_five_text!r}"
    )

    print(f"  0-5s 子 prompt（长度 {len(zero_to_five_text)}，全文长度 {len(prompt)}）: "
          f"{zero_to_five_text[:80]}...")


def test_extract_per_segment_texts_dispatches_to_fallback() -> None:
    """公开入口 _extract_per_segment_texts 在 strict 失败时要自动走 fallback，
    不能要求调用方手动判断走哪条路径。"""
    prompt = SYNTHETIC_FAILING_PROMPT
    segments = seedance_gen._extract_per_segment_texts(prompt)
    assert (0.0, 5.0) in [(s, e) for s, e, _ in segments]


def test_strict_path_still_preferred_when_section_title_present() -> None:
    """有独立「分时段描述：」小节时必须优先用 strict 路径，不应该被 fallback 覆盖或
    和其他小节内容混在一起。"""
    prompt = (
        "主体设定：一个测试角色。\n\n"
        "场景：测试场景。\n\n"
        "动作与运动描述：0-3秒，这是不应该被采用的干扰文本；3-6秒，同样是干扰文本。\n\n"
        "分时段描述：0-3秒，真正应该被提取的hook内容；3-6秒，真正应该被提取的卖点内容。\n\n"
        "音频设计：BGM 说明。"
    )
    segments = seedance_gen._extract_per_segment_texts(prompt)
    seg_map = {(s, e): t for s, e, t in segments}
    assert seg_map[(0.0, 3.0)] == "真正应该被提取的hook内容"
    assert seg_map[(3.0, 6.0)] == "真正应该被提取的卖点内容"


def main() -> None:
    tests = [
        test_strict_returns_none_for_real_failing_brief,
        test_fallback_extracts_0_5s_segment_from_real_failing_brief,
        test_extract_per_segment_texts_dispatches_to_fallback,
        test_strict_path_still_preferred_when_section_title_present,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过。")


if __name__ == "__main__":
    main()
