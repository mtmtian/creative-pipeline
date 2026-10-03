#!/usr/bin/env python3
"""
classify_llm.py --analysis out/<批次名> [--limit N] [--force]

LLM 分级精排：读 analyze.py 的产物（关键帧拼图 + transcript + OCR 品牌命中 +
classify.py 的规则版预分级），对每个文件调用 Anthropic Messages API 做"画面维度"
精排（真人脸 / 疑似第三方 IP / 结构价值 / L3 判定），产出：

  analysis/<stem>.json      LLM 返回的完整结构化 JSON（含 _raw_response 原始响应存档）
  classification_v2.csv     file, level_v2, material_type, has_real_face, motion_type,
                             hook_strength, structure_value, reasons

Prompt 模板：prompts/classify_v2.md（只读，运行时读取拼接，不在这里重复写死）。

断点续跑：analysis/<stem>.json 已存在且未传 --force 时跳过（不重复调用 API）。
JSON 解析失败：自动重试 1 次（追加"只输出 JSON"提示），仍失败则该文件记一行
error（csv 里体现），不中断整个批次继续跑其他文件。

API key 获取：config.get_anthropic_api_key()（运行时通过 hakko-secret 探测，
绝不打印/落盘 key 本身）。key 不可用时如实报错退出，不伪造 LLM 响应。
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
from pathlib import Path

import config

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "classify_v2.md"

REQUIRED_FIVE_ASPECT_FIELDS = [
    "Subject", "Subject Motion", "Scene", "Spatial Framing", "Camera",
]


# ---------------------------------------------------------------------------
# 数据加载：复用 analyze.py / classify.py 的产物结构
# ---------------------------------------------------------------------------


def load_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_file_context(analysis_dir: Path, file: str) -> dict:
    """为一个文件组装：带时间戳转写、OCR 品牌命中摘要、规则版预分级、总时长。"""
    transcript_rows = [r for r in load_csv_rows(analysis_dir / "transcript.csv") if r["file"] == file]
    brand_hit_rows = [r for r in load_csv_rows(analysis_dir / "transcript_brand_hits.csv") if r["file"] == file]
    brand_hit_starts = {r["start"] for r in brand_hit_rows}
    if transcript_rows:
        segment_lines = []
        for r in transcript_rows:
            marker = ""
            if r.get("start") in brand_hit_starts:
                hit_brands = sorted({h["brand"] for h in brand_hit_rows if h["start"] == r["start"]})
                marker = f" [命中品牌:{','.join(hit_brands)}]"
            segment_lines.append(f"{r['start']}-{r['end']}秒: {r.get('text', '')}{marker}")
        transcript_text = "\n".join(segment_lines)
    else:
        transcript_text = "（无转写内容，可能无音轨或为纯音乐/环境音）"

    ocr_hits = [r for r in load_csv_rows(analysis_dir / "ocr_brand_hits.csv") if r["file"] == file]
    if ocr_hits:
        ocr_summary = "; ".join(
            f"{r['ts']}s 命中品牌「{r['brand']}」（原文: {r['matched_text']}）" for r in ocr_hits
        )
    else:
        ocr_summary = "（OCR 未命中任何品牌词）"

    classification_rows = {r["file"]: r for r in load_csv_rows(analysis_dir / "classification.csv")}
    rule_row = classification_rows.get(file)
    if rule_row:
        rule_summary = (
            f"level={rule_row['level']}, reasons={rule_row['reasons']}, "
            f"visual_brand={rule_row['visual_brand']}, audio_brand={rule_row['audio_brand']}, "
            f"notes={rule_row['notes']}"
        )
    else:
        rule_summary = "（未找到规则版预分级，请先跑 classify.py）"

    metadata_rows = {r["file"]: r for r in load_csv_rows(analysis_dir / "metadata.csv")}
    duration_sec = None
    if file in metadata_rows and metadata_rows[file].get("duration_sec"):
        try:
            duration_sec = float(metadata_rows[file]["duration_sec"])
        except ValueError:
            duration_sec = None

    return {
        "transcript_text": transcript_text,
        "ocr_summary": ocr_summary,
        "rule_summary": rule_summary,
        "duration_sec": duration_sec,
    }


def find_keyframe_image(analysis_dir: Path, file: str) -> Path | None:
    stem = Path(file).stem
    candidate = analysis_dir / "keyframes" / f"{stem}.jpg"
    return candidate if candidate.exists() else None


def image_to_base64(path: Path) -> tuple[str, str]:
    media_type = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return media_type, data


# ---------------------------------------------------------------------------
# Anthropic API 调用
# ---------------------------------------------------------------------------


def call_anthropic(api_key: str, system_prompt_extra: str, image_media_type: str,
                    image_b64: str, context: dict) -> str:
    """调用 Anthropic Messages API，返回原始文本响应（不解析）。"""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    template_text = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")

    duration_line = (
        f"{context['duration_sec']:.2f} 秒" if context.get("duration_sec") is not None
        else "未知（未在 metadata.csv 中找到，请按转写/OCR 时间戳能覆盖到的最大时间估算）"
    )

    user_text = (
        f"{template_text}\n\n"
        f"---\n\n"
        f"## 本条素材的实际输入\n\n"
        f"### 素材总时长\n{duration_line}\n\n"
        f"### 带时间戳的转写全文\n{context['transcript_text']}\n\n"
        f"### OCR 品牌命中摘要\n{context['ocr_summary']}\n\n"
        f"### 规则版预分级\n{context['rule_summary']}\n"
        f"{system_prompt_extra}"
    )

    message = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.ANTHROPIC_MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image_media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )
    parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    return "".join(parts)


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


def validate_five_aspect(parsed: dict) -> list[str]:
    """检查 five_aspect 数组每个镜头是否包含全部必需字段（允许值为 "N/A"，但不能缺字段）。"""
    problems = []
    five_aspect = parsed.get("five_aspect")
    if not isinstance(five_aspect, list) or not five_aspect:
        problems.append("five_aspect 缺失或为空")
        return problems
    for i, shot in enumerate(five_aspect):
        for field_name in REQUIRED_FIVE_ASPECT_FIELDS:
            if field_name not in shot:
                problems.append(f"five_aspect[{i}] 缺少字段 {field_name}")
    return problems


def classify_one_file(api_key: str, analysis_dir: Path, file: str) -> tuple[dict, int]:
    """对单个文件调用 LLM，返回 (最终 parsed dict（含 _raw_response）, 实际 API 调用次数)。

    解析失败自动重试 1 次（最多 2 次真实 API 调用）。
    """
    image_path = find_keyframe_image(analysis_dir, file)
    if image_path is None:
        return {"_error": f"找不到关键帧拼图: keyframes/{Path(file).stem}.jpg"}, 0

    context = build_file_context(analysis_dir, file)
    media_type, image_b64 = image_to_base64(image_path)

    last_raw = ""
    calls = 0
    for attempt in range(2):
        extra = ""
        if attempt == 1:
            extra = "\n\n（上一次输出不是合法 JSON，这次只输出 JSON，不要有任何其他文字。）"
        try:
            raw = call_anthropic(api_key, extra, media_type, image_b64, context)
            calls += 1
        except Exception as e:
            return {"_error": f"API 调用失败: {e}"}, calls
        last_raw = raw
        cleaned = strip_code_fence(raw)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            if attempt == 0:
                continue
            return {"_error": f"JSON 解析失败（重试1次后仍失败）: {e}", "_raw_response": raw}, calls

        problems = validate_five_aspect(parsed)
        if problems:
            parsed.setdefault("_warnings", []).extend(problems)
        parsed["_raw_response"] = last_raw
        return parsed, calls

    return {"_error": "未知失败", "_raw_response": last_raw}, calls


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM 分级精排：画面维度补充 L1/L2/L3/L4 判定")
    ap.add_argument("--analysis", type=Path, required=True, help="analyze.py 的输出目录")
    ap.add_argument("--limit", type=int, default=None, help="最多处理多少个文件（调试用）")
    ap.add_argument("--force", action="store_true", help="忽略已有 analysis/<stem>.json，强制重跑")
    ap.add_argument("--file", type=str, default=None,
                     help="只处理指定文件（manifest.csv 里的 file 值，忽略 --limit）")
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

    manifest_rows = load_csv_rows(analysis_dir / "manifest.csv")
    if not manifest_rows:
        sys.exit(f"找不到 manifest.csv，先跑 analyze.py: {analysis_dir}")
    manifest_files = {r["file"] for r in manifest_rows}

    if args.file:
        if args.file not in manifest_files:
            sys.exit(f"--file {args.file!r} 不在 manifest.csv 里: {analysis_dir}")
        files = [args.file]
    else:
        files = [r["file"] for r in manifest_rows]
        if args.limit:
            files = files[: args.limit]

    out_analysis_dir = analysis_dir / "analysis"
    out_analysis_dir.mkdir(exist_ok=True)

    # 已有 classification_v2.csv 的行按 file 建索引（保留原有顺序/其余文件的行不变）；
    # --file 单文件重跑时只更新这一行，不整表重写、不重复追加。
    out_csv_path = analysis_dir / "classification_v2.csv"
    existing_rows = {r["file"]: r for r in load_csv_rows(out_csv_path)}

    api_call_count = 0

    for file in files:
        out_json_path = out_analysis_dir / f"{Path(file).stem}.json"
        if out_json_path.exists() and not args.force:
            print(f"  [跳过] 已存在 {out_json_path.name}")
            try:
                parsed = json.loads(out_json_path.read_text(encoding="utf-8"))
            except Exception:
                parsed = {}
        else:
            print(f"  [调用 LLM] {file}")
            parsed, calls = classify_one_file(api_key, analysis_dir, file)
            api_call_count += calls
            out_json_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

        if "_error" in parsed:
            existing_rows[file] = {
                "file": file, "level_v2": "", "material_type": "", "has_real_face": "",
                "motion_type": "", "hook_strength": "", "structure_value": "",
                "reasons": f"ERROR: {parsed['_error']}", "segment_summary": "",
            }
            continue

        level_v2 = parsed.get("level_v2", {})
        hook = parsed.get("hook_analysis", {})
        structure = parsed.get("structure_value", {})
        segment_plan = parsed.get("segment_plan") or []
        segment_summary = "|".join(
            f"{seg.get('action', '?')}:{seg.get('time_range', '?')}" for seg in segment_plan
        )
        existing_rows[file] = {
            "file": file,
            "level_v2": level_v2.get("level", ""),
            "material_type": parsed.get("material_type", ""),
            "has_real_face": parsed.get("has_real_face", ""),
            "motion_type": parsed.get("motion_type", ""),
            "hook_strength": hook.get("strength", ""),
            "structure_value": structure.get("score", ""),
            "reasons": level_v2.get("reason", ""),
            "segment_summary": segment_summary,
        }

    with open(out_csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "file", "level_v2", "material_type", "has_real_face", "motion_type",
            "hook_strength", "structure_value", "reasons", "segment_summary",
        ])
        w.writeheader()
        w.writerows(existing_rows.values())

    print()
    print(f"classification_v2.csv 写入 {len(existing_rows)} 行（本次更新 {len(files)} 行）-> {out_csv_path}")
    print(f"本次实际 LLM 调用次数: {api_call_count}（模型: {config.ANTHROPIC_MODEL}）")


if __name__ == "__main__":
    main()
