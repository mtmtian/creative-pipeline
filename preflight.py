#!/usr/bin/env python3
"""preflight.py：成片在真实投放花钱前的可用性预检。

    python3 preflight.py <成片或目录...> --profile hakko-pc [--locale US] [--channel google|tiktok] [--out DIR]

判断标准是“能不能在广告平台上用”，不是内部交付规格：编码、采样率、帧率只记录不判定
（新产出的统一规格由 batch_pipeline.py qc 把关）。每条成片给出 FAIL / WARN / PASS：
  FAIL  不可投，必须修：读不出视频、超过平台上传上限、短于 5 秒、解码错误、开场黑屏、全片无声、
        音画时长差过大、声轨或画面出现该产品的竞品品牌词、口播语种与地区不符
  WARN  需要人看一眼再决定：画幅不在 16:9/1:1/4:5/9:16、疑似字幕被裁到画面边缘、结尾没认出品牌/CTA、
        响度异常、长时间冻帧或静音、时长不在渠道推荐区间
  PASS  自动检查没发现问题；hook 吸引力、尺度、遮标质量仍由人在审片页上判断

产物写到 <out>/：report.json、report.html（审片页，每条附开头 3 秒 / 全片 / 结尾 3 秒抽帧）、sheets/。
任一成片 FAIL 时退出码为 1。只做本地处理，不上传任何东西。
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import sys
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import config
import scanner
from analyze import (VIDEO_EXTS, extract_audio_wav, ffprobe_json, frame_text, ocr_video, run,
                     whisper_transcribe_detect)
from batch_pipeline import FORMAT_SPECS, LOCALE_RE, parse_creative_id

REPO_DIR = Path(__file__).resolve().parent
LOCALE_LANGUAGE = {"US": "en", "GB": "en", "CA": "en", "AU": "en", "JP": "ja", "ES": "es", "MX": "es",
                   "BR": "pt", "DE": "de", "FR": "fr", "KR": "ko"}
CHANNEL_SECONDS = {"google": (10.0, 180.0), "tiktok": (9.0, 60.0)}
HARD_MIN_SECONDS = 5.0      # 以下三项与 google-ads-pc-campaign-launch 的上传校验一致
VIDEO_MAX_BYTES = 256 * 1024 ** 3
ASPECT_TOLERANCE = 0.01
HOOK_WINDOW = 3.0           # 开头这几秒决定留存，黑屏/冻帧/无声在这里更严
ENDCARD_WINDOW = 3.0
MIN_SPEECH_WORDS = 5        # 少于这么多词不判语种（纯音乐时 whisper 的语种不可信）
OCR_INTERVAL = 1.0
EDGE_SHARE = 0.03           # 文字框贴到左右 3% 以内视为贴边
CAPTION_MIN_HEIGHT = 0.025  # 只看字幕大小的字，过滤游戏 HUD 小字
CAPTION_MIN_WORDS = 3       # 至少 3 个词的一行才当字幕行
TRUE_PEAK_WARN = 1.0        # 投放母带常把峰值压在 0 dBFS，平台不拒；超过 +1 dBFS 才算明显削波
STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}


def result(check: str, status: str, detail: str) -> dict:
    return {"check": check, "status": status, "detail": detail}


def overall(results: list[dict]) -> str:
    return max((r["status"] for r in results), key=STATUS_ORDER.__getitem__, default="PASS")


# --------------------------------------------------------------------------- metadata

def infer_format(width: int, height: int) -> str | None:
    """16:9 / 1:1 / 9:16 / 4:5 中与画面比例相差不超过 1% 的那个。"""
    if not width or not height:
        return None
    for name in FORMAT_SPECS:
        ratio_w, ratio_h = (int(v) for v in name.split(":"))
        if abs((width / height) / (ratio_w / ratio_h) - 1.0) <= ASPECT_TOLERANCE:
            return name
    return None


def infer_locale(path: Path, default: str | None) -> str | None:
    """素材 ID 文件名或 50_delivery/<画幅>/<地区>/ 目录优先，其次 --locale。"""
    try:
        return parse_creative_id(path.stem)["locale"].upper()
    except ValueError:
        pass
    if LOCALE_RE.fullmatch(path.parent.name):
        return path.parent.name
    return default


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_summary(probe: dict) -> dict:
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    fps = _number(num) / _number(den) if _number(num) and _number(den) else None
    return {
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "fps": round(fps, 3) if fps else None,
        "duration": _number((probe.get("format") or {}).get("duration")) or _number(video.get("duration")) or 0.0,
        "video_duration": _number(video.get("duration")),
        "audio_duration": _number(audio.get("duration")) if audio else None,
        "has_audio": audio is not None,
    }


# --------------------------------------------------------------------------- picture and sound health

def media_scan_command(path: Path, has_audio: bool) -> list[str]:
    command = [config.FFMPEG, "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
               "-map", "0:v:0", "-vf", "blackdetect=d=0.3:pix_th=0.10,freezedetect=n=-60dB:d=1.5"]
    if has_audio:
        command += ["-map", "0:a:0", "-af", "silencedetect=noise=-50dB:d=1,ebur128=peak=true"]
    return command + ["-f", "null", "-"]


def _intervals(starts: list[float], ends: list[float], duration: float) -> list[tuple[float, float]]:
    """Pair start/end events in order; an interval still open at the end runs to the end of the file."""
    return [(max(0.0, start), min(duration, ends[i] if i < len(ends) else duration))
            for i, start in enumerate(starts)]


def parse_media_log(log: str, duration: float) -> dict:
    black = [(float(a), float(b)) for a, b in
             re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", log)]
    freeze = _intervals([float(v) for v in re.findall(r"freeze_start:\s*([\d.]+)", log)],
                        [float(v) for v in re.findall(r"freeze_end:\s*([\d.]+)", log)], duration)
    silence = _intervals([float(v) for v in re.findall(r"silence_start:\s*(-?[\d.]+)", log)],
                         [float(v) for v in re.findall(r"silence_end:\s*([\d.]+)", log)], duration)
    summary = log[log.rfind("Summary:"):] if "Summary:" in log else ""
    lufs = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", summary)
    peak = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    errors = [line.strip() for line in log.splitlines()
              if re.match(r"\[[^\]]+@ 0x[0-9a-f]+\]", line.strip()) and "Parsed_" not in line
              and re.search(r"error|corrupt|invalid|non-existing|missing picture", line, re.IGNORECASE)]
    return {
        "black": black,
        "freeze": freeze,
        "silence": silence,
        "lufs": float(lufs.group(1)) if lufs else None,
        "true_peak": (-math.inf if peak.group(1) == "-inf" else float(peak.group(1))) if peak else None,
        "errors": errors,
    }


def _span(a: float, b: float) -> str:
    return f"{a:.1f}-{b:.1f}s"


def judge_media(scan: dict, duration: float, has_audio: bool) -> list[dict]:
    results = []
    if scan["errors"]:
        results.append(result("decode", "FAIL", f"解码错误 {len(scan['errors'])} 处：{scan['errors'][0][:160]}"))
    else:
        results.append(result("decode", "PASS", "完整解码无错误"))

    black_total = sum(b - a for a, b in scan["black"])
    black_issues = []
    for a, b in scan["black"]:
        if a < HOOK_WINDOW and b - a >= 0.5:
            black_issues.append(("FAIL", f"开场 {_span(a, b)} 黑屏"))
        elif b - a >= 1.0:
            black_issues.append(("WARN", f"{_span(a, b)} 黑屏 {b - a:.1f}s"))
    if duration and black_total / duration >= 0.3:
        black_issues.append(("FAIL", f"黑屏累计 {black_total:.1f}s，占 {black_total / duration:.0%}"))
    results += [result("black", s, d) for s, d in black_issues] or [result("black", "PASS", "无明显黑屏")]

    # 结尾的静态尾帧本来就不动，不算冻帧。
    endcard_start = duration - ENDCARD_WINDOW - 1.0
    freezes = [(a, b) for a, b in scan["freeze"] if not (a >= endcard_start and b >= duration - 0.3)]
    freeze_total = sum(b - a for a, b in freezes)
    freeze_issues = []
    if duration and freeze_total / duration >= 0.5:
        freeze_issues.append(("FAIL", f"画面静止累计 {freeze_total:.1f}s，占 {freeze_total / duration:.0%}"))
    for a, b in freezes:
        if a < 1.0 and b - a >= 2.0:
            freeze_issues.append(("WARN", f"开场 {_span(a, b)} 画面静止"))
        elif b - a >= 3.0:
            freeze_issues.append(("WARN", f"{_span(a, b)} 画面静止 {b - a:.1f}s"))
    results += [result("freeze", s, d) for s, d in freeze_issues] or [result("freeze", "PASS", "无长时间静止画面")]

    if not has_audio:
        return results  # 没有音轨由规格检查判 FAIL
    silent_total = sum(b - a for a, b in scan["silence"])
    lufs, peak = scan["lufs"], scan["true_peak"]
    if (duration and silent_total / duration >= 0.95) or (lufs is not None and lufs <= -60):
        results.append(result("silence", "FAIL", "全片无声"))
        return results
    silence_issues = []
    for a, b in scan["silence"]:
        if a <= 0.1 and b - a >= 1.5:
            silence_issues.append(f"开场 {b - a:.1f}s 无声")
        elif b - a >= 3.0 and a < endcard_start:
            silence_issues.append(f"{_span(a, b)} 无声 {b - a:.1f}s")
    results += [result("silence", "WARN", d) for d in silence_issues] or [result("silence", "PASS", "无长段静音")]
    loud = []
    if lufs is not None and lufs < -24:
        loud.append(f"整体偏小声 I={lufs:.1f} LUFS")
    if lufs is not None and lufs > -8:
        loud.append(f"整体过响 I={lufs:.1f} LUFS")
    if peak is not None and peak > TRUE_PEAK_WARN:
        loud.append(f"真峰值 {peak:.1f} dBFS，明显削波")
    if loud:
        results += [result("loudness", "WARN", d) for d in loud]
    elif lufs is not None:
        note = "（峰值顶格，转码后可能有轻微爆音）" if peak is not None and peak > -1.0 else ""
        results.append(result("loudness", "PASS", f"I={lufs:.1f} LUFS，真峰值 {peak:.1f} dBFS{note}"))
    return results


def encoding_note(probe: dict) -> str:
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = probe_summary(probe)["fps"]
    parts = [f"{video.get('codec_name', '?')}", f"{fps:g}fps" if fps else "帧率未知"]
    if audio:
        parts.append(f"{audio.get('codec_name', '?')} {audio.get('sample_rate', '?')}Hz {audio.get('channels', '?')}ch")
    return " ".join(parts)


def judge_platform(probe: dict, size_bytes: int) -> list[dict]:
    """广告平台能不能接：只拦平台会拒的情况，编码细节只记录。"""
    info = probe_summary(probe)
    if not any(s.get("codec_type") == "video" for s in probe.get("streams", [])) or not info["width"]:
        return [result("platform", "FAIL", "没有可读的视频流")]
    if size_bytes > VIDEO_MAX_BYTES:
        return [result("platform", "FAIL", f"文件 {size_bytes / 1024 ** 3:.1f} GiB，超过平台上传上限 256 GiB")]
    format_name = infer_format(info["width"], info["height"])
    note = f"{info['width']}x{info['height']}，{encoding_note(probe)}（编码只作记录，不影响投放）"
    if format_name is None:
        return [result("platform", "WARN", f"{note}；画幅不是 16:9/1:1/4:5/9:16，部分版位会加黑边或裁切")]
    return [result("platform", "PASS", f"{format_name} {note}")]


def judge_duration(info: dict, channel: str | None) -> list[dict]:
    duration = info["duration"]
    results = []
    if duration < HARD_MIN_SECONDS:
        results.append(result("duration", "FAIL", f"时长 {duration:.1f}s，短于 {HARD_MIN_SECONDS:.0f}s"))
    elif channel in CHANNEL_SECONDS:
        low, high = CHANNEL_SECONDS[channel]
        if not low <= duration <= high:
            results.append(result("duration", "WARN", f"时长 {duration:.1f}s，不在 {channel} 推荐的 {low:.0f}-{high:.0f}s"))
    if not results:
        results.append(result("duration", "PASS", f"时长 {duration:.1f}s"))
    video, audio = info["video_duration"], info["audio_duration"]
    if video is not None and audio is not None:
        gap = abs(video - audio)
        if gap > 1.5:
            results.append(result("av_sync", "FAIL", f"视频 {video:.2f}s / 音频 {audio:.2f}s，相差 {gap:.2f}s"))
        elif gap > 0.5:
            results.append(result("av_sync", "WARN", f"视频 {video:.2f}s / 音频 {audio:.2f}s，相差 {gap:.2f}s"))
    return results


# --------------------------------------------------------------------------- speech

def judge_speech(segments: list[dict], language: str | None, locale: str | None,
                 brands: list[str]) -> list[dict]:
    results = []
    hits = [f"{s['start']:.1f}s [{h.brand}] “{s['text']}”"
            for s in segments for h in scanner.find_brand_hits(s["text"], brands)]
    if not brands:
        results.append(result("speech_brand", "PASS", "该产品没有竞品词表，未扫描"))
    elif hits:
        results.append(result("speech_brand", "FAIL", "口播出现竞品品牌词：" + "；".join(hits[:5])))
    else:
        results.append(result("speech_brand", "PASS", "口播无竞品品牌词"))
    words = sum(len(re.findall(r"\w+", s["text"])) for s in segments)
    expected = LOCALE_LANGUAGE.get(locale or "")
    if words < MIN_SPEECH_WORDS:
        results.append(result("speech_language", "PASS", "几乎没有口播，不判语种"))
    elif locale is None:
        results.append(result("speech_language", "PASS", f"口播语种 {language}（未指定地区，未比对）"))
    elif expected is None:
        results.append(result("speech_language", "WARN", f"口播语种 {language}；地区 {locale} 没有对应语种表，请人工确认"))
    elif language != expected:
        results.append(result("speech_language", "FAIL", f"口播语种 {language}，与地区 {locale}（应为 {expected}）不符"))
    else:
        results.append(result("speech_language", "PASS", f"口播语种 {language}，{words} 词"))
    return results


def transcribe(path: Path, scratch: Path) -> tuple[list[dict], str | None]:
    wav = scratch / "audio.wav"
    if not extract_audio_wav(path, wav):
        return [], None
    try:
        return whisper_transcribe_detect(wav)
    finally:
        wav.unlink(missing_ok=True)


# --------------------------------------------------------------------------- on-screen text

def caption_words(text: str) -> int:
    """一行字的词数；日文、中文这类不用空格分词的宽字符按约 3 个字一个词折算。"""
    words = sum(1 for w in text.split() if any(c.isalnum() for c in w))
    wide = sum(1 for c in text if unicodedata.east_asian_width(c) in "WF")
    return max(words, wide // 3)


def judge_ocr(frames: list[tuple[float, list[dict]]], width: int, height: int, duration: float,
              endcard_tokens: list[str], brands: list[str]) -> list[dict]:
    """frames 是 analyze.ocr_video 的结果：[(秒数, 文字行), ...]。"""
    results = []
    hits = []
    for ts, lines in frames:
        hits += [f"{ts:.0f}s [{h.brand}] “{h.matched_text}”" for h in scanner.find_brand_hits(frame_text(lines), brands)]
    if not brands:
        results.append(result("screen_brand", "PASS", "该产品没有竞品词表，未扫描"))
    elif hits:
        results.append(result("screen_brand", "FAIL", "画面文字出现竞品品牌词：" + "；".join(hits[:5])))
    else:
        results.append(result("screen_brand", "PASS", f"{len(frames)} 帧画面文字无竞品品牌词"))

    # 字幕被裁的样子是"一整行字横跨画面中线、顶到左右边缘"；单个词贴边、角落里的游戏 HUD、主播 ID、
    # 聊天浮窗不跨中线，都不算（Vision 认得出这些小字，2026-10 在 PC 人力批次上校准）。
    edge_frames = []
    for ts, lines in frames:
        for line in lines:
            left, right = line["left"], line["left"] + line["width"]
            if (line["conf"] >= 60 and line["height"] >= CAPTION_MIN_HEIGHT * height
                    and caption_words(line["text"]) >= CAPTION_MIN_WORDS
                    and left < width / 2 < right
                    and (left <= EDGE_SHARE * width or right >= (1 - EDGE_SHARE) * width)):
                edge_frames.append(f"{ts:.0f}s “{line['text'][:60]}”")
                break
    if len(edge_frames) >= 2:
        results.append(result("text_edge", "WARN",
                              f"{len(edge_frames)} 帧的字幕行顶到画面左右边缘，疑似被裁或没留安全边距：" + "；".join(edge_frames[:3])))
    else:
        results.append(result("text_edge", "PASS", "没有贴边被裁的字幕"))

    if endcard_tokens:
        # 按整行文字匹配，"app store" 这类多词 token 才认得出来
        tail = " ".join(frame_text(lines) for ts, lines in frames if ts >= duration - ENDCARD_WINDOW).lower()
        found = sorted(t for t in endcard_tokens if t in tail)
        if found:
            results.append(result("endcard", "PASS", f"结尾识别到 {', '.join(found)}"))
        else:
            results.append(result("endcard", "WARN", "结尾 3 秒没认出品牌/CTA 文字；风格化 logo OCR 认不出时请看审片页"))
    return results


# --------------------------------------------------------------------------- review sheet

def sheet_times(duration: float) -> list[float]:
    """开头 3 秒 6 帧、中段 6 帧、结尾 3 秒 6 帧。"""
    last = max(0.0, duration - 0.05)
    head = [min(last, t) for t in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)]
    middle_start = min(last, HOOK_WINDOW)
    middle_end = max(middle_start, duration - ENDCARD_WINDOW)
    middle = [middle_start + (middle_end - middle_start) * (i + 0.5) / 6 for i in range(6)]
    tail = [max(0.0, last - ENDCARD_WINDOW + ENDCARD_WINDOW * i / 5) for i in range(6)]
    return head + middle + tail


def contact_sheet(path: Path, info: dict, out: Path, scratch: Path) -> Path | None:
    thumb_h = 240
    thumb_w = max(2, int(round(thumb_h * info["width"] / max(1, info["height"]) / 2)) * 2)
    for index, t in enumerate(sheet_times(info["duration"])):
        frame = scratch / f"sheet_{index:02d}.jpg"
        label = f"drawtext=text='{t:.1f}s':x=6:y=6:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.6"
        completed = run([config.FFMPEG, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                         "-vf", f"scale={thumb_w}:{thumb_h},setsar=1,{label}", str(frame)])
        if completed.returncode != 0 or not frame.exists():
            run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:s={thumb_w}x{thumb_h}",
                 "-frames:v", "1", str(frame)])
    out.parent.mkdir(parents=True, exist_ok=True)
    completed = run([config.FFMPEG, "-v", "error", "-y", "-framerate", "1", "-i", str(scratch / "sheet_%02d.jpg"),
                     "-vf", "tile=6x3:padding=4:color=0x202020", "-frames:v", "1", str(out)])
    for frame in scratch.glob("sheet_*.jpg"):
        frame.unlink(missing_ok=True)
    return out if completed.returncode == 0 and out.exists() else None


# --------------------------------------------------------------------------- orchestration

def check_video(path: Path, profile: dict, locale: str | None, channel: str | None, out_dir: Path) -> dict:
    locale = infer_locale(path, locale)
    entry = {"file": str(path), "name": path.name, "locale": locale, "results": [], "transcript": []}
    scratch = Path(tempfile.mkdtemp(prefix=".preflight-", dir=out_dir))
    try:
        try:
            probe = ffprobe_json(path)
        except (RuntimeError, json.JSONDecodeError) as e:
            entry["results"].append(result("probe", "FAIL", f"ffprobe 读不了文件：{e}"))
            entry["status"] = "FAIL"
            return entry
        info = probe_summary(probe)
        entry.update({k: info[k] for k in ("width", "height", "fps", "duration")})
        entry["format"] = infer_format(info["width"], info["height"])
        entry["encoding"] = encoding_note(probe)
        entry["results"] += judge_platform(probe, path.stat().st_size)
        entry["results"] += judge_duration(info, channel)

        scan = parse_media_log(run(media_scan_command(path, info["has_audio"])).stderr, info["duration"])
        entry["lufs"], entry["true_peak"] = scan["lufs"], scan["true_peak"]
        entry["results"] += judge_media(scan, info["duration"], info["has_audio"])

        segments, language = transcribe(path, scratch) if info["has_audio"] else ([], None)
        entry["language"], entry["transcript"] = language, segments
        brands = profile.get("competitor_brands", [])
        entry["results"] += judge_speech(segments, language, locale, brands)

        try:
            frames = ocr_video(path, scratch, OCR_INTERVAL, LOCALE_LANGUAGE.get(locale or "", "en"))
            entry["results"] += judge_ocr(frames, info["width"], info["height"], info["duration"],
                                          profile.get("endcard_tokens", []), brands)
        except RuntimeError as e:
            entry["results"].append(result("screen_brand", "FAIL", f"画面文字扫描失败：{e}"))

        sheet = contact_sheet(path, info, out_dir / "sheets" / f"{path.stem}.jpg", scratch)
        entry["sheet"] = str(sheet.relative_to(out_dir)) if sheet else None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    entry["status"] = overall(entry["results"])
    return entry


def collect(paths: list[Path]) -> list[Path]:
    videos = []
    for path in paths:
        if path.is_dir():
            videos += sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                             and not any(part.startswith(".") for part in p.relative_to(path).parts))
        elif path.is_file() and path.suffix.lower() in VIDEO_EXTS:
            videos.append(path)
        else:
            raise ValueError(f"不是视频文件或目录：{path}")
    return list(dict.fromkeys(p.resolve() for p in videos))


STATUS_STYLE = {"FAIL": "fail", "WARN": "warn", "PASS": "pass"}


def render_html(report: dict) -> str:
    sections = []
    for entry in sorted(report["videos"], key=lambda e: (-STATUS_ORDER[e["status"]], e["name"])):
        issues = sorted((r for r in entry["results"] if r["status"] != "PASS"),
                        key=lambda r: -STATUS_ORDER[r["status"]])
        issue_html = "".join(f"<li class='{STATUS_STYLE[r['status']]}'><b>{r['status']}</b> "
                             f"{html.escape(r['check'])}：{html.escape(r['detail'])}</li>" for r in issues)
        passed = "".join(f"<li>{html.escape(r['check'])}：{html.escape(r['detail'])}</li>"
                         for r in entry["results"] if r["status"] == "PASS")
        transcript = "<br>".join(f"{s['start']:.1f}s {html.escape(s['text'])}" for s in entry.get("transcript", []))
        meta = " · ".join(str(v) for v in (
            entry.get("format") or "非常规画幅", f"{entry.get('width')}x{entry.get('height')}",
            entry.get("encoding") or "", f"{entry.get('duration') or 0:.1f}s", entry.get("locale") or "地区未知",
            f"口播 {entry.get('language') or '-'}"))
        sheet = f"<img src='{html.escape(entry['sheet'])}' alt='抽帧' loading='lazy'>" if entry.get("sheet") else ""
        sections.append(f"""
