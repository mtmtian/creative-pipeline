#!/usr/bin/env python3
"""
assemble.py —— 分段路由成片拼装器：把 remix_brief.py 产出的
`differentiated_storyboard`（每段标 source: reuse|generated）拼成一条成片。

用法：

    python3 assemble.py --brief <brief.json> --source <原片路径> \
        --gen-clip <时段>=<mp4路径> [--gen-clip ...] \
        [--endcard <mp4/png路径>] [--out out/assembled/]

按 storyboard 顺序处理每一段：

  - source=="reuse"：用 `reuse_cut.start_sec/end_sec`，从 `--source` 原片
    ffmpeg 精确切（精确到 0.1s，重新编码而非 `-c copy`，避免关键帧偏移导致
    切点漂移）。
  - source=="generated"：从 `--gen-clip <时段>=<mp4路径>` 里按 `time_range`
    （0.5s 容差，容差规则同 seedance_gen.py `--shot` 裁剪）找对应素材；找不到
    直接报错，列出缺哪些段（不静默跳过、不拿别的段顶替）。

每段先各自转码统一到 1080×1920@30fps（竖版，`scale+pad` 保守方案：等比缩放后
居中加黑边，不做裁切）+ 音频统一 aac 48kHz；非首/尾段在片头/片尾各加 30ms 音频
淡入/淡出（防音爆纪律，同 video-use skill 的规范）。统一转码后用 ffmpeg
`concat` filter（不是 concat demuxer）拼接成最终产物。

`--endcard` 可选：mp4 直接 append；png 先转成 2s 静帧视频再 append。不传时
产物是无尾帧版本，明确打印提示，不假装有尾帧。

拼装完成后自动跑 `qc.py` 对产物做品牌预检，把结论打印到 stdout（不因 qc FAIL
而让 assemble.py 本身非零退出——qc 结果是给人看的信号，不是本脚本的成败判据）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import config

DEFAULT_OUT_DIR = Path("out/assembled")

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_FPS = 30
TARGET_AUDIO_RATE = 48000
AUDIO_FADE_SEC = 0.03  # 30ms

TIME_RANGE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—~]\s*(\d+(?:\.\d+)?)\s*(?:s|秒)?")
MATCH_TOLERANCE_SEC = 0.5

SCALE_PAD_FILTER = (
    f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
    f"setsar=1,fps={TARGET_FPS}"
)


def set_target_canvas(width: int, height: int) -> None:
    """覆盖模块级 TARGET_WIDTH/TARGET_HEIGHT/SCALE_PAD_FILTER。

    供 worker.py 按 brief.ratio 动态设置画布尺寸（见 worker.py RATIO_CANVAS）；不调用时
    保持模块默认 1080x1920，assemble.py 自身的 CLI 入口（main()）不受影响。"""
    global TARGET_WIDTH, TARGET_HEIGHT, SCALE_PAD_FILTER
    TARGET_WIDTH = width
    TARGET_HEIGHT = height
    SCALE_PAD_FILTER = (
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={TARGET_FPS}"
    )


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def parse_time_range(text: str) -> tuple[float, float] | None:
    m = TIME_RANGE_PATTERN.search(text)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def ffprobe_has_audio(path: Path) -> bool:
    cp = run([
        config.FFPROBE, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
    ])
    return cp.returncode == 0 and cp.stdout.strip() != ""


def ffprobe_summary(path: Path) -> str:
    cp = run([
        config.FFPROBE, "-v", "error",
        "-show_entries", "format=duration,size",
        "-show_entries", "stream=width,height,avg_frame_rate,codec_name,codec_type",
        "-of", "default=noprint_wrappers=1", str(path),
    ])
    return cp.stdout.strip() if cp.returncode == 0 else f"(ffprobe 失败: {cp.stderr.strip()})"


# ---------------------------------------------------------------------------
# 输入解析：storyboard 段 -> (source, cut_range/gen_path)
# ---------------------------------------------------------------------------


def load_gen_clip_map(gen_clip_args: list[str]) -> dict[tuple[float, float], Path]:
    mapping = {}
    for raw in gen_clip_args:
        if "=" not in raw:
            sys.exit(f"--gen-clip 格式错误（应为 <时段>=<mp4路径>）: {raw!r}")
        time_part, path_part = raw.split("=", 1)
        parsed = parse_time_range(time_part)
        if parsed is None:
            sys.exit(f"--gen-clip 时段解析失败: {time_part!r}（来自 {raw!r}）")
        path = Path(path_part).expanduser()
        if not path.is_file():
            sys.exit(f"--gen-clip 指定的文件不存在: {path}")
        mapping[parsed] = path
    return mapping


def find_gen_clip(gen_clip_map: dict[tuple[float, float], Path], time_range_text: str) -> Path | None:
    target = parse_time_range(time_range_text)
    if target is None:
        return None
    for (start, end), path in gen_clip_map.items():
        if abs(start - target[0]) <= MATCH_TOLERANCE_SEC and abs(end - target[1]) <= MATCH_TOLERANCE_SEC:
            return path
    return None


def resolve_segments(storyboard: list[dict], source_path: Path | None,
                      gen_clip_map: dict[tuple[float, float], Path]) -> tuple[list[dict], list[str]]:
    """返回 (resolved segments, 报错列表)。报错非空时调用方应中止，不做任何 ffmpeg 调用。"""
    resolved = []
    problems = []
    for seg in storyboard:
        time_range = seg.get("time_range", "?")
        source = seg.get("source", "generated")  # 兼容旧 brief：没有 source 字段时按 generated 处理
        if source == "reuse":
            reuse_cut = seg.get("reuse_cut")
            if not reuse_cut or "start_sec" not in reuse_cut or "end_sec" not in reuse_cut:
                problems.append(f"reuse 段 {time_range} 缺少 reuse_cut 字段")
                continue
            if source_path is None:
                problems.append(f"reuse 段 {time_range} 需要 --source 原片路径，但未提供")
                continue
            if not source_path.is_file():
                problems.append(f"reuse 段 {time_range} 的 --source 原片不存在: {source_path}")
                continue
            resolved.append({
                "time_range": time_range,
                "source": "reuse",
                "start_sec": float(reuse_cut["start_sec"]),
                "end_sec": float(reuse_cut["end_sec"]),
                "source_path": source_path,
            })
        elif source == "generated":
            gen_path = find_gen_clip(gen_clip_map, time_range)
            if gen_path is None:
                problems.append(f"generated 段 {time_range} 缺少对应的 --gen-clip 素材")
                continue
            resolved.append({
                "time_range": time_range,
                "source": "generated",
                "gen_path": gen_path,
            })
        else:
            problems.append(f"段 {time_range} 的 source 字段不是 reuse/generated: {source!r}")
    return resolved, problems


# ---------------------------------------------------------------------------
# 转码 / 拼接
# ---------------------------------------------------------------------------


def normalize_one(idx: int, seg: dict, scratch: Path, fade_in: bool, fade_out: bool,
                   fade_duration_hint: float | None) -> Path:
    """把一个 segment 转码成 1080x1920@30fps + aac48k 的临时文件，按位置加 30ms 音频淡入淡出。"""
    out_path = scratch / f"seg_{idx:02d}.mp4"

    input_args: list[str]
    if seg["source"] == "reuse":
        start, end = seg["start_sec"], seg["end_sec"]
        dur = round(end - start, 1)
        input_args = ["-ss", f"{start:.1f}", "-i", str(seg["source_path"]), "-t", f"{dur:.1f}"]
        duration_for_fade = dur
    else:
        input_args = ["-i", str(seg["gen_path"])]
        duration_for_fade = fade_duration_hint

    has_audio = seg["source"] == "reuse" or ffprobe_has_audio(
        seg["source_path"] if seg["source"] == "reuse" else seg["gen_path"]
    )

    audio_filters = []
    if fade_in:
        audio_filters.append(f"afade=t=in:st=0:d={AUDIO_FADE_SEC}")
    if fade_out and duration_for_fade is not None:
        fade_start = max(0.0, duration_for_fade - AUDIO_FADE_SEC)
        audio_filters.append(f"afade=t=out:st={fade_start:.3f}:d={AUDIO_FADE_SEC}")
    audio_filter_str = ",".join(audio_filters) if audio_filters else "anull"

    cmd = [config.FFMPEG, "-y", *input_args]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo"]
        # 把生成的静音源接到主输入之后，映射时用第二路输入做音轨
        cmd += [
            "-vf", SCALE_PAD_FILTER,
            "-af", audio_filter_str,
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
        ]
    else:
        cmd += [
            "-vf", SCALE_PAD_FILTER,
            "-af", audio_filter_str,
            "-map", "0:v:0", "-map", "0:a:0",
        ]
    cmd += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(TARGET_FPS),
        "-c:a", "aac", "-ar", str(TARGET_AUDIO_RATE), "-ac", "2",
        str(out_path),
    ]

    cp = run(cmd)
    if cp.returncode != 0 or not out_path.exists():
        sys.exit(f"转码失败（段 {seg['time_range']}）: {cp.stderr[-2000:]}")
    return out_path


def prep_endcard(endcard_path: Path, scratch: Path) -> Path:
    out_path = scratch / "endcard.mp4"
    if endcard_path.suffix.lower() in (".png", ".jpg", ".jpeg"):
        cmd = [
            config.FFMPEG, "-y", "-loop", "1", "-i", str(endcard_path),
            "-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo",
            "-t", "2",
            "-vf", SCALE_PAD_FILTER,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(TARGET_FPS),
            "-c:a", "aac", "-ar", str(TARGET_AUDIO_RATE), "-ac", "2",
            "-shortest", str(out_path),
        ]
    else:
        has_audio = ffprobe_has_audio(endcard_path)
        cmd = [config.FFMPEG, "-y", "-i", str(endcard_path)]
        if not has_audio:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r={TARGET_AUDIO_RATE}:cl=stereo"]
            cmd += ["-vf", SCALE_PAD_FILTER, "-af", f"afade=t=in:st=0:d={AUDIO_FADE_SEC}",
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        else:
            cmd += ["-vf", SCALE_PAD_FILTER, "-af", f"afade=t=in:st=0:d={AUDIO_FADE_SEC}",
                    "-map", "0:v:0", "-map", "0:a:0"]
        cmd += [
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(TARGET_FPS),
            "-c:a", "aac", "-ar", str(TARGET_AUDIO_RATE), "-ac", "2",
            str(out_path),
        ]
    cp = run(cmd)
    if cp.returncode != 0 or not out_path.exists():
        sys.exit(f"尾帧转码失败: {cp.stderr[-2000:]}")
    return out_path


def concat_filter(clips: list[Path], out_path: Path) -> None:
    n = len(clips)
    cmd = [config.FFMPEG, "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    filter_parts = []
    concat_inputs = []
    for i in range(n):
        filter_parts.append(f"[{i}:v:0]")
        filter_parts.append(f"[{i}:a:0]")
        concat_inputs.append(f"[{i}:v:0][{i}:a:0]")
    filter_complex = "".join(concat_inputs) + f"concat=n={n}:v=1:a=1[outv][outa]"
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(TARGET_FPS),
        "-c:a", "aac", "-ar", str(TARGET_AUDIO_RATE),
        str(out_path),
    ]
    cp = run(cmd)
    if cp.returncode != 0 or not out_path.exists():
        sys.exit(f"concat 拼接失败: {cp.stderr[-2000:]}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="分段路由成片拼装器（reuse 段切原片 + generated 段接生成产物）")
    ap.add_argument("--brief", type=Path, required=True, help="remix_brief.py 产出的 brief.json")
    ap.add_argument("--source", type=Path, default=None, help="原片路径（reuse 段切片用）")
    ap.add_argument("--gen-clip", action="append", default=[], metavar="<时段>=<mp4路径>",
                     help="generated 段对应的生成产物，可重复传多个")
    ap.add_argument("--endcard", type=Path, default=None, help="尾帧插槽（mp4 直接 append，png 转 2s 静帧）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="产物输出目录")
    args = ap.parse_args()

    # 实际要用 ffmpeg/ffprobe 之前显式严格校验（import config 本身不再退出）。
    config.validate_strict()

    if not args.brief.is_file():
        sys.exit(f"brief 文件不存在: {args.brief}")
    brief = json.loads(args.brief.read_text(encoding="utf-8"))
    storyboard = brief.get("differentiated_storyboard")
    if not storyboard:
        sys.exit(f"brief 缺少 differentiated_storyboard: {args.brief}")

    source_path = args.source.resolve() if args.source else None
    gen_clip_map = load_gen_clip_map(args.gen_clip)

    resolved, problems = resolve_segments(storyboard, source_path, gen_clip_map)
    if problems:
        sys.stderr.write("拼装前置检查未通过，缺少以下段落素材（未执行任何 ffmpeg 调用）：\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        sys.exit(1)

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = out_dir / ".scratch"
    scratch.mkdir(exist_ok=True)

    print(f"共 {len(resolved)} 段（brief: {args.brief.name}）：")
    for seg in resolved:
        if seg["source"] == "reuse":
            print(f"  [reuse]     {seg['time_range']}  <- {seg['source_path'].name} "
                  f"[{seg['start_sec']:.1f}s-{seg['end_sec']:.1f}s]")
        else:
            print(f"  [generated] {seg['time_range']}  <- {seg['gen_path'].name}")

    n = len(resolved)
    normalized = []
    for i, seg in enumerate(resolved):
        fade_in = i > 0
        fade_out = i < n - 1
        dur_hint = None
        if seg["source"] == "generated":
            dur_hint = float(ffprobe_json_duration(seg["gen_path"]))
        clip_path = normalize_one(i, seg, scratch, fade_in, fade_out, dur_hint)
        normalized.append(clip_path)
        print(f"  转码完成: seg_{i:02d}.mp4 ({seg['time_range']})")

    endcard_present = args.endcard is not None
    if endcard_present:
        if not args.endcard.is_file():
            sys.exit(f"--endcard 指定的文件不存在: {args.endcard}")
        endcard_clip = prep_endcard(args.endcard.resolve(), scratch)
        normalized.append(endcard_clip)
        print(f"  尾帧转码完成: {endcard_clip.name}")
    else:
        print("缺尾帧，产物为无尾帧版本")

    stem = Path(args.brief).stem
    out_path = out_dir / f"{stem}_assembled.mp4"
    concat_filter(normalized, out_path)

    print()
    print(f"拼装完成 -> {out_path}")
    print("ffprobe:")
    print(ffprobe_summary(out_path))

    print()
    print("跑 qc.py 品牌预检...")
    qc_cp = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "qc.py"), str(out_path),
         "--out", str(out_dir / "qc")],
        capture_output=True, text=True,
    )
    print(qc_cp.stdout)
    if qc_cp.returncode != 0:
        print(f"（qc.py 退出码 {qc_cp.returncode}，见上方 FAIL 明细；assemble.py 本身不因此非零退出）")
    if qc_cp.stderr.strip():
        print("qc.py stderr:", qc_cp.stderr.strip())


def ffprobe_json_duration(path: Path) -> str:
    cp = run([
        config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return cp.stdout.strip() or "0"


if __name__ == "__main__":
    main()
