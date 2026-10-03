#!/usr/bin/env python3
"""
remix_brief.py --analysis out/<批次名> [--file <stem>]

L3 复刻 brief 生成器：对 classification_v2.csv 里 level_v2=L3 的文件（或用 --file
指定单个文件，忽略 level_v2 过滤），读取 analysis/<stem>.json 里的 five_aspect，
组装 prompts/remix_brief.md 模板 + PRODUCT 上下文，调用 Anthropic API，产出
briefs/<stem>.json。

生成后自动跑 prompt_lint（把对应 analysis json 的 has_real_face 传给 lint）。
lint 不过（有 error）：把报错文本回喂给 LLM 要求修正，重新生成再 lint，最多 2 轮
修复。仍不过：brief JSON 里加 needs_review=true 和 lint_errors 字段，不中断批次。

API key 获取：config.get_anthropic_api_key()，探测失败即如实报错退出。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import config
import prompt_lint

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "remix_brief.md"

MAX_LINT_FIX_ROUNDS = 2


def load_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def call_anthropic_text(api_key: str, user_text: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.ANTHROPIC_MAX_TOKENS,
        messages=[{"role": "user", "content": user_text}],
    )
    parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    return "".join(parts)


def build_prompt(five_aspect: list, duration_sec: float | None, has_real_face: bool,
                  source_file: str, segment_plan: list | None = None) -> str:
    template_text = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    product_text = json.dumps(config.PRODUCT, ensure_ascii=False, indent=2)
    five_aspect_text = json.dumps(five_aspect, ensure_ascii=False, indent=2)
    segment_plan_text = json.dumps(segment_plan or [], ensure_ascii=False, indent=2)
    duration_line = f"该素材总时长约 {duration_sec:.1f} 秒。" if duration_sec else "该素材总时长未知，请按 five_aspect 镜头数量合理估算。"
    return (
        f"{template_text}\n\n"
        f"---\n\n"
        f"## 本条素材的实际输入\n\n"
        f"{duration_line}\n\n"
        f"### has_real_face\n{json.dumps(has_real_face)}\n\n"
        f"### source_file\n{source_file}\n\n"
        f"### segment_plan（分段路由计划，来自 classify_llm.py，空数组=无分段路由信息）\n"
        f"```json\n{segment_plan_text}\n```\n\n"
        f"### five_aspect（逐镜拆解）\n```json\n{five_aspect_text}\n```\n\n"
        f"### PRODUCT 上下文\n```json\n{product_text}\n```\n"
    )


def generate_brief(api_key: str, five_aspect: list, duration_sec: float | None,
                    has_real_face: bool, source_file: str, segment_plan: list | None = None) -> dict:
    prompt_text = build_prompt(five_aspect, duration_sec, has_real_face, source_file, segment_plan)
    raw = call_anthropic_text(api_key, prompt_text)
    try:
        cleaned = strip_code_fence(raw)
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # 解析失败重试一次：把报错位置回喂，要求修复后只输出合法 JSON
        retry_text = (
            f"{prompt_text}\n\n---\n\n"
            f"你上一次的输出不是合法 JSON（解析报错: {e}）。上次输出如下：\n"
            f"{raw}\n\n"
            f"修复 JSON 语法错误（注意字符串内的引号必须转义），"
            f"只输出完整合法的 JSON，不要任何解释或代码块围栏。"
        )
        raw = call_anthropic_text(api_key, retry_text)
        cleaned = strip_code_fence(raw)
        parsed = json.loads(cleaned)  # 第二次仍失败则往上抛，由调用方处理
    parsed["_raw_response"] = raw
    return parsed


def fix_brief_with_lint_errors(api_key: str, five_aspect: list, duration_sec: float | None,
                                 has_real_face: bool, source_file: str,
                                 prior_brief: dict, lint_errors: list[str],
                                 segment_plan: list | None = None) -> dict:
    template_text = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    product_text = json.dumps(config.PRODUCT, ensure_ascii=False, indent=2)
    five_aspect_text = json.dumps(five_aspect, ensure_ascii=False, indent=2)
    segment_plan_text = json.dumps(segment_plan or [], ensure_ascii=False, indent=2)
    prior_text = json.dumps(
        {k: v for k, v in prior_brief.items() if not k.startswith("_")},
        ensure_ascii=False, indent=2,
    )
    errors_text = "\n".join(f"- {e}" for e in lint_errors)
    fix_prompt = (
        f"{template_text}\n\n"
        f"---\n\n"
        f"## 本条素材的实际输入\n\n"
        f"### has_real_face\n{json.dumps(has_real_face)}\n\n"
        f"### source_file\n{source_file}\n\n"
        f"### segment_plan（分段路由计划，来自 classify_llm.py，空数组=无分段路由信息）\n"
        f"```json\n{segment_plan_text}\n```\n\n"
        f"### five_aspect（逐镜拆解）\n```json\n{five_aspect_text}\n```\n\n"
        f"### PRODUCT 上下文\n```json\n{product_text}\n```\n\n"
        f"---\n\n"
        f"## 修正任务\n\n"
        f"你上一次生成的 brief JSON 是：\n```json\n{prior_text}\n```\n\n"
        f"这份 brief 的 seedance_prompt 经 prompt_lint 检查，命中以下 error 级问题：\n"
        f"{errors_text}\n\n"
        f"根据以上 lint 报错修正 seedance_prompt（其余字段如 differentiated_storyboard/"
        f"sample_shots/est_cost_usd 保持不变，除非修正 seedance_prompt 需要联动调整），"
        f"只输出修正后的完整 brief JSON，不要输出任何解释性文字或 markdown 代码块围栏。"
    )
    raw = call_anthropic_text(api_key, fix_prompt)
    cleaned = strip_code_fence(raw)
    parsed = json.loads(cleaned)
    parsed["_raw_response"] = raw
    return parsed


def process_file(api_key: str, analysis_dir: Path, briefs_dir: Path, file: str) -> tuple[bool, str]:
    stem = Path(file).stem
    analysis_json_path = analysis_dir / "analysis" / f"{stem}.json"
    if not analysis_json_path.exists():
        return False, f"找不到 analysis/{stem}.json，先跑 classify_llm.py"

    try:
        analysis_data = json.loads(analysis_json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"analysis json 解析失败: {e}"

    five_aspect = analysis_data.get("five_aspect")
    if not five_aspect:
        return False, f"analysis/{stem}.json 缺少 five_aspect，无法生成 brief"

    has_real_face = bool(analysis_data.get("has_real_face", False))
    segment_plan = analysis_data.get("segment_plan") or []

    metadata_rows = {r["file"]: r for r in load_csv_rows(analysis_dir / "metadata.csv")}
    duration_sec = None
    if file in metadata_rows and metadata_rows[file].get("duration_sec"):
        try:
            duration_sec = float(metadata_rows[file]["duration_sec"])
        except ValueError:
            duration_sec = None

    try:
        brief = generate_brief(api_key, five_aspect, duration_sec, has_real_face, file, segment_plan)
    except Exception as e:
        return False, f"生成 brief 失败: {e}"

    lint_result = prompt_lint.lint(
        brief.get("seedance_prompt", ""), has_real_face=has_real_face, duration_sec=duration_sec
    )

    rounds = 0
    while lint_result.errors and rounds < MAX_LINT_FIX_ROUNDS:
        rounds += 1
        print(f"    lint 未过（{len(lint_result.errors)} 个 error），第 {rounds} 轮修复...")
        try:
            brief = fix_brief_with_lint_errors(
                api_key, five_aspect, duration_sec, has_real_face, file, brief, lint_result.errors,
                segment_plan,
            )
        except Exception as e:
            brief["needs_review"] = True
            brief["lint_errors"] = lint_result.errors + [f"修复调用失败: {e}"]
            break
        lint_result = prompt_lint.lint(
            brief.get("seedance_prompt", ""), has_real_face=has_real_face, duration_sec=duration_sec
        )

    if lint_result.errors:
        brief["needs_review"] = True
        brief["lint_errors"] = lint_result.errors

    brief["_lint_warnings"] = lint_result.warnings
    brief["_has_real_face"] = has_real_face
    brief["_duration_sec"] = duration_sec
    brief["_segment_plan"] = segment_plan

    briefs_dir.mkdir(exist_ok=True)
    out_path = briefs_dir / f"{stem}.json"
    out_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "PASS" if not lint_result.errors else "NEEDS_REVIEW"
    return True, f"{status}（{len(lint_result.errors)} errors, {len(lint_result.warnings)} warnings, {rounds} 轮修复）"


def main() -> None:
    ap = argparse.ArgumentParser(description="L3 复刻 brief 生成器")
    ap.add_argument("--analysis", type=Path, required=True, help="analyze.py 的输出目录")
    ap.add_argument("--file", type=str, default=None,
                     help="只处理指定文件（文件名，忽略 level_v2 过滤）")
    args = ap.parse_args()

    analysis_dir = args.analysis.resolve()
    if not analysis_dir.is_dir():
        sys.exit(f"analysis 目录不存在: {analysis_dir}")

    api_key = config.get_anthropic_api_key()
    if not api_key:
        sys.exit(
            "无法获取 Anthropic API key：hakko-secret 探测的所有密钥名均不可用。"
            "如实报告：LLM 调用不可能发生，未伪造任何响应。"
        )

    if args.file:
        target_files = [args.file]
    else:
        v2_rows = load_csv_rows(analysis_dir / "classification_v2.csv")
        if not v2_rows:
            sys.exit(f"找不到 classification_v2.csv，先跑 classify_llm.py: {analysis_dir}")
        target_files = [r["file"] for r in v2_rows if r.get("level_v2") == "L3"]
        if not target_files:
            sys.exit("classification_v2.csv 中没有 level_v2=L3 的文件，"
                     "可用 --file <文件名> 强制对指定文件生成 brief")

    briefs_dir = analysis_dir / "briefs"

    ok_count = 0
    for file in target_files:
        print(f"  处理: {file}")
        ok, msg = process_file(api_key, analysis_dir, briefs_dir, file)
        print(f"    {msg}")
        if ok:
            ok_count += 1

    print()
    print(f"共处理 {len(target_files)} 个文件，{ok_count} 个成功产出 brief -> {briefs_dir}")


if __name__ == "__main__":
    main()