<section class="video {STATUS_STYLE[entry['status']]}">
  <h2><span class="badge {STATUS_STYLE[entry['status']]}">{entry['status']}</span> {html.escape(entry['name'])}</h2>
  <p class="meta">{html.escape(meta)}</p>
  <ul>{issue_html or "<li class='pass'>自动检查没有发现问题</li>"}</ul>
  {sheet}
  <p class="hint">第 1 行开头 3 秒（hook），第 2 行中段，第 3 行结尾 3 秒（尾帧）。仍需人工判断：hook 是否抓人、尺度、遮标质量、字幕可读性。</p>
  <details><summary>通过的检查</summary><ul>{passed}</ul></details>
  <details><summary>口播转写</summary><p>{transcript or '（无口播）'}</p></details>
</section>""")
    counts = report["counts"]
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>成片可用性预检</title>
<style>
:root {{ --bg:#fafafa; --fg:#1d1d1f; --muted:#666; --card:#fff; --line:#e5e5e5; --fail:#c62828; --warn:#a15c00; --pass:#2e7d32; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --fg:#eee; --muted:#9a9a9a; --card:#1e1e1e; --line:#333;
  --fail:#ef5350; --warn:#ffb74d; --pass:#81c784; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, "PingFang SC", sans-serif;
  margin:0 auto; max-width:1280px; padding:24px 16px; }}
.counts b {{ margin-right:16px; }}
.video {{ background:var(--card); border:1px solid var(--line); border-left:4px solid var(--line); border-radius:8px;
  padding:12px 16px; margin:16px 0; }}
.video.fail {{ border-left-color:var(--fail); }} .video.warn {{ border-left-color:var(--warn); }}
.video.pass {{ border-left-color:var(--pass); }}
.badge {{ font-size:12px; padding:2px 8px; border-radius:4px; color:#fff; vertical-align:middle; }}
.badge.fail {{ background:var(--fail); }} .badge.warn {{ background:var(--warn); }} .badge.pass {{ background:var(--pass); }}
h2 {{ font-size:16px; margin:4px 0; word-break:break-all; }} .meta, .hint {{ color:var(--muted); font-size:13px; }}
li.fail {{ color:var(--fail); }} li.warn {{ color:var(--warn); }} img {{ max-width:100%; border-radius:4px; }}
</style></head><body>
<h1>成片可用性预检</h1>
<p class="meta">{html.escape(report['created_at'])} · profile {html.escape(report['profile'])} · 渠道 {html.escape(report.get('channel') or '未指定')}</p>
<p class="counts"><b style="color:var(--fail)">FAIL {counts['FAIL']}</b><b style="color:var(--warn)">WARN {counts['WARN']}</b><b style="color:var(--pass)">PASS {counts['PASS']}</b></p>
<p class="hint">FAIL 不可投，必须修；WARN 需要人看一眼；PASS 只代表自动检查没发现问题。</p>
{''.join(sections)}
</body></html>
"""


