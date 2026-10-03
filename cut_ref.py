#!/usr/bin/env python3
"""
cut_ref.py —— 从原片切参照片段，供 Seedance `@视频1` 运镜/节奏/风格参照使用。

两种用法：

    # 直接指定源视频 + 起止秒数
    python3 cut_ref.py <源视频路径> <start_sec> <end_sec> [--out out/refs/]

    # 从 remix_brief.py 产出的 brief JSON 读 ref_video_cut 字段（source_file 在
    # _batches/<批次名>/ 下按软链接找，找不到直接报错，不静默跳过）
    python3 cut_ref.py --brief out/<批次>/briefs/<x>.json [--out out/refs/]

用 ffmpeg 切片（`config.FFMPEG`），优先 `-c copy` 无损快切；若关键帧对不上导致
片头/片尾偏差明显，可加 `--reencode` 强制重新编码保证起止点精确（会更慢）。

切完用 ffprobe 校验 Ark 约束：单段时长 2-15s、文件 < 50MB、格式 mp4——任何一项
不满足直接报错并保留产物供人工检查（不删除、不静默放行）。

产物命名：`<源文件 stem>_<start>-<end>s.mp4`，落在 `--out`（默认 `out/refs/`）。

打印提示：这个本地 mp4 还不能直接传给 seedance_gen.py 的 --ref-video（Ark 只吃
公网 URL，不吃本地路径/base64，见 seedance_gen.py 的 build_reference_content()），
需要先手动上传到可公网访问的存储，再把 URL 传给 --ref-video。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import config

DEFAULT_OUT_DIR = Path("out/refs")

ARK_VIDEO_MIN_SEC = 2.0
ARK_VIDEO_MAX_SEC = 15.0
ARK_VIDEO_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


class CutRefError(Exception):
    """参数不合法 / 源文件缺失 / ffmpeg 失败 / 切出的片段不满足 Ark 约束时抛出。"""


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe_duration_sec(path: Path) -> float:
    cp = run([
        config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    if cp.returncode != 0:
        raise CutRefError(f"ffprobe 探测时长失败: {path}\n{cp.stderr}")
    out = cp.stdout.strip()
    if not out:
        raise CutRefError(f"ffprobe 未返回时长: {path}")
    return float(out)


def resolve_source_from_brief(brief_path: Path) -> tuple[Path, float, float]:
    """从 brief JSON 的 ref_video_cut 字段解析出 (源文件路径, start_sec, end_sec)。

    源文件按 <brief所在批次目录>/../_batches/<批次名>/<source_file> 的软链接查找
    ——remix_brief.py 的 briefs/ 目录固定在 out/<批次名>/briefs/，源软链接固定在
    仓库根目录 _batches/<批次名>/ 下（analyze.py 建软链接的约定），两者共享
    <批次名> 这一级目录名。
    """
    if not brief_path.exists():
        raise CutRefError(f"brief 文件不存在: {brief_path}")
    try:
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise CutRefError(f"brief JSON 解析失败: {brief_path}: {e}")

    ref_cut = brief.get("ref_video_cut")
    if not ref_cut:
        raise CutRefError(
            f"brief 中没有 ref_video_cut 字段（{brief_path}）——只有 has_real_face=false "
            f"且成功选出参照片段的 brief 才有这个字段，has_real_face=true 的素材本来就不该切参照。"
        )
    for key in ("source_file", "start_sec", "end_sec"):
        if key not in ref_cut:
            raise CutRefError(f"brief 的 ref_video_cut 缺少字段 {key!r}: {ref_cut}")

    source_file = ref_cut["source_file"]
    start_sec = float(ref_cut["start_sec"])
    end_sec = float(ref_cut["end_sec"])

    # briefs/<stem>.json 所在目录是 out/<批次名>/briefs，批次名是其上两级目录名。
    batch_name = brief_path.resolve().parent.parent.name
    repo_root = Path(__file__).parent
    source_path = repo_root / "_batches" / batch_name / source_file
    if not source_path.exists():
        raise CutRefError(
            f"在 _batches/{batch_name}/ 下找不到源文件 {source_file!r}（{source_path}）。"
            f"确认 analyze.py 建的软链接批次目录名和 brief 所在的 out/<批次名>/ 是否一致。"
        )
    return source_path, start_sec, end_sec


def cut_segment(source: Path, start_sec: float, end_sec: float, out_dir: Path,
                 reencode: bool) -> Path:
    if end_sec <= start_sec:
        raise CutRefError(f"end_sec({end_sec}) 必须大于 start_sec({start_sec})")

    duration = end_sec - start_sec
    if not (ARK_VIDEO_MIN_SEC <= duration <= ARK_VIDEO_MAX_SEC):
        raise CutRefError(
            f"切片时长 {duration:.2f}s 不满足 Ark 约束（单段 {ARK_VIDEO_MIN_SEC:g}-"
            f"{ARK_VIDEO_MAX_SEC:g}s），拒绝切片"
        )

    source_duration = ffprobe_duration_sec(source)
    if end_sec > source_duration + 0.05:
        raise CutRefError(
            f"end_sec={end_sec} 超出源文件总时长 {source_duration:.2f}s，拒绝切片"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = lambda s: f"{s:g}"
    dest = out_dir / f"{source.stem}_{fmt(start_sec)}-{fmt(end_sec)}s.mp4"

    if reencode:
        cmd = [
            config.FFMPEG, "-y", "-ss", str(start_sec), "-to", str(end_sec),
            "-i", str(source), "-c:v", "libx264", "-c:a", "aac", str(dest),
        ]
    else:
        # 无损快切：-ss 放在 -i 之前做输入端 seek（快，可能不精确到帧），
        # -c copy 不重新编码。若起止点偏差过大，用 --reencode 换精确切点。
        cmd = [
            config.FFMPEG, "-y", "-ss", str(start_sec), "-i", str(source),
            "-to", str(duration), "-c", "copy", str(dest),
        ]

    cp = run(cmd)
    if cp.returncode != 0 or not dest.exists():
        raise CutRefError(f"ffmpeg 切片失败: {' '.join(cmd)}\n{cp.stderr}")

    return dest


def validate_ark_constraints(path: Path) -> None:
    problems = []

    duration = ffprobe_duration_sec(path)
    if not (ARK_VIDEO_MIN_SEC <= duration <= ARK_VIDEO_MAX_SEC):
        problems.append(
            f"时长 {duration:.2f}s 不在 Ark 约束的 {ARK_VIDEO_MIN_SEC:g}-{ARK_VIDEO_MAX_SEC:g}s 区间内"
        )

    size_bytes = path.stat().st_size
    if size_bytes >= ARK_VIDEO_MAX_BYTES:
        problems.append(
            f"文件大小 {size_bytes / 1024 / 1024:.2f}MB 超过 Ark 约束的 "
            f"{ARK_VIDEO_MAX_BYTES / 1024 / 1024:.0f}MB 上限"
        )

    if path.suffix.lower() != ".mp4":
        problems.append(f"格式 {path.suffix} 不是 Ark 要求的 mp4")

    if problems:
        raise CutRefError(
            f"切出的片段 {path} 不满足 Ark 约束（产物已保留，供人工检查）:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    print(f"  Ark 约束校验通过: 时长={duration:.2f}s, 大小={size_bytes / 1024 / 1024:.2f}MB, 格式=mp4")


def main() -> None:
    ap = argparse.ArgumentParser(description="从原片切 Seedance @视频 参照片段")
    ap.add_argument("source", nargs="?", type=Path, help="源视频路径（不与 --brief 同时使用）")
    ap.add_argument("start_sec", nargs="?", type=float, help="起始秒数")
    ap.add_argument("end_sec", nargs="?", type=float, help="结束秒数")
    ap.add_argument("--brief", type=Path, default=None,
                     help="从 remix_brief.py 产出的 brief JSON 读 ref_video_cut 字段"
                          "（源文件在 _batches/<批次名>/ 下按软链接查找）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="输出目录（默认 out/refs/）")
    ap.add_argument("--reencode", action="store_true",
                     help="重新编码切片保证起止点精确（默认 -c copy 无损快切，可能有关键帧误差）")
    args = ap.parse_args()

    # 实际要用 ffmpeg/ffprobe 之前显式严格校验（import config 本身不再退出）。
    config.validate_strict()

    try:
        if args.brief:
            if args.source or args.start_sec is not None or args.end_sec is not None:
                raise CutRefError("--brief 与位置参数 source/start_sec/end_sec 不能同时使用")
            source, start_sec, end_sec = resolve_source_from_brief(args.brief)
        else:
            if args.source is None or args.start_sec is None or args.end_sec is None:
                raise CutRefError("需要 <源视频路径> <start_sec> <end_sec>，或用 --brief <brief.json>")
            source = args.source
            if not source.exists():
                raise CutRefError(f"源视频不存在: {source}")
            start_sec, end_sec = args.start_sec, args.end_sec

        print(f"切片: {source.name} [{start_sec}s - {end_sec}s] -> {args.out}/")
        dest = cut_segment(source, start_sec, end_sec, args.out, args.reencode)
        validate_ark_constraints(dest)

        print(f"\n产物: {dest}")
        print("需上传公网 URL 后经 --ref-video 传入（Ark 只吃公网可访问 URL，"
              "本地路径/base64 均不支持，见 seedance_gen.py build_reference_content()）。")

    except CutRefError as e:
        sys.exit(f"拒绝执行: {e}")


if __name__ == "__main__":
    main()
