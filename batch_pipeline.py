#!/usr/bin/env python3
"""本地素材生产批次：初始化、分析、字幕、尾帧、审核清单、规格 QC、人工审核、交付 manifest。

不下载素材，不调用远端生成 API。

批次目录（PC 端 2026-07-31 约定，移动端沿用）::

    <batch>/
      batch.json          profile、产品快照、可选 locale_policy
      00_input/           源素材或软链接（源片也可以留在 KOL-KOC 源视频目录，由 20_plan 记录）
      20_plan/            方案、EDL、creatives.json（每个 hook 的投放标注）、审核记录
      30_source_hooks/    API 原始 Hook 与生成记录（有则保留）
      50_delivery/<16x9|1x1|9x16|4x5>/<US|JP|ES…>/<素材ID>.mp4

抽帧、转写、尾帧、QC 快照等过程文件写到仓库 out/batches/<batch_id>/，不放回批次目录。

素材 ID：{产品}_{批次}_{地区}_{画幅}_{hook}_v{N}，例如
hakkopc_20260727-ai-girlfriend-hooks_us_16x9_anytime_v1。字段用 "_" 分隔、字段内用 "-"，
它同时是成片文件名、YouTube 标题和 TikTok 素材名，投放回收数据靠它对回批次。

审核门：review 扫描 50_delivery 写 20_plan/review_manifest.json → qc 写 20_plan/qc_report.json（规格）→
preflight 写 20_plan/preflight_report.json（可用性预检，见 preflight.py）→ 人看审片页后 approve 写
20_plan/approved.json → deliver 校验四者都绑定当前 review manifest、规格与预检无 FAIL、文件未变，
写出 50_delivery/manifest.json，它是交给投放的唯一交接文件。全程不复制、不删除成片。
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path, PurePosixPath

import config

REPO_DIR = Path(__file__).resolve().parent
WORK_ROOT = REPO_DIR / "out" / "batches"
INIT_DIRS = ("00_input", "20_plan", "50_delivery")
REQUIRED_DIRS = ("20_plan", "50_delivery")
FORMAT_SPECS = {
    "16:9": (1920, 1080, "16x9"),
    "1:1": (1080, 1080, "1x1"),
    "9:16": (1080, 1920, "9x16"),
    "4:5": (1080, 1350, "4x5"),
}
FORMAT_BY_DIR = {spec[2]: name for name, spec in FORMAT_SPECS.items()}
AUDIO_STRATEGIES = {"source", "seedance", "voiceover", "silent"}

REVIEW_MANIFEST = "20_plan/review_manifest.json"
QC_REPORT = "20_plan/qc_report.json"
APPROVAL = "20_plan/approved.json"
CREATIVE_TAGS = "20_plan/creatives.json"
PREFLIGHT_REPORT = "20_plan/preflight_report.json"
HANDOFF = "50_delivery/manifest.json"

BATCH_ID_RE = re.compile(r"^\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*$")
LOCALE_RE = re.compile(r"^[A-Z]{2}$")
HOOK_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CREATIVE_ID_RE = re.compile(
    r"^(?P<product>[a-z]+)_(?P<batch>\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*)_(?P<locale>[a-z]{2})_"
    r"(?P<ratio>\d+x\d+)_(?P<hook>[a-z0-9]+(?:-[a-z0-9]+)*)_v(?P<version>[1-9]\d*)$"
)
MAX_CREATIVE_ID = 100  # YouTube 标题上限


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- profiles / IDs

def batch_profile(name: str) -> dict:
    if name not in config.batch_profiles():
        raise ValueError(f"profile {name!r} 不能建生产批次；可选: {', '.join(config.batch_profiles())}")
    return config.get_product_profile(name)


def creative_id(profile_name: str, batch_id: str, locale: str, format_name: str, hook: str,
                version: int) -> str:
    if format_name not in FORMAT_SPECS:
        raise ValueError(f"不支持的 format: {format_name}")
    if not LOCALE_RE.fullmatch(locale):
        raise ValueError(f"locale 必须是两位大写地区码，如 US/JP/ES: {locale!r}")
    if not HOOK_RE.fullmatch(hook):
        raise ValueError(f"hook 只能用小写字母、数字和单个连字符，如 ai-girlfriend: {hook!r}")
    if not BATCH_ID_RE.fullmatch(batch_id):
        raise ValueError(f"batch id 必须形如 20260727-ai-girlfriend-hooks 才能进素材 ID: {batch_id!r}")
    if type(version) is not int or version < 1:
        raise ValueError("version 必须是正整数")
    value = "_".join([batch_profile(profile_name)["code"], batch_id, locale.lower(),
                      FORMAT_SPECS[format_name][2], hook, f"v{version}"])
    if len(value) > MAX_CREATIVE_ID:
        raise ValueError(f"素材 ID 超过 {MAX_CREATIVE_ID} 字符，请缩短 hook 或批次名: {value}")
    return value


def parse_creative_id(value: str) -> dict:
    match = CREATIVE_ID_RE.fullmatch(value)
    if not match or len(value) > MAX_CREATIVE_ID:
        raise ValueError(f"不是合法素材 ID: {value!r}")
    parts = match.groupdict()
    if parts["ratio"] not in FORMAT_BY_DIR:
        raise ValueError(f"素材 ID 画幅不受支持: {value!r}")
    return {**parts, "format": FORMAT_BY_DIR[parts["ratio"]], "version": int(parts["version"])}


# --------------------------------------------------------------------------- safe paths and control files

def _lexical_absolute(path: Path) -> Path:
    """Return an absolute path without resolving symlinks."""
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_range(path: Path, boundary: Path, label: str) -> None:
    path = _lexical_absolute(path)
    boundary = _lexical_absolute(boundary)
    try:
        relative = path.relative_to(boundary)
    except ValueError as e:
        raise ValueError(f"{label} is outside its safe root") from e
    current = boundary
    for part in (Path(), *relative.parts):
        if part != Path():
            current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"{label} must not contain a symlink: {current}")


def _validate_batch_layout(batch_dir: Path, required: tuple[str, ...]) -> Path:
    batch_dir = _lexical_absolute(batch_dir)
    # The caller-supplied batch root (the batch parent) and batch itself are trust boundaries.
    _reject_symlink_range(batch_dir, batch_dir.parent, "batch path")
    if batch_dir.exists() and not batch_dir.is_dir():
        raise ValueError("batch path must be a directory")
    for relative in INIT_DIRS:
        path = batch_dir / relative
        _reject_symlink_range(path, batch_dir, f"batch directory {relative}")
        if path.exists() and not path.is_dir():
            raise ValueError(f"batch directory must be a directory: {relative}")
    for relative in required:
        if not (batch_dir / relative).is_dir():
            raise ValueError(f"missing batch directory: {relative}")
    return batch_dir


def _control_path_is_safe(path: Path, label: str) -> None:
    path = _lexical_absolute(path)
    _reject_symlink_range(path, path.parent, label)
    if path.parent.is_symlink():
        raise ValueError(f"{label} parent must not be a symlink: {path.parent}")
    if not path.parent.is_dir():
        raise ValueError(f"{label} parent must be a directory: {path.parent}")


def _open_control_parent(path: Path, label: str) -> int:
    _control_path_is_safe(path, label)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.parent, flags)
    except OSError as e:
        raise ValueError(f"{label} parent must be a safe directory: {path.parent}") from e
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"{label} parent must be a directory: {path.parent}")
    return descriptor


def _read_control_json(path: Path, label: str) -> tuple[dict, bytes, str]:
    path = _lexical_absolute(path)
    parent_descriptor = _open_control_parent(path, label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            raw = source.read()
    except OSError as e:
        if e.errno in {errno.ELOOP, errno.EMLINK} or path.is_symlink():
            raise ValueError(f"{label} must not be a symlink: {path}") from e
        raise ValueError(f"{label} is not valid JSON: {path}") from e
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{label} is not valid JSON: {path}") from e
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return value, raw, hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, data: dict) -> None:
    path = _lexical_absolute(path)
    parent_descriptor = _open_control_parent(path, path.name)
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    temporary_exists = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
        temporary_exists = True
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            target_mode = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False).st_mode
        except FileNotFoundError:
            target_mode = None
        if target_mode is not None and stat.S_ISLNK(target_mode):
            raise ValueError(f"{path.name} must not be a symlink: {path}")
        os.replace(
            temporary_name, path.name,
            src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
        )
        temporary_exists = False
        os.fsync(parent_descriptor)
    finally:
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def _unlink_control(path: Path) -> None:
    path = _lexical_absolute(path)
    parent_descriptor = _open_control_parent(path, path.name)
    try:
        try:
            target_mode = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(target_mode):
            raise ValueError(f"{path.name} must not be a symlink: {path}")
        os.unlink(path.name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _require_exact_dict(value: object, keys: set[str], label: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    if set(value) != keys:
        raise ValueError(f"{label} has invalid fields")
    return value


def _is_timestamp(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (type(value) is str and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _reject_symlink_components(path: Path, batch_dir: Path, label: str) -> None:
    current = path
    while current != batch_dir.parent:
        if current.is_symlink():
            raise ValueError(f"{label} must not contain a symlink: {current}")
        if current == batch_dir:
            return
        current = current.parent
    raise ValueError(f"{label} is outside batch")


def _copy_and_hash(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source, os.O_RDONLY | nofollow)
    destination_descriptor = -1
    try:
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            raise ValueError(f"candidate is not a regular file: {source}")
        destination_descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o600
        )
        if not stat.S_ISREG(os.fstat(destination_descriptor).st_mode):
            raise ValueError(f"snapshot target is not a regular file: {destination}")
        with (
            os.fdopen(source_descriptor, "rb") as input_file,
            os.fdopen(destination_descriptor, "wb") as output_file,
        ):
            source_descriptor = -1
            destination_descriptor = -1
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
                output_file.write(chunk)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    return digest.hexdigest()


def _work_dir(batch_dir: Path, *parts: str) -> Path:
    path = WORK_ROOT.joinpath(batch_dir.name, *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- batch metadata

def _validate_batch_metadata(batch_dir: Path, metadata: object, profile_name: str | None = None) -> dict:
    if type(metadata) is not dict:
        raise ValueError("batch.json must be an object")
    required = {"schema_version", "batch_id", "profile", "created_at"}
    optional = {"product", "locale_policy"}
    if not required <= set(metadata) or set(metadata) - required - optional:
        raise ValueError("batch.json has invalid fields")
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        raise ValueError("batch.json schema_version must be integer 1")
    if type(metadata["batch_id"]) is not str or metadata["batch_id"] != batch_dir.name:
        raise ValueError("batch.json batch_id does not match batch directory")
    if type(metadata["profile"]) is not str:
        raise ValueError("batch.json profile is invalid")
    batch_profile(metadata["profile"])
    if profile_name is not None and metadata["profile"] != profile_name:
        raise ValueError("batch.json profile does not match requested profile")
    # product 是建批次时的快照，profile 之后演进不会让旧批次失效。
    if "product" in metadata and type(metadata["product"]) is not dict:
        raise ValueError("batch.json product must be an object")
    if "locale_policy" in metadata and type(metadata["locale_policy"]) is not dict:
        raise ValueError("batch.json locale_policy must be an object")
    if not _is_timestamp(metadata["created_at"]):
        raise ValueError("batch.json created_at must be an ISO-8601 UTC timestamp")
    return metadata


def _validate_batch(batch_dir: Path) -> dict:
    batch_dir = _validate_batch_layout(batch_dir, REQUIRED_DIRS)
    metadata, _, _ = _read_control_json(batch_dir / "batch.json", "batch.json")
    return _validate_batch_metadata(batch_dir, metadata)


def initialize_batch(batch_dir: Path, profile_name: str) -> dict:
    profile = batch_profile(profile_name)
    batch_dir = _validate_batch_layout(batch_dir, ())
    if not BATCH_ID_RE.fullmatch(batch_dir.name):
        raise ValueError(f"batch id 必须形如 20261004-topic-name（日期-小写短横线）: {batch_dir.name!r}")
    metadata_path = batch_dir / "batch.json"
    if metadata_path.exists() or metadata_path.is_symlink():
        existing = _validate_batch_metadata(
            batch_dir, _read_control_json(metadata_path, "batch.json")[0], profile_name
        )
        for relative in INIT_DIRS:
            (batch_dir / relative).mkdir(parents=True, exist_ok=True)
        _validate_batch_layout(batch_dir, INIT_DIRS)
        return existing
    for relative in INIT_DIRS:
        (batch_dir / relative).mkdir(parents=True, exist_ok=True)
    _validate_batch_layout(batch_dir, INIT_DIRS)
    metadata = {
        "schema_version": 1,
        "batch_id": batch_dir.name,
        "profile": profile_name,
        "product": profile,
        "created_at": _utc_now(),
    }
    _write_json(metadata_path, metadata)
    return metadata


def run_local_analyze(batch_dir: Path, runner=None) -> Path:
    batch_dir = _lexical_absolute(batch_dir)
    _validate_batch(batch_dir)
    source = batch_dir / "00_input"
    if not source.is_dir():
        raise ValueError("analyze 需要批次的 00_input 目录")
    output = _work_dir(batch_dir, "analysis")
    command = [sys.executable, str(REPO_DIR / "analyze.py"), str(source.resolve()), "--out", str(output)]
    if runner is None:
        subprocess.run(command, check=True)
    else:
        runner(command)
    return output


# --------------------------------------------------------------------------- subtitles / endcard

def _escape_subtitles_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_normalize_command(input_video: Path, output_video: Path) -> list[str]:
    """只转编码、不改画面：H.264/yuv420p、恒定 30fps、SAR 1:1、AAC 48kHz 立体声。"""
    if _lexical_absolute(input_video) == _lexical_absolute(output_video):
        raise ValueError("normalize 的输出不能覆盖输入")
    return [
        config.FFMPEG, "-y", "-i", str(input_video), "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "setsar=1", "-fps_mode", "cfr", "-r", "30",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(output_video),
    ]


def build_burn_srt_command(input_video: Path, srt_path: Path, output_video: Path,
                           format_name: str, audio_strategy: str,
                           audio_path: Path | None) -> list[str]:
    if format_name not in FORMAT_SPECS:
        raise ValueError(f"不支持的 format: {format_name}")
    if audio_strategy not in AUDIO_STRATEGIES:
        raise ValueError(f"不支持的 audio strategy: {audio_strategy}")
    if audio_strategy == "voiceover" and audio_path is None:
        raise ValueError("voiceover 音频策略必须提供 --audio")

    width, height, _ = FORMAT_SPECS[format_name]
    # subtitles 必须是最后一个视频 filter；后续不得再接画面处理。
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps=30,subtitles=filename='{_escape_subtitles_path(srt_path)}'"
    )
    command = [config.FFMPEG, "-y", "-i", str(input_video)]
    if audio_strategy == "voiceover":
        command += ["-i", str(audio_path)]
        audio_input = "[1:a:0]"
    elif audio_strategy == "silent":
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-map", "0:v:0", "-map", "1:a:0"]
    else:  # source / seedance：音频已在输入视频内，显式要求该音轨存在。
        audio_input = "[0:a:0]"
    if audio_strategy != "silent":
        command += ["-filter_complex", f"{audio_input}apad[aout]",
                    "-map", "0:v:0", "-map", "[aout]"]
    command += [
        "-vf", video_filter,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
        str(output_video),
    ]
    return command


def burn_srt(input_video: Path, srt_path: Path, output_video: Path,
             format_name: str, audio_strategy: str = "source",
             audio_path: Path | None = None) -> None:
    command = build_burn_srt_command(
        input_video, srt_path, output_video, format_name, audio_strategy, audio_path
    )
    output_video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True)


def build_endcard_command(format_name: str, locale: str, output_video: Path,
                          profile_name: str = "hakko-pc") -> list[str]:
    if format_name not in FORMAT_SPECS:
        raise ValueError(f"不支持的 format: {format_name}")
    profile = batch_profile(profile_name)
    if "endcard_logo_path" not in profile:
        raise ValueError(f"profile {profile_name} 不生成尾帧；使用现成尾帧素材: {profile.get('endcard_assets', '')}")
    if locale not in profile.get("cta", {}):
        raise ValueError(f"profile {profile_name} 没有 {locale} 的 CTA 文案")
    width, height, _ = FORMAT_SPECS[format_name]
    logo_width = width // 2
    font = "Arial" if locale == "US" else "Hiragino Sans"
    cta = profile["cta"][locale]
    filters = (
        f"[1:v]scale={logo_width}:-1[logo];"
        "[0:v][logo]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2-80,"
        f"drawtext=font='{font}':text='{cta}':fontcolor=white:fontsize={height // 15}:"
        "x=(w-text_w)/2:y=h*0.72[outv]"
    )
    return [
        config.FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c=#101014:s={width}x{height}:d=2:r=30",
        "-loop", "1", "-i", profile["endcard_logo_path"],
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-filter_complex", filters,
        "-map", "[outv]", "-map", "2:a:0",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
        str(output_video),
    ]


def make_endcard(batch_dir: Path, format_name: str, locale: str, output: Path | None = None) -> Path:
    batch_dir = _lexical_absolute(batch_dir)
    metadata = _validate_batch(batch_dir)
    command_profile = metadata["profile"]
    profile = batch_profile(command_profile)
    if "endcard_logo_path" in profile and not Path(profile["endcard_logo_path"]).is_file():
        raise FileNotFoundError(profile["endcard_logo_path"])
    if format_name not in FORMAT_SPECS:
        raise ValueError(f"不支持的 format: {format_name}")
    _, _, format_dir = FORMAT_SPECS[format_name]
    output = output or _work_dir(batch_dir, "endcards") / f"endcard_{format_dir}_{locale}.mp4"
    command = build_endcard_command(format_name, locale, output, command_profile)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True)
    return output


# --------------------------------------------------------------------------- review manifest

def _delivery_path_from_value(batch_dir: Path, value: object) -> Path:
    """A manifest path must be 50_delivery/<format_dir>/<LOCALE>/<creative_id>.mp4 and a regular file."""
    if type(value) is not str or not value or "\\" in value:
        raise ValueError("candidate path must be a canonical relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or str(pure) != value:
        raise ValueError("candidate path must be a canonical relative path without '..'")
    if len(pure.parts) != 4 or pure.parts[0] != "50_delivery":
        raise ValueError("candidate path must be 50_delivery/<format>/<LOCALE>/<file>")
    lexical = batch_dir / Path(*pure.parts)
    _reject_symlink_components(lexical, batch_dir, "candidate path")
    if not lexical.is_file():
        raise ValueError(f"candidate path must name a regular file: {value}")
    return lexical


def _load_tags(batch_dir: Path) -> dict:
    path = batch_dir / CREATIVE_TAGS
    if not path.exists() and not path.is_symlink():
        return {}
    tags, _, _ = _read_control_json(path, "creatives.json")
    for hook, value in tags.items():
        if not HOOK_RE.fullmatch(hook) or type(value) is not dict:
            raise ValueError(f"creatives.json 的键必须是 hook、值必须是对象: {hook!r}")
        for key, item in value.items():
            ok = type(item) is str or (type(item) is list and all(type(x) is str for x in item))
            if type(key) is not str or not ok:
                raise ValueError(f"creatives.json[{hook!r}].{key} 只能是字符串或字符串数组")
    return tags


def scan_delivery(batch_dir: Path, profile_name: str) -> tuple[list[dict], list[str]]:
    """Every deliverable under 50_delivery as a review row; returns (rows, ignored top-level entries)."""
    code = batch_profile(profile_name)["code"]
    delivery = batch_dir / "50_delivery"
    _reject_symlink_components(delivery, batch_dir, "delivery path")
    rows, ignored, problems = [], [], []
    for entry in sorted(delivery.iterdir()):
        if entry.name.startswith(".") or entry.name == "manifest.json":
            continue
        if entry.name not in FORMAT_BY_DIR:
            ignored.append(entry.name)
            continue
        if entry.is_symlink() or not entry.is_dir():
            problems.append(f"{entry.name}: 画幅目录必须是普通目录")
            continue
        format_name = FORMAT_BY_DIR[entry.name]
        for locale_dir in sorted(entry.iterdir()):
            if locale_dir.name.startswith("."):
                continue
            if not locale_dir.is_dir() or locale_dir.is_symlink() or not LOCALE_RE.fullmatch(locale_dir.name):
                problems.append(f"{entry.name}/{locale_dir.name}: 地区目录必须是两位大写地区码")
                continue
            for item in sorted(locale_dir.iterdir()):
                if item.name.startswith("."):
                    continue
                relative = f"50_delivery/{entry.name}/{locale_dir.name}/{item.name}"
                if item.is_symlink() or not item.is_file():
                    problems.append(f"{relative}: 必须是普通文件")
                    continue
                if item.suffix != ".mp4":
                    problems.append(f"{relative}: 交付目录只放 .mp4 成片")
                    continue
                try:
                    parts = parse_creative_id(item.stem)
                except ValueError:
                    problems.append(f"{relative}: 文件名不是素材 ID（{{产品}}_{{批次}}_{{地区}}_{{画幅}}_{{hook}}_v{{N}}）")
                    continue
                expected = {"product": code, "batch": batch_dir.name,
                            "locale": locale_dir.name.lower(), "ratio": entry.name}
                mismatched = [k for k, v in expected.items() if parts[k] != v]
                if mismatched:
                    problems.append(f"{relative}: 素材 ID 的 {', '.join(mismatched)} 与所在批次/目录不一致")
                    continue
                rows.append({"creative_id": item.stem, "format": format_name, "locale": locale_dir.name,
                             "path": relative, "hook": parts["hook"]})
    if problems:
        raise ValueError("50_delivery 有不合规文件:\n  " + "\n  ".join(problems))
    return rows, ignored


def _validate_manifest(batch_dir: Path, manifest: object) -> dict:
    manifest = _require_exact_dict(
        manifest, {"schema_version", "status", "created_at", "batch_id", "candidates"},
        "review_manifest.json",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("review manifest schema_version must be integer 1")
    if manifest["status"] != "pending_human_review":
        raise ValueError("review manifest status is invalid")
    if manifest["batch_id"] != batch_dir.name:
        raise ValueError("review manifest batch_id does not match batch")
    if not _is_timestamp(manifest["created_at"]):
        raise ValueError("review manifest created_at is invalid")
    if type(manifest["candidates"]) is not list or not manifest["candidates"]:
        raise ValueError("review manifest candidates must be a non-empty array")
    ids = set()
    for row in manifest["candidates"]:
        row = _require_exact_dict(row, {"creative_id", "format", "locale", "path", "sha256", "tags"},
                                  "manifest row")
        parts = parse_creative_id(row["creative_id"]) if type(row["creative_id"]) is str else None
        if parts is None or parts["batch"] != batch_dir.name:
            raise ValueError("manifest row creative_id is invalid")
        if row["format"] != parts["format"] or row["locale"] != parts["locale"].upper():
            raise ValueError("manifest row format/locale does not match creative_id")
        expected_path = f"50_delivery/{parts['ratio']}/{row['locale']}/{row['creative_id']}.mp4"
        if row["path"] != expected_path:
            raise ValueError("manifest row path does not match creative_id")
        if not _is_sha256(row["sha256"]):
            raise ValueError("manifest row sha256 is invalid")
        if type(row["tags"]) is not dict:
            raise ValueError("manifest row tags must be an object")
        _delivery_path_from_value(batch_dir, row["path"])
        if row["creative_id"] in ids:
            raise ValueError("review manifest contains duplicate creative_id")
        ids.add(row["creative_id"])
    return manifest


def create_review_manifest(batch_dir: Path) -> dict:
    """Scan 50_delivery, hash each deliverable and reset approval, QC and handoff."""
    batch_dir = _lexical_absolute(batch_dir)
    metadata = _validate_batch(batch_dir)
    rows, ignored = scan_delivery(batch_dir, metadata["profile"])
    if not rows:
        raise ValueError("50_delivery 里没有成片；成片放在 50_delivery/<画幅>/<地区>/<素材ID>.mp4")
    tags = _load_tags(batch_dir)
    candidates = [{
        "creative_id": row["creative_id"],
        "format": row["format"],
        "locale": row["locale"],
        "path": row["path"],
        "sha256": _sha256_file(batch_dir / row["path"]),
        "tags": tags.get(row["hook"], {}),
    } for row in rows]
    manifest = {
        "schema_version": 1,
        "status": "pending_human_review",
        "created_at": _utc_now(),
        "batch_id": batch_dir.name,
        "candidates": candidates,
    }
    _validate_manifest(batch_dir, manifest)
    for relative in (REVIEW_MANIFEST, APPROVAL, QC_REPORT, PREFLIGHT_REPORT, HANDOFF):
        _control_path_is_safe(batch_dir / relative, Path(relative).name)
    _write_json(batch_dir / REVIEW_MANIFEST, manifest)
    for relative in (APPROVAL, QC_REPORT, PREFLIGHT_REPORT, HANDOFF):
        _unlink_control(batch_dir / relative)
    return {
        "manifest": manifest,
        "ignored": ignored,
        "untagged_hooks": sorted({row["hook"] for row in rows} - set(tags)),
    }


def _load_manifest(batch_dir: Path) -> tuple[dict, str]:
    batch_dir = _lexical_absolute(batch_dir)
    _validate_batch(batch_dir)
    manifest, _, manifest_sha = _read_control_json(batch_dir / REVIEW_MANIFEST, "review_manifest.json")
    return _validate_manifest(batch_dir, manifest), manifest_sha


# --------------------------------------------------------------------------- approval

def _validate_approval(approval: object) -> dict:
    approval = _require_exact_dict(
        approval, {"approved", "reviewer", "approved_at", "review_manifest_sha256"},
        "approved.json",
    )
    if approval["approved"] is not True:
        raise ValueError("approved.json approved must be true")
    if type(approval["reviewer"]) is not str or not approval["reviewer"].strip():
        raise ValueError("approved.json reviewer is invalid")
    if not _is_timestamp(approval["approved_at"]):
        raise ValueError("approved.json approved_at is invalid")
    if not _is_sha256(approval["review_manifest_sha256"]):
        raise ValueError("approved.json manifest hash is invalid")
    return approval


def approve_review(batch_dir: Path, reviewer: str) -> dict:
    if type(reviewer) is not str or not reviewer.strip():
        raise ValueError("reviewer 不能为空")
    batch_dir = _lexical_absolute(batch_dir)
    _, manifest_sha = _load_manifest(batch_dir)
    approval = {
        "approved": True,
        "reviewer": reviewer,
        "approved_at": _utc_now(),
        "review_manifest_sha256": manifest_sha,
    }
    _write_json(batch_dir / APPROVAL, approval)
    return approval


def is_approved(batch_dir: Path) -> bool:
    batch_dir = _lexical_absolute(batch_dir)
    if not (batch_dir / REVIEW_MANIFEST).is_file() or not (batch_dir / APPROVAL).is_file():
        return False
    try:
        _, manifest_sha = _load_manifest(batch_dir)
        approval = _validate_approval(_read_control_json(batch_dir / APPROVAL, "approved.json")[0])
    except (OSError, ValueError):
        return False
    return approval["review_manifest_sha256"] == manifest_sha


# --------------------------------------------------------------------------- spec QC

def check_media_spec(probe: dict, format_name: str) -> list[str]:
    if format_name not in FORMAT_SPECS:
        return [f"unsupported format: {format_name}"]
    streams = probe.get("streams", [])
    errors = []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(streams) != 2 or len(videos) != 1 or len(audios) != 1:
        errors.append("media must contain exactly 1 video and 1 audio stream and no other streams")
    video = videos[0] if videos else None
    audio = audios[0] if audios else None
    if video is None:
        errors.append("missing video track")
    else:
        min_width, min_height, _ = FORMAT_SPECS[format_name]
        width = int(video.get("width", 0))
        height = int(video.get("height", 0))
        if width < min_width or height < min_height:
            errors.append(
                f"resolution must be at least {min_width}x{min_height}; got {width}x{height}"
            )
        ratio_w, ratio_h = (int(v) for v in format_name.split(":"))
        if width * ratio_h != height * ratio_w:
            errors.append(f"aspect ratio must be {format_name}; got {width}x{height}")
        if video.get("codec_name") != "h264":
            errors.append(f"video codec must be h264; got {video.get('codec_name')}")
        if video.get("pix_fmt") != "yuv420p":
            errors.append(f"pixel format must be yuv420p; got {video.get('pix_fmt')}")
        if video.get("sample_aspect_ratio") != "1:1":
            errors.append(f"SAR must be 1:1; got {video.get('sample_aspect_ratio')}")
        rotations = []
        if type(video.get("tags")) is dict and "rotate" in video["tags"]:
            rotations.append(video["tags"]["rotate"])
        if type(video.get("side_data_list")) is list:
            rotations.extend(item.get("rotation") for item in video["side_data_list"]
                             if type(item) is dict and "rotation" in item)
        for rotation in rotations:
            try:
                nonzero = float(rotation) != 0
            except (TypeError, ValueError):
                nonzero = True
            if nonzero:
                errors.append(f"rotation must be 0; got {rotation}")
                break
        for field in ("avg_frame_rate", "r_frame_rate"):
            rate = video.get(field)
            try:
                valid_rate = Fraction(rate) == 30
            except (TypeError, ValueError, ZeroDivisionError):
                valid_rate = False
            if not valid_rate:
                errors.append(f"{field} fps must be 30; got {rate}")
    if audio is None:
        errors.append("missing audio track")
    else:
        if audio.get("codec_name") != "aac":
            errors.append(f"audio codec must be aac; got {audio.get('codec_name')}")
        if audio.get("sample_rate") != "48000":
            errors.append(f"audio sample rate must be 48000; got {audio.get('sample_rate')}")
        if audio.get("channels") != 2 or audio.get("channel_layout") != "stereo":
            errors.append(
                f"audio must be stereo; got channels={audio.get('channels')} "
                f"layout={audio.get('channel_layout')}"
            )
    return errors


def _probe_media(path: Path) -> dict:
    completed = subprocess.run([
        config.FFPROBE, "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ], capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def _media_summary(probe: dict) -> tuple[int | None, int | None, float | None]:
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    try:
        duration = round(float((probe.get("format") or {}).get("duration")), 3)
    except (TypeError, ValueError):
        duration = None
    if video is None:
        return None, None, duration
    return int(video.get("width", 0)), int(video.get("height", 0)), duration


QC_ROW_KEYS = {"creative_id", "path", "format", "locale", "sha256", "passed", "errors",
               "width", "height", "duration_sec"}


def _validate_qc_report(batch_dir: Path, report: object) -> dict:
    report = _require_exact_dict(
        report, {"passed", "checked_at", "review_manifest_sha256", "candidates"}, "qc_report.json",
    )
    if type(report["passed"]) is not bool:
        raise ValueError("qc_report.json passed must be a boolean")
    if not _is_timestamp(report["checked_at"]):
        raise ValueError("qc_report.json checked_at is invalid")
    if not _is_sha256(report["review_manifest_sha256"]):
        raise ValueError("qc_report.json manifest hash is invalid")
    if type(report["candidates"]) is not list or not report["candidates"]:
        raise ValueError("qc_report.json candidates must be a non-empty array")
    ids = set()
    for row in report["candidates"]:
        row = _require_exact_dict(row, QC_ROW_KEYS, "QC row")
        if type(row["format"]) is not str or row["format"] not in FORMAT_SPECS:
            raise ValueError("QC row format is invalid")
        if type(row["locale"]) is not str or not LOCALE_RE.fullmatch(row["locale"]):
            raise ValueError("QC row locale is invalid")
        _delivery_path_from_value(batch_dir, row["path"])
        if row["sha256"] is not None and not _is_sha256(row["sha256"]):
            raise ValueError("QC row sha256 is invalid")
        if type(row["passed"]) is not bool:
            raise ValueError("QC row passed must be a boolean")
        if (type(row["errors"]) is not list
                or any(type(error) is not str for error in row["errors"])):
            raise ValueError("QC row errors must be an array of strings")
        if row["passed"] is not (not row["errors"]):
            raise ValueError("QC row passed does not match errors")
        if row["creative_id"] in ids:
            raise ValueError("qc_report.json contains duplicate creative_id")
        ids.add(row["creative_id"])
    if report["passed"] is not all(row["passed"] is True for row in report["candidates"]):
        raise ValueError("qc_report.json passed does not match candidate rows")
    return report


def run_spec_qc(batch_dir: Path, prober=None) -> dict:
    batch_dir = _lexical_absolute(batch_dir)
    manifest, manifest_sha = _load_manifest(batch_dir)
    prober = prober or _probe_media
    snapshot_dir = _work_dir(batch_dir, "qc")
    results = []
    for row in manifest["candidates"]:
        path = _delivery_path_from_value(batch_dir, row["path"])
        errors = []
        width = height = duration = None
        snapshot_fd, snapshot_name = tempfile.mkstemp(prefix=".qc-snapshot-", suffix=path.suffix,
                                                      dir=snapshot_dir)
        os.close(snapshot_fd)
        snapshot = Path(snapshot_name)
        current_sha = None
        try:
            current_sha = _copy_and_hash(path, snapshot)
            if current_sha != row["sha256"]:
                errors.append("candidate changed after review manifest")
            try:
                probe = prober(snapshot)
                errors.extend(check_media_spec(probe, row["format"]))
                width, height, duration = _media_summary(probe)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
                errors.append(f"ffprobe failed: {e}")
        finally:
            snapshot.unlink(missing_ok=True)
        results.append({
            "creative_id": row["creative_id"], "path": row["path"], "format": row["format"],
            "locale": row["locale"], "sha256": current_sha, "passed": not errors, "errors": errors,
            "width": width, "height": height, "duration_sec": duration,
        })
    report = {
        "passed": bool(results) and all(row["passed"] for row in results),
        "checked_at": _utc_now(),
        "review_manifest_sha256": manifest_sha,
        "candidates": results,
    }
    _validate_qc_report(batch_dir, report)
    _write_json(batch_dir / QC_REPORT, report)
    return report


# --------------------------------------------------------------------------- usability preflight

PREFLIGHT_STATUSES = {"PASS", "WARN", "FAIL"}


def run_batch_preflight(batch_dir: Path, channel: str | None = None, runner=None) -> dict:
    """Run preflight.py on every reviewed deliverable and bind the outcome to the review manifest."""
    batch_dir = _lexical_absolute(batch_dir)
    metadata = _validate_batch(batch_dir)
    manifest, manifest_sha = _load_manifest(batch_dir)
    if runner is None:
        import preflight  # preflight imports this module; import lazily to avoid a cycle
        runner = preflight.run_preflight
    out_dir = _work_dir(batch_dir, "preflight")
    paths = [_delivery_path_from_value(batch_dir, row["path"]) for row in manifest["candidates"]]
    report = runner(paths, metadata["profile"], None, channel, out_dir)
    by_file = {Path(entry["file"]).resolve(): entry for entry in report["videos"]}
    rows = []
    for row, path in zip(manifest["candidates"], paths):
        entry = by_file.get(path.resolve())
        if entry is None:
            raise RuntimeError(f"preflight did not report {row['path']}")
        issues = [{"check": r["check"], "status": r["status"], "detail": r["detail"]}
                  for r in entry["results"] if r["status"] != "PASS"]
        status = entry["status"]
        if _sha256_file(path) != row["sha256"]:
            issues.append({"check": "changed", "status": "FAIL", "detail": "文件在审核清单之后被改动"})
            status = "FAIL"
        rows.append({"creative_id": row["creative_id"], "path": row["path"], "sha256": row["sha256"],
                     "status": status, "issues": issues})
    result = {
        "passed": all(row["status"] != "FAIL" for row in rows),
        "checked_at": _utc_now(),
        "review_manifest_sha256": manifest_sha,
        "channel": channel,
        "report_html": str(out_dir / "report.html"),
        "candidates": rows,
    }
    _validate_preflight_report(batch_dir, result)
    _write_json(batch_dir / PREFLIGHT_REPORT, result)
    return result


def _validate_preflight_report(batch_dir: Path, report: object) -> dict:
    report = _require_exact_dict(
        report, {"passed", "checked_at", "review_manifest_sha256", "channel", "report_html", "candidates"},
        "preflight_report.json",
    )
    if type(report["passed"]) is not bool or not _is_timestamp(report["checked_at"]):
        raise ValueError("preflight_report.json passed/checked_at is invalid")
    if not _is_sha256(report["review_manifest_sha256"]):
        raise ValueError("preflight_report.json manifest hash is invalid")
    if type(report["candidates"]) is not list or not report["candidates"]:
        raise ValueError("preflight_report.json candidates must be a non-empty array")
    for row in report["candidates"]:
        row = _require_exact_dict(row, {"creative_id", "path", "sha256", "status", "issues"}, "preflight row")
        if row["status"] not in PREFLIGHT_STATUSES or type(row["issues"]) is not list:
            raise ValueError("preflight row status/issues is invalid")
        if not _is_sha256(row["sha256"]):
            raise ValueError("preflight row sha256 is invalid")
        _delivery_path_from_value(batch_dir, row["path"])
    if report["passed"] is not all(row["status"] != "FAIL" for row in report["candidates"]):
        raise ValueError("preflight_report.json passed does not match candidate rows")
    return report


# --------------------------------------------------------------------------- delivery handoff

def deliver(batch_dir: Path) -> Path:
    """Seal an approved, QC-passed review as 50_delivery/manifest.json for the launch tools."""
    batch_dir = _lexical_absolute(batch_dir)
    metadata = _validate_batch(batch_dir)
    manifest, manifest_sha = _load_manifest(batch_dir)
    try:
        approval = _validate_approval(_read_control_json(batch_dir / APPROVAL, "approved.json")[0])
    except ValueError as e:
        raise RuntimeError("deliver blocked: matching human approved.json is required") from e
    if approval["review_manifest_sha256"] != manifest_sha:
        raise RuntimeError("deliver blocked: approval does not match the current review manifest")
    try:
        report = _validate_qc_report(batch_dir, _read_control_json(batch_dir / QC_REPORT, "qc_report.json")[0])
    except ValueError as e:
        raise RuntimeError("deliver blocked: passing QC report is required") from e
    if report["passed"] is not True or report["review_manifest_sha256"] != manifest_sha:
        raise RuntimeError("deliver blocked: QC is failed or stale")

    try:
        preflight = _validate_preflight_report(
            batch_dir, _read_control_json(batch_dir / PREFLIGHT_REPORT, "preflight_report.json")[0])
    except ValueError as e:
        raise RuntimeError("deliver blocked: preflight report is required (batch_pipeline.py preflight)") from e
    if preflight["passed"] is not True or preflight["review_manifest_sha256"] != manifest_sha:
        raise RuntimeError("deliver blocked: preflight has FAIL or is stale")

    qc_by_id = {row["creative_id"]: row for row in report["candidates"]}
    preflight_by_id = {row["creative_id"]: row for row in preflight["candidates"]}
    manifest_ids = {row["creative_id"] for row in manifest["candidates"]}
    if set(qc_by_id) != manifest_ids or set(preflight_by_id) != manifest_ids:
        raise RuntimeError("deliver blocked: QC or preflight rows do not match manifest one-to-one")
    creatives = []
    for row in manifest["candidates"]:
        qc_row = qc_by_id[row["creative_id"]]
        if (qc_row["path"] != row["path"] or qc_row["sha256"] != row["sha256"]
                or qc_row["passed"] is not True):
            raise RuntimeError(f"deliver blocked: QC row does not match manifest: {row['creative_id']}")
        path = _delivery_path_from_value(batch_dir, row["path"])
        if _sha256_file(path) != row["sha256"]:
            raise RuntimeError(f"deliver blocked: file changed after review: {row['path']}")
        parts = parse_creative_id(row["creative_id"])
        creatives.append({
            "creative_id": row["creative_id"],
            "path": row["path"],
            "format": row["format"],
            "locale": row["locale"],
            "hook": parts["hook"],
            "version": parts["version"],
            "width": qc_row["width"],
            "height": qc_row["height"],
            "duration_sec": qc_row["duration_sec"],
            "bytes": path.stat().st_size,
            "sha256": row["sha256"],
            "tags": row["tags"],
            "preflight": {"status": preflight_by_id[row["creative_id"]]["status"],
                          "warnings": [i["detail"] for i in preflight_by_id[row["creative_id"]]["issues"]]},
        })
    handoff = {
        "schema_version": 1,
        "batch_id": batch_dir.name,
        "profile": metadata["profile"],
        "product_code": batch_profile(metadata["profile"])["code"],
        "sealed_at": _utc_now(),
        "review_manifest_sha256": manifest_sha,
        "approved_by": approval["reviewer"],
        "approved_at": approval["approved_at"],
        "qc_checked_at": report["checked_at"],
        "preflight_checked_at": preflight["checked_at"],
        "preflight_report_html": preflight["report_html"],
        "creatives": creatives,
    }
    _write_json(batch_dir / HANDOFF, handoff)
    return batch_dir / HANDOFF


# --------------------------------------------------------------------------- CLI

def _parse_batch_id(value: str) -> str:
    if not BATCH_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("batch-id 必须形如 20261004-topic-name（日期-小写短横线）")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地素材生产批次（Hakko PC / 移动端）")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="初始化批次目录和 batch.json")
    init.add_argument("--profile", required=True, choices=config.batch_profiles())
    init.add_argument("--batch-id", required=True, type=_parse_batch_id)
    init.add_argument("--batch-root", type=Path, help="默认取 profile 的 batch_root")

    name = commands.add_parser("name", help="生成成片素材 ID 与交付路径")
    name.add_argument("batch", type=Path)
    name.add_argument("--locale", required=True)
    name.add_argument("--format", required=True, choices=list(FORMAT_SPECS))
    name.add_argument("--hook", required=True)
    name.add_argument("--version", type=int, default=1)

    analyze = commands.add_parser("analyze", help="对 00_input 跑 analyze.py，产物写到 out/batches/<批次>/analysis")
    analyze.add_argument("batch", type=Path)

    review = commands.add_parser("review", help="扫描 50_delivery，生成待人工审核清单")
    review.add_argument("batch", type=Path)

    approve = commands.add_parser("approve", help="人工看完成片后记录审核通过")
    approve.add_argument("batch", type=Path)
    approve.add_argument("--reviewer", required=True)

    check = commands.add_parser("check-approved", help="检查人工审核门")
    check.add_argument("batch", type=Path)

    normalize = commands.add_parser("normalize", help="只转编码不改画面：H.264/yuv420p、30fps、AAC 48k 立体声")
    normalize.add_argument("input", type=Path)
    normalize.add_argument("output", type=Path)
    normalize.add_argument("--force", action="store_true", help="覆盖已存在的输出")

    burn = commands.add_parser("burn-srt", help="规格归一化后在最后一步烧录 SRT")
    burn.add_argument("input", type=Path)
    burn.add_argument("srt", type=Path)
    burn.add_argument("output", type=Path)
    burn.add_argument("--format", required=True, choices=list(FORMAT_SPECS))
    burn.add_argument("--audio-strategy", default="source", choices=sorted(AUDIO_STRATEGIES))
    burn.add_argument("--audio", type=Path)

    endcard = commands.add_parser("make-endcard", help="生成品牌尾帧（仅有 logo/CTA 配置的 profile）")
    endcard.add_argument("batch", type=Path)
    endcard.add_argument("--format", required=True, choices=list(FORMAT_SPECS))
    endcard.add_argument("--locale", required=True)
    endcard.add_argument("--out", type=Path)

    qc = commands.add_parser("qc", help="对审核清单中的成片跑规格 QC")
    qc.add_argument("batch", type=Path)

    preflight = commands.add_parser("preflight", help="对审核清单中的成片跑可用性预检，生成审片页")
    preflight.add_argument("batch", type=Path)
    preflight.add_argument("--channel", choices=["google", "tiktok"])

    delivery = commands.add_parser("deliver", help="规格 QC、可用性预检、人工审核都通过后写出 50_delivery/manifest.json")
    delivery.add_argument("batch", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        root = args.batch_root or Path(batch_profile(args.profile)["batch_root"])
        data = initialize_batch(root / args.batch_id, args.profile)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.command == "name":
        batch_dir = _lexical_absolute(args.batch)
        metadata = _validate_batch(batch_dir)
        value = creative_id(metadata["profile"], batch_dir.name, args.locale, args.format, args.hook,
                            args.version)
        print(json.dumps({"creative_id": value,
                          "path": str(batch_dir / "50_delivery" / FORMAT_SPECS[args.format][2]
                                      / args.locale / f"{value}.mp4")}, ensure_ascii=False))
        return 0
    if args.command == "analyze":
        print(run_local_analyze(args.batch))
        return 0
    if args.command == "review":
        result = create_review_manifest(args.batch)
        print(json.dumps(result["manifest"], ensure_ascii=False, indent=2))
        if result["ignored"]:
            print(f"ignored in 50_delivery: {', '.join(result['ignored'])}", file=sys.stderr)
        if result["untagged_hooks"]:
            print(f"hooks without 20_plan/creatives.json tags: {', '.join(result['untagged_hooks'])}",
                  file=sys.stderr)
        return 0
    if args.command == "approve":
        print(json.dumps(approve_review(args.batch, args.reviewer), ensure_ascii=False, indent=2))
        return 0
    if args.command == "check-approved":
        approved = is_approved(args.batch)
        print("APPROVED" if approved else "NOT APPROVED")
        return 0 if approved else 1
    if args.command == "normalize":
        if args.output.exists() and not args.force:
            raise ValueError(f"{args.output} 已存在；需要覆盖请加 --force")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(build_normalize_command(args.input, args.output), check=True)
        print(args.output)
        return 0
    if args.command == "burn-srt":
        burn_srt(args.input, args.srt, args.output, args.format, args.audio_strategy, args.audio)
        return 0
    if args.command == "make-endcard":
        print(make_endcard(args.batch, args.format, args.locale, args.out))
        return 0
    if args.command == "qc":
        report = run_spec_qc(args.batch)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.command == "preflight":
        result = run_batch_preflight(args.batch, args.channel)
        for row in result["candidates"]:
            print(f"{row['status']:4} {row['creative_id']}")
            for issue in row["issues"]:
                print(f"       {issue['status']} {issue['check']}: {issue['detail'][:140]}")
        print(f"审片页: {result['report_html']}")
        return 0 if result["passed"] else 1
    if args.command == "deliver":
        print(deliver(args.batch))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