def run_preflight(paths: list[Path], profile_name: str, locale: str | None, channel: str | None,
                  out_dir: Path, workers: int = 3) -> dict:
    profile = config.get_product_profile(profile_name)
    videos = collect(paths)
    if not videos:
        raise ValueError("没有找到视频文件")
    out_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(videos)))) as pool:
        entries = list(pool.map(lambda v: check_video(v, profile, locale, channel, out_dir), videos))
    counts = {status: sum(e["status"] == status for e in entries) for status in STATUS_ORDER}
    report = {"created_at": datetime.now().astimezone().isoformat(timespec="seconds"), "profile": profile_name,
              "channel": channel, "counts": counts, "videos": entries}
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    (out_dir / "report.html").write_text(render_html(report))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="成片投放前可用性预检")
    parser.add_argument("paths", nargs="+", type=Path, help="成片文件或目录（目录会递归）")
    parser.add_argument("--profile", required=True, choices=config.batch_profiles())
    parser.add_argument("--locale", help="成片目标地区（US/JP…）；文件名是素材 ID 或位于 <地区>/ 目录时自动识别")
    parser.add_argument("--channel", choices=sorted(CHANNEL_SECONDS), help="按渠道检查推荐时长")
    parser.add_argument("--out", type=Path, help="默认 out/preflight/<时间>")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)
    if args.locale and not LOCALE_RE.fullmatch(args.locale):
        parser.error("--locale 必须是两位大写地区码")
    config.validate_strict()
    out_dir = (args.out or REPO_DIR / "out" / "preflight" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    report = run_preflight(args.paths, args.profile, args.locale, args.channel, out_dir, args.workers)
    for entry in report["videos"]:
        print(f"{entry['status']:4} {entry['name']}")
        for r in sorted((r for r in entry["results"] if r["status"] != "PASS"),
                        key=lambda r: -STATUS_ORDER[r["status"]]):
            print(f"       {r['status']} {r['check']}: {r['detail'][:140]}")
    counts = report["counts"]
    print(f"\nFAIL {counts['FAIL']} / WARN {counts['WARN']} / PASS {counts['PASS']}")
    print(f"审片页: {out_dir / 'report.html'}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
