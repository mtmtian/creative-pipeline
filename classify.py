#!/usr/bin/env python3
"""
classify.py --analysis out/<批次名>

⚠️ 此为规则版分级建议（08-competitor-creative-pipeline.md §4.1 + Hakko SOP §0）。
   L3 判定（结构值钱 / 含第三方 IP / 真人脸）需要人工或 LLM 看画面识别，本版本不做——
   所有输出最高只到 L1 / L2 / L4，不产出 L3。

规则（对每个 file 计算 audio_brand / ocr_hit_ratio 后判定）：
  audio_brand=True 且 ocr_hit_ratio > 0.30            -> L4（灵感入库，画面声轨都不能用）
  audio_brand=True 且 ocr_hit_ratio <= 0.30            -> L2（抽片段重组，需去声轨）
  audio_brand=False 且 ocr_hit_ratio > 0.30            -> L4（OCR 单独大面积命中，画面不能直接投）
  audio_brand=False 且 0.10 < ocr_hit_ratio <= 0.30    -> L2（画面中等命中，需重组/大量遮挡）
  audio_brand=False 且 0 < ocr_hit_ratio <= 0.10        -> L1（少量固定 logo，delogo/遮标）
  audio_brand=False 且 ocr_hit_ratio == 0               -> L1（全干净，只需换尾帧）
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path


def load_ocr_ratio(analysis_dir: Path) -> dict[str, float]:
    """按 file 统计 OCR 抽帧品牌命中率 = 命中帧数 / 总抽帧数。"""
    total = defaultdict(int)
    hit_frames = defaultdict(set)

    ocr_frames_path = analysis_dir / "ocr_frames.csv"
    if ocr_frames_path.exists():
        with open(ocr_frames_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total[row["file"]] += 1

    hits_path = analysis_dir / "ocr_brand_hits.csv"
    if hits_path.exists():
        with open(hits_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hit_frames[row["file"]].add(row["ts"])

    ratios: dict[str, float] = {}
    for file, n in total.items():
        if n == 0:
            ratios[file] = 0.0
        else:
            ratios[file] = len(hit_frames.get(file, set())) / n
    return ratios


def load_audio_brand(analysis_dir: Path) -> set[str]:
    files = set()
    hits_path = analysis_dir / "transcript_brand_hits.csv"
    if hits_path.exists():
        with open(hits_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                files.add(row["file"])
    return files


def load_all_files(analysis_dir: Path) -> list[str]:
    manifest_path = analysis_dir / "manifest.csv"
    if not manifest_path.exists():
        sys.exit(f"找不到 manifest.csv，先跑 analyze.py: {manifest_path}")
    with open(manifest_path, newline="", encoding="utf-8") as f:
        return [row["file"] for row in csv.DictReader(f)]


def classify_one(file: str, audio_brand: bool, ocr_ratio: float) -> tuple[str, str]:
    reasons = []
    if audio_brand and ocr_ratio > 0.30:
        level = "L4"
        reasons.append(f"声轨命中品牌词 且 OCR命中率{ocr_ratio:.0%} (>30%区间) -> 灵感入库，画面声轨都不能用")
    elif audio_brand:
        level = "L2"
        reasons.append(f"声轨命中品牌词，OCR命中率{ocr_ratio:.0%} (<=30%，画面可剪) -> 抽片段重组，需去声轨")
    elif ocr_ratio > 0.30:
        level = "L4"
        reasons.append(f"OCR命中率{ocr_ratio:.0%} (>30%区间，声轨无命中) -> 画面大面积品牌露出，视为灵感入库")
    elif ocr_ratio > 0.10:
        level = "L2"
        reasons.append(f"OCR命中率{ocr_ratio:.0%} (10%-30%区间，中等命中) -> 抽片段重组")
    elif ocr_ratio > 0.0:
        level = "L1"
        reasons.append(f"OCR命中率{ocr_ratio:.0%} (<10%，疑似固定logo) -> delogo/遮标候选")
    else:
        level = "L1"
        reasons.append("声轨、OCR均无品牌命中，全干净 -> 只需换尾帧")
    return level, "; ".join(reasons)


def main() -> None:
    ap = argparse.ArgumentParser(description="规则版分级建议（L1/L2/L4，不做L3）")
    ap.add_argument("--analysis", type=Path, required=True, help="analyze.py 的输出目录")
    args = ap.parse_args()

    analysis_dir = args.analysis.resolve()
    if not analysis_dir.is_dir():
        sys.exit(f"analysis 目录不存在: {analysis_dir}")

    files = load_all_files(analysis_dir)
    ocr_ratios = load_ocr_ratio(analysis_dir)
    audio_brand_files = load_audio_brand(analysis_dir)

    out_path = analysis_dir / "classification.csv"
    rows = []
    level_counts = defaultdict(int)
    for file in files:
        ocr_ratio = ocr_ratios.get(file, 0.0)
        audio_brand = file in audio_brand_files
        level, reasons = classify_one(file, audio_brand, ocr_ratio)
        level_counts[level] += 1
        rows.append({
            "file": file,
            "level": level,
            "reasons": reasons,
            "visual_brand": "true" if ocr_ratio > 0 else "false",
            "audio_brand": "true" if audio_brand else "false",
            "notes": f"ocr_hit_ratio={ocr_ratio:.3f}",
        })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "level", "reasons", "visual_brand", "audio_brand", "notes"])
        w.writeheader()
        w.writerows(rows)

    print(f"classification.csv 写入 {len(rows)} 行 -> {out_path}")
    print(f"分级分布: {dict(level_counts)}")


if __name__ == "__main__":
    main()
