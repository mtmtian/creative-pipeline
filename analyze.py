#!/usr/bin/env python3
"""
analyze.py <素材目录> [--out out/<批次名>] [--force]

四件套批处理：对目录下每个视频文件产出
  manifest.csv            file, rel_path, size_bytes, duration_sec, fingerprint, dup_of
  metadata.csv             ffprobe 逐文件元信息
  keyframes/<视频名>.jpg   0.3s/2s/5s/中点/尾帧 contact sheet
  transcript.csv           whisper-cli 转写（file, start, end, text）
  transcript_brand_hits.csv
  ocr_frames.csv           每 N 秒抽帧过 tesseract（file, ts, text）
  ocr_brand_hits.csv

支持断点续跑：某个文件在对应 csv 里已经有记录就跳过，--force 强制重跑全部。

OCR 抽帧间隔默认 2s（可在 config.py 的 OCR_INTERVAL_SEC 调回 1s，2s 是为了在真实素材上
跑得动，见 README 验收记录里的取舍说明）。转写只用 base 模型（本机没有 base.en，见
config.py 里的探测记录）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import config
import scanner

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

OCR_INTERVAL_SEC = getattr(config, "OCR_INTERVAL_SEC", 2.0)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe_json(path: Path) -> dict:
    cp = run([
        config.FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    if cp.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {path}\n{cp.stderr}")
    return json.loads(cp.stdout)


def get_duration_sec(probe: dict) -> float:
    fmt = probe.get("format", {})
    if fmt.get("duration"):
        return float(fmt["duration"])
    for s in probe.get("streams", []):
        if s.get("duration"):
            return float(s["duration"])
    return 0.0


def md5_prefix(path: Path, n: int = 12, chunk: int = 1024 * 1024) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        # 只读前几个 chunk 做指纹（大文件不必整读），配合时长一起够用于去重标记
        for _ in range(4):
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()[:n]


def csv_read_existing_files(csv_path: Path, key_col: str = "file") -> set[str]:
    if not csv_path.exists():
        return set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        return {row[key_col] for row in csv.DictReader(f) if row.get(key_col)}


# ---------------------------------------------------------------------------
# manifest.csv
# ---------------------------------------------------------------------------


def build_manifest(video_dir: Path, videos: list[Path], out_dir: Path, force: bool) -> None:
    manifest_path = out_dir / "manifest.csv"
    done = set() if force else csv_read_existing_files(manifest_path)
    rows = []
    if manifest_path.exists() and not force:
        with open(manifest_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    fingerprints: dict[str, str] = {r["file"]: r["fingerprint"] for r in rows}

    for v in videos:
        if v.name in done:
            continue
        try:
            probe = ffprobe_json(v)
            duration = get_duration_sec(probe)
        except Exception as e:
            print(f"  [manifest] ffprobe 失败，跳过指纹计算: {v.name}: {e}", file=sys.stderr)
            duration = 0.0
        size_bytes = v.stat().st_size
        fp = f"{md5_prefix(v)}_{duration:.1f}s"
        dup_of = ""
        for existing_name, existing_fp in fingerprints.items():
            if existing_fp == fp and existing_name != v.name:
                dup_of = existing_name
                break
        fingerprints[v.name] = fp
        rows.append({
            "file": v.name,
            "rel_path": str(v.relative_to(video_dir)),
            "size_bytes": size_bytes,
            "duration_sec": f"{duration:.3f}",
            "fingerprint": fp,
            "dup_of": dup_of,
        })

    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "rel_path", "size_bytes", "duration_sec", "fingerprint", "dup_of"])
        w.writeheader()
        w.writerows(rows)
    print(f"  manifest.csv 写入 {len(rows)} 行")


# ---------------------------------------------------------------------------
# metadata.csv
# ---------------------------------------------------------------------------


def build_metadata(videos: list[Path], out_dir: Path, force: bool) -> None:
    metadata_path = out_dir / "metadata.csv"
    rows = []
    if metadata_path.exists() and not force:
        with open(metadata_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    done = {r["file"] for r in rows}

    for v in videos:
        if v.name in done:
            continue
        try:
            probe = ffprobe_json(v)
        except Exception as e:
            print(f"  [metadata] ffprobe 失败: {v.name}: {e}", file=sys.stderr)
            continue
        fmt = probe.get("format", {})
        vstream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        astream = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None)

        duration = get_duration_sec(probe)
        fps = 0.0
        rate = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "0/1"
        try:
            num, den = rate.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 0.0
        except Exception:
            pass
        bitrate = fmt.get("bit_rate")
        bitrate_kbps = f"{int(bitrate) / 1000:.1f}" if bitrate else ""

        rows.append({
            "file": v.name,
            "duration_sec": f"{duration:.3f}",
            "width": vstream.get("width", ""),
            "height": vstream.get("height", ""),
            "fps": f"{fps:.3f}",
            "video_codec": vstream.get("codec_name", ""),
            "has_audio": "true" if astream else "false",
            "audio_codec": astream.get("codec_name", "") if astream else "",
            "bitrate_kbps": bitrate_kbps,
        })

    with open(metadata_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "file", "duration_sec", "width", "height", "fps",
            "video_codec", "has_audio", "audio_codec", "bitrate_kbps",
        ])
        w.writeheader()
        w.writerows(rows)
    print(f"  metadata.csv 写入 {len(rows)} 行")


# ---------------------------------------------------------------------------
# keyframes/<视频名>.jpg contact sheet
# ---------------------------------------------------------------------------


def build_keyframes(videos: list[Path], out_dir: Path, force: bool) -> None:
    kf_dir = out_dir / "keyframes"
    kf_dir.mkdir(exist_ok=True)
    ok, fail = 0, 0
    for v in videos:
        sheet_path = kf_dir / f"{v.stem}.jpg"
        if sheet_path.exists() and not force:
            continue
        try:
            probe = ffprobe_json(v)
            duration = get_duration_sec(probe)
        except Exception as e:
            print(f"  [keyframes] ffprobe 失败: {v.name}: {e}", file=sys.stderr)
            fail += 1
            continue

        # 5 个抽帧时间点，钳制到 [0, duration - 0.05]，避免超短视频抽帧越界
        max_ts = max(duration - 0.05, 0.0)
        candidates = [0.3, 2.0, 5.0, duration / 2.0, max(duration - 0.3, 0.0)]
        timestamps = sorted({min(max(t, 0.0), max_ts) for t in candidates})
        if not timestamps:
            timestamps = [0.0]

        frame_paths = []
        try:
            for i, ts in enumerate(timestamps):
                frame_path = kf_dir / f".{v.stem}_frame{i}.jpg"
                cp = run([
                    config.FFMPEG, "-y", "-ss", f"{ts:.3f}", "-i", str(v),
                    "-frames:v", "1", "-q:v", "3", str(frame_path),
                ])
                if cp.returncode == 0 and frame_path.exists():
                    frame_paths.append(frame_path)

            if not frame_paths:
                raise RuntimeError("没有成功抽到任何一帧")

            # hstack 拼接成一张 contact sheet
            inputs = []
            for fp in frame_paths:
                inputs += ["-i", str(fp)]
            n = len(frame_paths)
            filter_complex = "".join(f"[{i}:v]" for i in range(n)) + f"hstack=inputs={n}[out]"
            cp = run([
                config.FFMPEG, "-y", *inputs,
                "-filter_complex", filter_complex, "-map", "[out]",
                str(sheet_path),
            ])
            if cp.returncode != 0:
                raise RuntimeError(f"hstack 拼接失败: {cp.stderr}")
            ok += 1
        except Exception as e:
            print(f"  [keyframes] 失败: {v.name}: {e}", file=sys.stderr)
            fail += 1
        finally:
            for fp in frame_paths:
                fp.unlink(missing_ok=True)

    print(f"  keyframes: 成功 {ok}，失败 {fail}")


# ---------------------------------------------------------------------------
# transcript.csv + transcript_brand_hits.csv
# ---------------------------------------------------------------------------


def extract_audio_wav(video: Path, wav_path: Path) -> bool:
    cp = run([
        config.FFMPEG, "-y", "-i", str(video),
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path),
    ])
    return cp.returncode == 0 and wav_path.exists()


def _whisper_json(wav_path: Path, language: str) -> dict | None:
    out_prefix = wav_path.with_suffix("")
    cp = run([
        config.WHISPER_CLI,
        "-m", config.WHISPER_MODEL,
        "-l", language,
        "-oj", "-of", str(out_prefix),
        # 注意：不能加 -nt。-nt 会让 whisper-cli 的 json 输出退化成一整段
        # 假 offsets（固定 0~30000ms），不是真实分段时间戳；segments 级时间戳
        # 必须去掉 -nt 才能拿到（实测验证，见 README 验收记录）。
        str(wav_path),
    ])
    json_path = Path(str(out_prefix) + ".json")
    if not json_path.exists():
        print(f"    whisper-cli 未产出 json（returncode={cp.returncode}）: {cp.stderr[-500:]}", file=sys.stderr)
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"    whisper json 解析失败: {e}", file=sys.stderr)
        return None
    finally:
        json_path.unlink(missing_ok=True)


def _whisper_segments(data: dict) -> list[dict]:
    segments = []
    for t in data.get("transcription", []):
        offsets = t.get("offsets", {})
        segments.append({
            "start": offsets.get("from", 0) / 1000.0,
            "end": offsets.get("to", 0) / 1000.0,
            "text": t.get("text", "").strip(),
        })
    return segments


def whisper_transcribe(wav_path: Path) -> list[dict]:
    """调用 whisper-cli（固定英语）输出 json，解析出 segments。"""
    data = _whisper_json(wav_path, "en")
    return _whisper_segments(data) if data else []


def whisper_transcribe_detect(wav_path: Path) -> tuple[list[dict], str | None]:
    """自动识别语种的转写：返回 (segments, whisper 识别出的语种代码如 'en'/'ja')。"""
    data = _whisper_json(wav_path, "auto")
    if not data:
        return [], None
    return _whisper_segments(data), (data.get("result") or {}).get("language")


def build_transcript(videos: list[Path], out_dir: Path, force: bool) -> tuple[int, int]:
    transcript_path = out_dir / "transcript.csv"
    hits_path = out_dir / "transcript_brand_hits.csv"
    scratch = out_dir / ".scratch"
    scratch.mkdir(exist_ok=True)

    rows = []
    hit_rows = []
    if transcript_path.exists() and not force:
        with open(transcript_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    if hits_path.exists() and not force:
        with open(hits_path, newline="", encoding="utf-8") as f:
            hit_rows = list(csv.DictReader(f))
    done = {r["file"] for r in rows}

    empty_count = 0
    hit_files = set()

    for v in videos:
        if v.name in done:
            continue
        wav_path = scratch / f"{v.stem}.wav"
        if not extract_audio_wav(v, wav_path):
            print(f"  [transcript] 抽音频失败（可能无音轨）: {v.name}", file=sys.stderr)
            rows.append({"file": v.name, "start": "", "end": "", "text": ""})
            empty_count += 1
            continue
        segments = whisper_transcribe(wav_path)
        wav_path.unlink(missing_ok=True)

        if not segments:
            rows.append({"file": v.name, "start": "", "end": "", "text": ""})
            empty_count += 1
            continue

        for seg in segments:
            rows.append({
                "file": v.name,
                "start": f"{seg['start']:.2f}",
                "end": f"{seg['end']:.2f}",
                "text": seg["text"],
            })
            for hit in scanner.find_brand_hits(seg["text"]):
                hit_rows.append({
                    "file": v.name, "start": f"{seg['start']:.2f}", "end": f"{seg['end']:.2f}",
                    "brand": hit.brand, "matched_text": hit.matched_text,
                })
                hit_files.add(v.name)

    with open(transcript_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "start", "end", "text"])
        w.writeheader()
        w.writerows(rows)
    with open(hits_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "start", "end", "brand", "matched_text"])
        w.writeheader()
        w.writerows(hit_rows)

    try:
        scratch.rmdir()
    except OSError:
        pass

    # 汇总统计从最终落盘内容重新算，而不是只累加"本次新处理"的部分，
    # 这样断点续跑（本次全部跳过）时汇总数字依然准确。
    empty_count_final = sum(1 for r in rows if not r.get("text"))
    hit_files_final = {r["file"] for r in hit_rows}
    print(f"  transcript.csv 写入 {len(rows)} 行，{len(hit_rows)} 处品牌命中（{len(hit_files_final)} 个文件）")
    return empty_count_final, len(hit_files_final)


# ---------------------------------------------------------------------------
# ocr_frames.csv + ocr_brand_hits.csv
# ---------------------------------------------------------------------------


def build_ocr(videos: list[Path], out_dir: Path, force: bool) -> int:
    ocr_path = out_dir / "ocr_frames.csv"
    hits_path = out_dir / "ocr_brand_hits.csv"
    scratch = out_dir / ".scratch"
    scratch.mkdir(exist_ok=True)

    rows = []
    hit_rows = []
    if ocr_path.exists() and not force:
        with open(ocr_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    if hits_path.exists() and not force:
        with open(hits_path, newline="", encoding="utf-8") as f:
            hit_rows = list(csv.DictReader(f))
    done = {r["file"] for r in rows}
    hit_files = set()

    for v in videos:
        if v.name in done:
            continue
        try:
            probe = ffprobe_json(v)
            duration = get_duration_sec(probe)
        except Exception as e:
            print(f"  [ocr] ffprobe 失败: {v.name}: {e}", file=sys.stderr)
            continue

        ts = 0.0
        while ts < duration:
            frame_path = scratch / f".{v.stem}_{ts:.2f}.jpg"
            cp = run([
                config.FFMPEG, "-y", "-ss", f"{ts:.3f}", "-i", str(v),
                "-frames:v", "1", "-q:v", "3", str(frame_path),
            ])
            text = ""
            if cp.returncode == 0 and frame_path.exists():
                ocr_cp = run([config.TESSERACT, str(frame_path), "stdout"])
                if ocr_cp.returncode == 0:
                    text = " ".join(ocr_cp.stdout.split())
                frame_path.unlink(missing_ok=True)
            rows.append({"file": v.name, "ts": f"{ts:.2f}", "text": text})
            for hit in scanner.find_brand_hits(text):
                hit_rows.append({"file": v.name, "ts": f"{ts:.2f}", "brand": hit.brand, "matched_text": hit.matched_text})
                hit_files.add(v.name)
            ts += OCR_INTERVAL_SEC

    with open(ocr_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "ts", "text"])
        w.writeheader()
        w.writerows(rows)
    with open(hits_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["file", "ts", "brand", "matched_text"])
        w.writeheader()
        w.writerows(hit_rows)

    try:
        scratch.rmdir()
    except OSError:
        pass

    hit_files_final = {r["file"] for r in hit_rows}
    print(f"  ocr_frames.csv 写入 {len(rows)} 行，{len(hit_rows)} 处品牌命中（{len(hit_files_final)} 个文件，间隔 {OCR_INTERVAL_SEC}s）")
    return len(hit_files_final)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="竞品素材四件套批处理：manifest/metadata/keyframes/transcript/ocr")
    ap.add_argument("video_dir", type=Path, help="素材目录")
    ap.add_argument("--out", type=Path, default=None, help="输出目录，默认 out/<素材目录名>")
    ap.add_argument("--force", action="store_true", help="强制重跑，忽略已有产物")
    args = ap.parse_args()

    # 实际要用 ffmpeg/tesseract/whisper 之前显式严格校验（import config 本身不再退出）。
    config.validate_strict()

    video_dir = args.video_dir.resolve()
    if not video_dir.is_dir():
        sys.exit(f"素材目录不存在: {video_dir}")

    out_dir = (args.out or Path("out") / video_dir.name).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    videos = sorted(p for p in video_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not videos:
        sys.exit(f"目录下没有找到视频文件（支持后缀: {sorted(VIDEO_EXTS)}）: {video_dir}")

    print(f"素材目录: {video_dir}")
    print(f"输出目录: {out_dir}")
    print(f"共 {len(videos)} 个视频文件{'（--force 强制重跑）' if args.force else ''}")

    print("[1/5] manifest.csv ...")
    build_manifest(video_dir, videos, out_dir, args.force)

    print("[2/5] metadata.csv ...")
    build_metadata(videos, out_dir, args.force)

    print("[3/5] keyframes ...")
    build_keyframes(videos, out_dir, args.force)

    print("[4/5] transcript ...")
    empty_transcripts, brand_audio_files = build_transcript(videos, out_dir, args.force)

    print("[5/5] ocr ...")
    brand_ocr_files = build_ocr(videos, out_dir, args.force)

    print()
    print("===== 汇总 =====")
    print(f"处理文件数: {len(videos)}")
    print(f"转写为空的文件数: {empty_transcripts}")
    print(f"声轨命中品牌词的文件数: {brand_audio_files}")
    print(f"OCR 命中品牌词的文件数: {brand_ocr_files}")


if __name__ == "__main__":
    main()
