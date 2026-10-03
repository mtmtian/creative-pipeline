#!/usr/bin/env python3
"""
prompt_lint.py — 纯确定性规则引擎，检查 Seedance 生成 prompt 是否符合规范。

不调用任何 LLM / 网络。核心函数 `lint(seedance_prompt, has_real_face=False,
duration_sec=None) -> LintResult`。

CLI 模式：
    python3 prompt_lint.py <brief.json 路径>

brief.json 需含 `seedance_prompt` 字段；可选 `has_real_face` 字段（从对应的
analysis/<stem>.json 传入，因为"写实真人脸不得用视频参照"这条规则需要这个信息）。

退出码：任何 error 级问题存在时非零退出；只有 warning 时退出码为 0。

GENERIC_PHRASES 废话词库借用自 OpenMontage/lib/variation_checker.py（只读引用，
未修改该文件），挑了其中适用于中文 remix prompt 语境的几十个通用空泛表达。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 规则用常量
# ---------------------------------------------------------------------------

REF_PATTERN = re.compile(r"@(图片|视频|音频)(\d+)")

# 用途声明动词（引用所在句子必须包含其中之一）
PURPOSE_VERBS = ("作为", "参考", "参照", "插槽")

# 分句用标点（用于按句子检查用途声明，以及粗分 beat 数）
SENTENCE_SPLIT_PATTERN = re.compile(r"[。！？\n]")

# 时间段模式："0-3秒" "3-8秒" "12–15秒"（兼容全角/半角连字符）
TIME_SEGMENT_PATTERN = re.compile(r"\d+\s*[-–—~]\s*\d+\s*秒")

# 互斥运镜词
FIXED_CAMERA_WORDS = ("固定镜头", "镜头保持静止", "静止镜头")
MOVING_CAMERA_WORDS = ("环绕", "环绕镜头", "推拉", "跟随镜头", "跟拍")

# 音频设计关键词
AUDIO_KEYWORDS = ("音频设计", "音效", "BGM", "背景音乐", "配乐")

# 生成内文字指令词
ON_SCREEN_TEXT_WORDS = ("字幕出现", "标题文字", "logo 出现", "Logo 出现", "LOGO 出现")

# 特例：尾帧 CTA 允许的措辞（命中时 warning 而非 error）
CTA_ALLOWED_PHRASE = "品牌广告语出现"

# GENERIC_PHRASES：借用自 OpenMontage/lib/variation_checker.py 的 GENERIC_PHRASES
# 词库思路（该文件是英文视频 scene-plan 语境的词表，这里挑选/补充适用于中文
# remix prompt 语境的空泛表达，不是整体复制该模块逻辑，只借用"通用空泛词命中即
# warning"的检查思路）。
GENERIC_PHRASES = {
    "现代", "未来感", "前沿", "革命性", "创新", "尖端",
    "沉浸式", "极致体验", "颠覆性", "重新定义", "无与伦比",
    "state-of-the-art", "next-generation", "cutting-edge",
    "a person", "a beautiful", "modern", "futuristic",
    "innovative", "seamless", "stunning", "breathtaking",
    "amazing", "incredible", "powerful", "vibrant",
    "令人惊叹", "美轮美奂", "震撼人心", "高端大气", "国际范",
}

# 单时段动作 beat 数上限
MAX_BEATS_PER_SEGMENT = 3

# @引用数量上限
MAX_IMAGE_REFS = 9
MAX_VIDEO_REFS = 3
MAX_TOTAL_REFS = 12


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"errors": self.errors, "warnings": self.warnings, "ok": self.ok}


def _split_sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT_PATTERN.split(text) if s.strip()]


def _split_segments(text: str) -> list[str]:
    """按分时段描述切分文本；若没有分时段标记，整段文本作为一个 segment。"""
    matches = list(TIME_SEGMENT_PATTERN.finditer(text))
    if not matches:
        return [text]
    segments = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segments.append(text[start:end])
    return segments


def _check_ref_purpose_declarations(text: str, errors: list[str]) -> None:
    sentences = _split_sentences(text)
    # 把逗号也视为子句边界，避免一句话里两个引用共享同一个用途声明动词导致误判过宽，
    # 这里采用宽松策略：按句号级切分句子，只要句子内出现过用途动词即算合格
    # （粗粒度检查，允许一定误差，但明显缺失时必须能抓出来）。
    for ref_match in REF_PATTERN.finditer(text):
        ref_text = ref_match.group(0)
        # 定位该引用所在的句子
        containing_sentence = None
        for sent in sentences:
            if ref_text in sent:
                containing_sentence = sent
                break
        if containing_sentence is None:
            containing_sentence = text
        if not any(v in containing_sentence for v in PURPOSE_VERBS):
            errors.append(f"缺少用途声明：引用 {ref_text} 所在句子中未出现"
                           f"「{'/'.join(PURPOSE_VERBS)}」任一用途声明词")


def _check_ref_counts(text: str, errors: list[str]) -> None:
    image_refs = re.findall(r"@图片\d+", text)
    video_refs = re.findall(r"@视频\d+", text)
    audio_refs = re.findall(r"@音频\d+", text)
    n_image = len(set(image_refs))
    n_video = len(set(video_refs))
    n_audio = len(set(audio_refs))
    n_total = n_image + n_video + n_audio

    if n_image > MAX_IMAGE_REFS:
        errors.append(f"@图片 引用数量 {n_image} 超出上限 {MAX_IMAGE_REFS}")
    if n_video > MAX_VIDEO_REFS:
        errors.append(f"@视频 引用数量 {n_video} 超出上限 {MAX_VIDEO_REFS}")
    if n_total > MAX_TOTAL_REFS:
        errors.append(f"全部引用总数 {n_total} 超出上限 {MAX_TOTAL_REFS}")


def _check_time_segments(text: str, errors: list[str], duration_sec: float | None) -> None:
    """若素材总时长 > 8s，必须检测到至少一个分时段描述。

    判据：优先用传入的 duration_sec；未传入时，用文本里能否找到时间段模式作为
    弱判据（找不到就跳过这条检查，因为无法确定原始时长，不能瞎报 error）。
    """
    if duration_sec is None:
        return
    if duration_sec <= 8:
        return
    if not TIME_SEGMENT_PATTERN.search(text):
        errors.append(f"素材时长 {duration_sec:.1f}s > 8s，但未检测到「数字-数字秒」"
                       f"形式的分时段描述")


def _check_camera_conflict(text: str, errors: list[str]) -> None:
    for seg in _split_segments(text):
        has_fixed = any(w in seg for w in FIXED_CAMERA_WORDS)
        has_moving = any(w in seg for w in MOVING_CAMERA_WORDS)
        if has_fixed and has_moving:
            errors.append(f"同一时段运镜冲突：同时出现固定镜头类词与环绕/推拉/"
                           f"跟随类互斥词 -> 片段: \"{seg.strip()[:80]}\"")


def _check_beat_count(text: str, warnings: list[str]) -> None:
    for seg in _split_segments(text):
        seg = seg.strip()
        if not seg:
            continue
        # 用逗号、顿号粗分句，计数分句数作为 beat 数的粗略近似
        parts = [p for p in re.split(r"[，,、]", seg) if p.strip()]
        if len(parts) > MAX_BEATS_PER_SEGMENT:
            warnings.append(f"单时段动作 beat 数 {len(parts)} 超过建议上限 "
                             f"{MAX_BEATS_PER_SEGMENT}（粗分句计数，存在误差）"
                             f" -> 片段: \"{seg[:80]}\"")


def _check_audio_section(text: str, errors: list[str]) -> None:
    if not any(kw in text for kw in AUDIO_KEYWORDS):
        errors.append("缺少音频设计段：未检测到「音频设计/音效/BGM/背景音乐/配乐」"
                       "等关键词")


def _check_on_screen_text(text: str, errors: list[str], warnings: list[str]) -> None:
    for word in ON_SCREEN_TEXT_WORDS:
        idx = text.find(word)
        while idx != -1:
            # 检查命中上下文是否是尾帧 CTA 的允许措辞
            context = text[max(0, idx - 20): idx + len(word) + 20]
            if CTA_ALLOWED_PHRASE in context:
                warnings.append(f"检测到生成内文字指令词「{word}」，但出现在"
                                 f"尾帧CTA上下文（含「{CTA_ALLOWED_PHRASE}」），降级为 warning")
            else:
                errors.append(f"检测到生成内文字指令词「{word}」，这类文字应交给"
                               f"后期 overlay 叠加，不应写进生成 prompt")
            idx = text.find(word, idx + len(word))

    # 单独检查"品牌广告语出现"本身也要 warn 一下，提醒它是允许但需要注意的措辞
    if CTA_ALLOWED_PHRASE in text:
        warnings.append(f"检测到「{CTA_ALLOWED_PHRASE}」——尾帧CTA允许措辞，"
                         f"确认上下文确实是尾帧CTA段")


def _check_real_face_video_ref(text: str, has_real_face: bool, errors: list[str]) -> None:
    if has_real_face and re.search(r"@视频\d+", text):
        errors.append("写实真人脸素材不得使用视频参照：has_real_face=true 但 "
                       "prompt 中出现 @视频N 引用")


def _check_generic_phrases(text: str, warnings: list[str]) -> None:
    hits = [p for p in GENERIC_PHRASES if p in text]
    if hits:
        warnings.append(f"命中废话/空泛词（GENERIC_PHRASES，借用自 "
                         f"OpenMontage/lib/variation_checker.py 的检查思路）: "
                         f"{', '.join(sorted(hits))}")


def lint(seedance_prompt: str, has_real_face: bool = False,
          duration_sec: float | None = None) -> LintResult:
    """对一段 Seedance 生成 prompt 跑全部规则检查，返回 LintResult。"""
    errors: list[str] = []
    warnings: list[str] = []

    text = seedance_prompt or ""

    _check_ref_purpose_declarations(text, errors)
    _check_ref_counts(text, errors)
    _check_time_segments(text, errors, duration_sec)
    _check_camera_conflict(text, errors)
    _check_beat_count(text, warnings)
    _check_audio_section(text, errors)
    _check_on_screen_text(text, errors, warnings)
    _check_real_face_video_ref(text, has_real_face, errors)
    _check_generic_phrases(text, warnings)

    return LintResult(errors=errors, warnings=warnings)


def format_report(result: LintResult, label: str = "") -> str:
    lines = []
    header = f"prompt_lint 报告{f' ({label})' if label else ''}"
    lines.append(header)
    lines.append("=" * len(header))
    if result.errors:
        lines.append(f"\nERROR ({len(result.errors)}):")
        for e in result.errors:
            lines.append(f"  [ERROR] {e}")
    if result.warnings:
        lines.append(f"\nWARNING ({len(result.warnings)}):")
        for w in result.warnings:
            lines.append(f"  [WARN]  {w}")
    if not result.errors and not result.warnings:
        lines.append("\n无问题。")
    lines.append("")
    lines.append(f"结论: {'FAIL' if result.errors else 'PASS'}"
                  f"（errors={len(result.errors)}, warnings={len(result.warnings)}）")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Seedance prompt 规则引擎 lint")
    ap.add_argument("brief_json", type=Path, help="brief JSON 文件路径（含 seedance_prompt 字段）")
    ap.add_argument("--has-real-face", action="store_true",
                     help="显式声明该素材含写实真人脸（若不传，尝试从 brief json 的 "
                          "has_real_face 字段读取，默认 false）")
    ap.add_argument("--duration-sec", type=float, default=None,
                     help="素材总时长（秒），用于分时段检查；不传则尝试从 brief json 读取")
    args = ap.parse_args()

    if not args.brief_json.exists():
        sys.exit(f"文件不存在: {args.brief_json}")

    data = json.loads(args.brief_json.read_text(encoding="utf-8"))
    prompt = data.get("seedance_prompt", "")
    if not prompt:
        sys.exit(f"brief json 中没有 seedance_prompt 字段: {args.brief_json}")

    has_real_face = args.has_real_face or bool(data.get("has_real_face", False))
    duration_sec = args.duration_sec if args.duration_sec is not None else data.get("duration_sec")

    result = lint(prompt, has_real_face=has_real_face, duration_sec=duration_sec)
    print(format_report(result, label=str(args.brief_json)))

    sys.exit(1 if result.errors else 0)


if __name__ == "__main__":
    main()
