#!/usr/bin/env python3
"""
qc.py <成片文件或目录>

投放前预检：对成片重跑 whisper 转写 + OCR，扫品牌词命中，输出 qc_report.csv
（file, check, status, detail）。任何一处命中即整体 FAIL 且非零退出码；全干净则
PASS，退出码 0。

复用 analyze.py 里的 ffprobe/whisper/ocr 抽取函数（不复制粘贴），品牌词扫描复用
scanner.py（跟 analyze.py 共享同一份逻辑）。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import config
import scanner
from analyze import (
    VIDEO_EXTS,
    OCR_INTERVAL_SEC,
    extract_audio_wav,
    frame_text,
    ocr_video,
    whisper_transcribe,
)


def qc_audio(video: Path, scratch: Path) -> list[dict]:
    wav_path = scratch / f"{video.stem}.wav"
    rows = []
    if not extract_audio_wav(video, wav_path):
        rows.append({"file": video.name, "check": "audio_brand_scan", "status": "PASS", "detail": "无音轨或音频抽取失败，跳过声轨扫描"})
        return rows

    segments = whisper_transcribe(wav_path)
    wav_path.unlink(missing_ok=True)

    hits = []
    for seg in segments:
        for hit in scanner.find_brand_hits(seg["text"]):
            hits.append(f"{seg['start']:.2f}s-{seg['end']:.2f}s [{hit.brand}] \"{hit.matched_text}\" in \"{seg['text']}\"")

    if hits:
        rows.append({"file": video.name, "check": "audio_brand_scan", "status": "FAIL", "detail": " | ".join(hits)})
    else:
        rows.append({"file": video.name, "check": "audio_brand_scan", "status": "PASS", "detail": f"{len(segments)} 段转写均无品牌词命中"})
    return rows


def scan_frames_for_brand_hits(
    video: Path, scratch: Path, interval_sec: float = OCR_INTERVAL_SEC,
) -> tuple[list[dict], int]:
    """共享的抽帧 OCR 品牌词扫描：抽帧和识别用 analyze.ocr_video，再逐帧做品牌词匹配。

    返回 (hits, frame_count)：
      hits = [{"ts": float, "brand": str, "matched_text": str, "frame_text": str}, ...]，
             按 ts 升序；frame_text 是该帧完整 OCR 文本（供 qc.py 拼 detail 字符串用）。
      frame_count = 实际成功导出的帧数（用于 qc.py 的 PASS 提示文案）。

    qc.py 自己的 CLI 路径（qc_ocr）和 worker.py 的 qc_scan 都调用这个函数——两边输出契约
    不同（qc.py 要拼 detail 字符串，worker.py 要结构化 hits），由各自调用方加工。

    抽帧失败（文件损坏/无法解码等）会抛 RuntimeError，不能让扫不动的文件被误判成 PASS。"""
    frames = ocr_video(video, scratch, interval_sec)
    hits: list[dict] = []
    for ts, lines in frames:
        text = frame_text(lines)
        for hit in scanner.find_brand_hits(text):
            hits.append({"ts": ts, "brand": hit.brand, "matched_text": hit.matched_text, "frame_text": text})
    return hits, len(frames)


def qc_ocr(video: Path, scratch: Path) -> list[dict]:
    try:
        hits, frame_count = scan_frames_for_brand_hits(video, scratch)
    except RuntimeError as e:
        return [{"file": video.name, "check": "ocr_brand_scan", "status": "FAIL", "detail": str(e)}]

    if hits:
        detail = [
            f"{h['ts']:.2f}s [{h['brand']}] \"{h['matched_text']}\" in \"{h['frame_text']}\""
            for h in hits
        ]
        return [{"file": video.name, "check": "ocr_brand_scan", "status": "FAIL", "detail": " | ".join(detail)}]
    return [{"file": video.name, "check": "ocr_brand_scan", "status": "PASS", "detail": f"{frame_count} 抽帧均无品牌词命中（间隔 {OCR_INTERVAL_SEC}s）"}]


def qc_one(video: Path, scratch: Path) -> list[dict]:
    print(f"  QC: {video.name}")
    rows = []
    rows += qc_audio(video, scratch)
    rows += qc_ocr(video, scratch)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="投放前预检：品牌词声轨+画面扫描")
    ap.add_argument("target", type=Path, help="成片文件或目录")
    ap.add_argument("--out", type=Path, default=Path("out/qc"), help="qc_report.csv 输出目录")
    args = ap.parse_args()

    # 实际要用 ffmpeg/OCR/whisper 之前显式严格校验（import config 本身不再退出）。
    config.validate_strict()

    target = args.target.resolve()
    if target.is_dir():
        videos = sorted(p for p in target.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    elif target.is_file():
        videos = [target]
    else:
        sys.exit(f"目标不存在: {target}")

    if not videos:
        sys.exit(f"没有找到视频文件: {target}")

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = out_dir / ".scratch"
    scratch.mkdir(exist_ok=True)

    all_rows = []
    for v in videos:
        all_rows += qc_one(v, scratch)

    try:
        scratch.rmdir()
    except OSError:
        pass

    report_path = out_dir / "qc_report.csv"
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "check", "status", "detail"])
        w.writeheader()
        w.writerows(all_rows)

    fail_rows = [r for r in all_rows if r["status"] == "FAIL"]
    print(f"\nqc_report.csv -> {report_path}")
    print(f"共 {len(all_rows)} 项检查，{len(fail_rows)} 项 FAIL")
    for r in fail_rows:
        print(f"  FAIL: {r['file']} / {r['check']}: {r['detail'][:200]}")

    if fail_rows:
        sys.exit(1)
    print("全部 PASS")


if __name__ == "__main__":
    main()
