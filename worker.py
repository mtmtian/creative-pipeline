#!/usr/bin/env python3
"""
worker.py —— remix job 执行客户端（adex control-plane 的 worker 端）。

流程：向 adex 的 3 个接口（claim/report/upload）发起带 HMAC 签名的 HTTP 请求，
认领一个 RemixJob，按 brief.storyboard 逐镜生成视频（dry-run 默认用 ffmpeg 合成
占位片段，零成本零网络；--confirm 才复用 seedance_gen.py 发起真实 Ark 调用），
拼装成片（复用 assemble.py 的转码/拼接/30ms 音频淡入淡出能力），跑品牌预检
（复用 scanner.py/analyze.py 的转写+OCR+品牌匹配），上传成片，逐阶段上报状态。

鉴权：每个 HTTP 请求都带 x-adex-timestamp（秒级时间戳字符串）+ x-adex-signature
（"sha256=" + hex(HMAC_SHA256(secret, f"{timestamp}:{rawBody}"))）。upload 接口
比较特殊：签名基串是 f"{timestamp}:{jobId}:{claimToken}:{content_sha256_hex}"，
不是原始二进制体（HMAC 摘要不适合直接喂二进制body），且必须带 header
x-adex-claim-token。secret 只能来自 --secret 或环境变量 WORKER_WEBHOOK_SECRET，
绝不打印/落盘。

claim 围栏：claim 响应里的 job 带 claimToken（每次 claim 刷新的 UUID）+ attempt
（自增计数），worker 必须原样带着它们走完这个 job 的全流程——report 请求体必填
claimToken，upload 必须带 x-adex-claim-token header。job 被别的 worker re-claim
后，旧 token 在 report/upload 上一律收到 409 {"error":"stale claim"}，worker 见到
就放弃该 job（记日志、非零退出），不重试、不二次上报。

用法：
    python3 worker.py --base-url http://127.0.0.1:8000 --secret test
    python3 worker.py --base-url https://adex.example.com --confirm --job-id job-xxx

任何阶段抛异常都会先尽力上报 status=failed + error 明细，再非零退出（exit 1）；
job=null（没有可用任务）时打印提示后 exit 0，不是失败。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import assemble
import config
import cut_ref
import scanner
import seedance_gen
from analyze import extract_audio_wav, ffprobe_json, get_duration_sec, whisper_transcribe
from qc import scan_frames_for_brand_hits

UPLOAD_MAX_BYTES = 100 * 1024 * 1024  # 100MB 上传体积上限（客户端侧拦截）
REF_VIDEO_MAX_BYTES = 200 * 1024 * 1024  # 200MB T2 参照源视频下载体积上限（客户端侧拦截）

# 真实模式下用于估算 costTokens 的分辨率/帧率假设，对齐 seedance_gen.py 里记录的
# 真实任务实测参数（720p 9:16 24fps，见 seedance_gen.py TOKENS_PER_SEC_720P 注释）。
GEN_WIDTH = 720
GEN_HEIGHT = 1280
GEN_FPS = 24

DRYRUN_COLORS = {
    "hook": "0x336699",
    "scene": "0x663399",
    "end-card": "0x339966",
}
DRYRUN_DEFAULT_COLOR = "0x555555"

# brief.ratio -> 画布尺寸 (width, height)。brief 缺 ratio 或值不在这张表里时按 9:16 处理。
RATIO_CANVAS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}
DEFAULT_RATIO = "9:16"


def resolve_canvas(ratio: str | None) -> tuple[int, int]:
    return RATIO_CANVAS.get(ratio or DEFAULT_RATIO, RATIO_CANVAS[DEFAULT_RATIO])


class WorkerError(Exception):
    """流程失败，需要上报 status=failed 并非零退出，不静默吞掉。"""


class StaleClaimError(WorkerError):
    """claim 已被抢占（claimToken 过期/被其他 worker re-claim），worker 必须放弃该
    job，不重试，也不再尝试上报（再报也是同样的 409）。"""


# ---------------------------------------------------------------------------
# HMAC 签名 + HTTP
# ---------------------------------------------------------------------------

def _sign(secret: str, timestamp: str, base_body: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}:{base_body}".encode("utf-8"), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _raise_for_http_error(path: str, e: urllib.error.HTTPError, action: str = "") -> None:
    """HTTP 409 + body {"error":"stale claim"} 时抛 StaleClaimError（worker 该放弃这个
    job，不重试），其余错误仍按老样子抛 WorkerError。"""
    err_body = e.read().decode("utf-8", errors="replace")
    if e.code == 409:
        try:
            parsed = json.loads(err_body)
        except Exception:
            parsed = {}
        if parsed.get("error") == "stale claim":
            raise StaleClaimError(f"POST {path} {action}失败 HTTP 409: {err_body}") from e
    raise WorkerError(f"POST {path} {action}失败 HTTP {e.code}: {err_body}") from e


def _post_json(base_url: str, path: str, secret: str, payload: dict) -> dict:
    body_bytes = json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = _sign(secret, timestamp, body_bytes.decode("utf-8"))

    req = urllib.request.Request(base_url + path, data=body_bytes, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-adex-timestamp", timestamp)
    req.add_header("x-adex-signature", signature)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _raise_for_http_error(path, e)
    except urllib.error.URLError as e:
        raise WorkerError(f"POST {path} 网络错误: {e}") from e


def _post_bytes(base_url: str, path: str, secret: str, data: bytes, job_id: str, claim_token: str) -> dict:
    if len(data) > UPLOAD_MAX_BYTES:
        raise WorkerError(
            f"上传体积 {len(data)} 字节超过客户端上限 {UPLOAD_MAX_BYTES} 字节（100MB），拒绝上传"
        )
    content_sha256 = hashlib.sha256(data).hexdigest()
    timestamp = str(int(time.time()))
    # upload 接口的签名基串是 f"{timestamp}:{jobId}:{claimToken}:{content_sha256_hex}"，
    # 不是原始二进制体，且绑定了 job/claimToken（防止 stale worker 的签名被复用）——
    # 复用 _sign()，把 f"{jobId}:{claimToken}:{content_sha256}" 当 base_body 传进去，
    # 它会自动拼出 f"{timestamp}:{base_body}"，跟契约要求的基串完全一致。
    signature = _sign(secret, timestamp, f"{job_id}:{claim_token}:{content_sha256}")

    req = urllib.request.Request(base_url + path, data=data, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("x-adex-timestamp", timestamp)
    req.add_header("x-adex-signature", signature)
    req.add_header("x-adex-content-sha256", content_sha256)
    req.add_header("x-adex-claim-token", claim_token)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _raise_for_http_error(path, e, action="上传")
    except urllib.error.URLError as e:
        raise WorkerError(f"POST {path} 上传网络错误: {e}") from e


def claim_job(base_url: str, secret: str, job_id: str | None) -> dict | None:
    payload = {"jobId": job_id} if job_id else {}
    resp = _post_json(base_url, "/api/worker/remix-jobs/claim", secret, payload)
    return resp.get("job")


def report(base_url: str, secret: str, job_id: str, claim_token: str, status: str, **fields) -> dict:
    payload: dict = {"jobId": job_id, "claimToken": claim_token, "status": status}
    for k, v in fields.items():
        if v is not None:
            payload[k] = v
    return _post_json(base_url, "/api/worker/remix-jobs/report", secret, payload)


def upload_video(base_url: str, secret: str, job_id: str, claim_token: str, data: bytes) -> str:
    resp = _post_bytes(
        base_url, f"/api/worker/remix-jobs/upload?jobId={job_id}", secret, data, job_id, claim_token
    )
    file_url = resp.get("fileUrl")
    if not file_url:
        raise WorkerError(f"upload 响应缺少 fileUrl 字段: {resp}")
    return file_url


def download_ref_video(url: str, dest_path: Path) -> None:
    """下载 T2 job.refs[0].url 指向的竞品源视频到本地工作目录（≤200MB 上限，边下边数
    字节，不只信 Content-Length 头——避免响应头撒谎导致超限文件被下满）。失败/超限都
    抛 WorkerError（超限时清理已写入的部分文件，不留半成品）。"""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f:
            total = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > REF_VIDEO_MAX_BYTES:
                    raise WorkerError(
                        f"参照源视频下载体积超过客户端上限 {REF_VIDEO_MAX_BYTES} 字节（200MB），"
                        f"拒绝下载: {url}"
                    )
                f.write(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        dest_path.unlink(missing_ok=True)
        raise WorkerError(f"下载参照源视频失败: {url}: {e}") from e
    except WorkerError:
        dest_path.unlink(missing_ok=True)
        raise
    if not dest_path.is_file() or dest_path.stat().st_size == 0:
        raise WorkerError(f"参照源视频下载后文件为空: {url}")


# ---------------------------------------------------------------------------
# T2 segmentPlan 归一化：control-plane 的 {start,end,...} 数值形态 / classify_llm.py
# 本地产的 {time_range:"0-11.12s",...} 字符串形态，统一成 {start,end,action,description}。
# ---------------------------------------------------------------------------

def normalize_segment_plan(raw_plan: list[dict]) -> list[dict]:
    """归一化 + 校验 job.segmentPlan：
      - 接受 {start,end,action,description?} 数值形态（control-plane 契约）；
      - 也接受 {time_range:"0-11.12s",action,description?/reason?} 字符串形态
        （本地 classify_llm.py 产的形态），用 assemble.parse_time_range 解析
        （同一份正则，不重新实现时间段解析）。
      - 按 start 升序排序（control-plane 契约保证升序，这里再兜底排一次）。
      - 校验：至少一个 reuse 段；段间不重叠（允许有 gap——gap 就是被 drop 的部分，
        契约里说了可能有 gap，不强制无缝覆盖，这点跟 classify_llm.py 本地 segment_plan
        的“全片无缝覆盖”硬性约束不同，不能套用那份校验）。
    校验失败一律抛 WorkerError，不静默丢段/静默改动作。"""
    normalized = []
    for seg in raw_plan:
        if "start" in seg and "end" in seg:
            start, end = float(seg["start"]), float(seg["end"])
        elif "time_range" in seg:
            parsed = assemble.parse_time_range(str(seg["time_range"]))
            if parsed is None:
                raise WorkerError(f"segmentPlan 段 time_range 解析失败: {seg.get('time_range')!r}")
            start, end = parsed
        else:
            raise WorkerError(f"segmentPlan 段缺少 start/end 或 time_range 字段: {seg}")

        action = seg.get("action")
        if action not in ("reuse", "remake", "drop"):
            raise WorkerError(f"segmentPlan 段 action 字段不合法: {action!r}（应为 reuse/remake/drop）")

        normalized.append({
            "start": start,
            "end": end,
            "action": action,
            "description": seg.get("description") or seg.get("reason") or "",
        })

    if not normalized:
        raise WorkerError("segmentPlan 为空，T2 job 至少需要一段")

    normalized.sort(key=lambda s: s["start"])

    if not any(s["action"] == "reuse" for s in normalized):
        raise WorkerError("segmentPlan 中没有任何 reuse 段（T2 的存在前提就是至少有一段可直接复用）")

    for i in range(len(normalized) - 1):
        cur, nxt = normalized[i], normalized[i + 1]
        if cur["end"] > nxt["start"] + 1e-6:
            raise WorkerError(
                f"segmentPlan 段重叠: [{cur['start']:g},{cur['end']:g}) 与 "
                f"[{nxt['start']:g},{nxt['end']:g})，不允许重叠（gap 允许，代表被 drop）"
            )

    return normalized


# ---------------------------------------------------------------------------
# 逐镜生成：dry-run（本地 ffmpeg 占位）/ --confirm（真实 Ark 调用，复用 seedance_gen.py）
# ---------------------------------------------------------------------------

def gen_clip_dryrun(role: str, index: int, seconds: float, out_path: Path,
                     canvas: tuple[int, int]) -> None:
    """dry-run 占位 clip：按 brief.ratio 决定的画布尺寸（见 RATIO_CANVAS）纯色背景 +
    drawtext 标注 role/index + 静音音轨（aevalsrc），零成本零网络调用。音轨是必须的——
    下游 assemble/QC 都要有真实音轨。"""
    dur = max(float(seconds), 0.1)
    color = DRYRUN_COLORS.get(role, DRYRUN_DEFAULT_COLOR)
    label = f"{role} #{index}".replace("'", "")
    width, height = canvas
    cmd = [
        config.FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:d={dur}:r=30",
        "-f", "lavfi", "-i", f"aevalsrc=0:s=48000:d={dur}",
        "-vf", f"drawtext=text='{label}':fontcolor=white:fontsize=60:"
               f"x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-shortest", str(out_path),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode != 0 or not out_path.exists():
        raise WorkerError(
            f"dry-run 占位 clip 生成失败（beat {index}/{role}）: {cp.stderr[-2000:]}"
        )


def gen_clip_real(api_key: str, prompt: str, ratio: str, duration_sec: int, out_path: Path) -> str:
    """真实模式：复用 seedance_gen.py 的请求体组装 + 提交/轮询/下载函数，不重新实现。
    返回 Ark task id。"""
    body = seedance_gen.build_request_body(
        prompt=prompt,
        ratio=ratio,
        duration_sec=duration_sec,
        generate_audio=False,
        ref_images=[],
        ref_videos=[],
        ref_audios=[],
    )
    task = seedance_gen.create_task(api_key, body)
    task_id = task.get("id")
    if not task_id:
        raise WorkerError(f"创建 Ark 任务响应中没有 id 字段: {task}")

    final_task = seedance_gen.poll_task(api_key, task_id)
    if final_task.get("status") != "succeeded":
        err = final_task.get("error", {})
        raise WorkerError(
            f"Ark 任务 {task_id} 未成功完成: status={final_task.get('status')} "
            f"error={err.get('code')}: {err.get('message')}"
        )

    # 同 seedance_gen.execute_real：Ark 响应把结果放在 content.video_url。
    video_url = (final_task.get("content") or {}).get("video_url") or (
        final_task.get("output") or {}
    ).get("video_url")
    if not video_url:
        raise WorkerError(f"Ark 任务 {task_id} succeeded 但响应中没有 video_url: {final_task}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    seedance_gen.download_video(video_url, out_path)
    return task_id


# ---------------------------------------------------------------------------
# T1 @视频锚定重生成：按 beat 时间窗口从竞品源视频切参照片段 + 组装带 refs 的
# Ark 请求（复用 cut_ref.py 的切片/校验函数，seedance_gen.py 的请求体组装已经
# 支持 ref_videos 参数，不需要新增字段）。
# ---------------------------------------------------------------------------

def compute_ref_window(cum_start: float, cum_end: float, source_duration: float) -> tuple[float, float]:
    """按 beat 在 storyboard 时间轴上的累计偏移 [cum_start, cum_end) 计算参照片段的
    切割窗口，clamp 到 Ark 约束 [cut_ref.ARK_VIDEO_MIN_SEC, cut_ref.ARK_VIDEO_MAX_SEC]。

    - 源视频短于该 beat 所需窗口（source_duration < cum_end）：整段源视频作参照
      （仍 clamp 到 15s 上限）。
    - 窗口本身短于 2s：先尝试往后扩窗口到 2s；源视频剩余长度不够往后扩的话，再
      往前借（把 start 往回退），尽量凑够 2s——源视频总长本身就 < 2s 的极端情况
      无法凑够，留给 cut_ref.cut_segment() 的硬性校验兜底报错，不在这里假装成功。
    - 窗口长于 15s：直接截到 15s。
    """
    if source_duration < cum_end - 1e-6:
        start, end = 0.0, min(source_duration, cut_ref.ARK_VIDEO_MAX_SEC)
        return start, end

    start, end = cum_start, cum_end
    dur = end - start
    if dur < cut_ref.ARK_VIDEO_MIN_SEC:
        end = min(start + cut_ref.ARK_VIDEO_MIN_SEC, source_duration)
        dur = end - start
        if dur < cut_ref.ARK_VIDEO_MIN_SEC:
            start = max(0.0, end - cut_ref.ARK_VIDEO_MIN_SEC)
    elif dur > cut_ref.ARK_VIDEO_MAX_SEC:
        end = start + cut_ref.ARK_VIDEO_MAX_SEC

    if end - start > cut_ref.ARK_VIDEO_MAX_SEC:
        end = start + cut_ref.ARK_VIDEO_MAX_SEC
    return start, end


def ensure_ref_clip_size(path: Path) -> None:
    """T1 参照片段体积兜底：cut_ref 无损快切出的片段若 ≥50MB（Ark 约束，见
    cut_ref.ARK_VIDEO_MAX_BYTES），转码到 720p 重新压缩后再检查一次；转码后仍
    超限就明确报错，不静默放行、不悄悄截断。"""
    size = path.stat().st_size
    if size < cut_ref.ARK_VIDEO_MAX_BYTES:
        return
    tmp_path = path.with_suffix(".720p.mp4")
    cmd = [
        config.FFMPEG, "-y", "-i", str(path), "-vf", "scale=-2:720",
        "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k", str(tmp_path),
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode != 0 or not tmp_path.exists():
        raise WorkerError(f"参照片段转码 720p 兜底失败: {path}: {cp.stderr[-2000:]}")
    tmp_path.replace(path)
    new_size = path.stat().st_size
    if new_size >= cut_ref.ARK_VIDEO_MAX_BYTES:
        raise WorkerError(
            f"参照片段转码 720p 后仍超过 Ark 约束的 50MB 上限: {path} "
            f"({new_size / 1024 / 1024:.2f}MB)"
        )


def cut_beat_ref_clip(source_path: Path, start: float, end: float, out_dir: Path, index: int) -> Path:
    """从源视频切出第 index 个 beat 的参照片段（复用 cut_ref.cut_segment 的 ffmpeg
    切片逻辑 + cut_ref.validate_ark_constraints 的 2-15s/50MB/mp4 校验，不重新实现），
    超限先走 720p 转码兜底，仍不满足就抛 WorkerError（失败干净上报，不挂死）。"""
    try:
        dest = cut_ref.cut_segment(source_path, start, end, out_dir, reencode=False)
    except cut_ref.CutRefError as e:
        raise WorkerError(f"参照片段切割失败（beat {index}）: {e}") from e
    ensure_ref_clip_size(dest)
    try:
        cut_ref.validate_ark_constraints(dest)
    except cut_ref.CutRefError as e:
        raise WorkerError(f"参照片段不满足 Ark 约束（beat {index}）: {e}") from e
    return dest


def build_t1_ref_payload_preview(prompt: str, ratio: str, duration_sec: float,
                                  ref_clip_path: Path) -> dict:
    """dry-run 专用：构造完整的 Ark 请求 payload 预览（含 refs 字段），dump 到 out
    目录 json 文件供检查，证明切片逻辑真的跑通了、参照片段真的被组进了请求体。

    字段名对齐 seedance_gen.build_request_body/build_reference_content 已有的
    content 数组形态（type="video_url"，值形如 {"url": ...}，role="reference_video"）
    ——这部分已核对过 adex ts 客户端 src/lib/platforms/seedance2.ts 的 ContentItem
    定义，有代码依据，不是瞎编的。

    但这里特意不直接调 build_reference_content()：那个函数对本地文件路径会强制
    报错拒绝（是专门为真实调用设计的防线——Ark contents 接口只认公网 URL，见其
    docstring），而 dry-run 阶段参照片段还没有上传到公网存储，所以这里用本地路径
    占位并显式标成 "LOCAL_PENDING_UPLOAD:<path>"，不假装它是一个 Ark 真能消费的
    URL。多参照片段场景是否需要额外的顺序/时间戳字段，仓库里目前没有真实调用
    验证过，字段名待首次真实调用校验。"""
    body = seedance_gen.build_request_body(
        prompt=prompt, ratio=ratio, duration_sec=duration_sec,
        generate_audio=False, ref_images=[], ref_videos=[], ref_audios=[],
    )
    body["content"].append({
        "type": "video_url",
        "video_url": {"url": f"LOCAL_PENDING_UPLOAD:{ref_clip_path}"},
        "role": "reference_video",  # 字段名待首次真实调用校验
    })
    return body


def gen_clip_real_t1(api_key: str, prompt: str, ratio: str, duration_sec: int,
                      ref_video_url: str, out_path: Path) -> str:
    """T1 真实模式：复用 seedance_gen 的请求体组装/提交/轮询/下载函数，带 refs 字段
    发起 Ark 调用（build_request_body 的 ref_videos 参数早就支持，不需要改
    seedance_gen.py）。返回 Ark task id。

    已知限制（如实标注，不假装解决了）：仓库里目前没有把本地切出的 beat 级参照
    片段上传到公网存储的能力——cut_ref.py 本身的设计也是"切完手动上传"（见其 CLI
    结尾提示），不是自动化闭环。所以这里真实调用时带上的参照视频 URL 是
    job.refs[0].url 指向的完整竞品源视频（已经过 download_ref_video() 验证公网
    可下载），不是逐 beat 切出的本地片段；本地切片只用于 dry-run 阶段验证切片
    逻辑本身正确 + payload 预览。等仓库里有可用的公网存储上传能力后，可以把这里
    换成上传后的逐 beat 切片 URL，让 Ark 拿到的参照片段跟 storyboard 时间窗口
    对得更准。"""
    body = seedance_gen.build_request_body(
        prompt=prompt, ratio=ratio, duration_sec=duration_sec,
        generate_audio=False, ref_images=[], ref_videos=[ref_video_url], ref_audios=[],
    )
    task = seedance_gen.create_task(api_key, body)
    task_id = task.get("id")
    if not task_id:
        raise WorkerError(f"创建 Ark 任务响应中没有 id 字段: {task}")

    final_task = seedance_gen.poll_task(api_key, task_id)
    if final_task.get("status") != "succeeded":
        err = final_task.get("error", {})
        raise WorkerError(
            f"Ark 任务 {task_id} 未成功完成: status={final_task.get('status')} "
            f"error={err.get('code')}: {err.get('message')}"
            f"（T1 常见失败原因：写实真人脸参照视频触发 Ark content policy 拦截，"
            f"这里如实报告，不重试、不伪造成功）"
        )

    video_url = (final_task.get("content") or {}).get("video_url") or (
        final_task.get("output") or {}
    ).get("video_url")
    if not video_url:
        raise WorkerError(f"Ark 任务 {task_id} succeeded 但响应中没有 video_url: {final_task}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    seedance_gen.download_video(video_url, out_path)
    return task_id


def trim_clip_to_storyboard_duration(clip_path: Path, storyboard_seconds: float) -> float:
    """真实模式下 Ark 生成的 clip 实际时长可能因为 clamping/尾帧生成而超过 storyboard
    指定的 beat.seconds。若发现超出，用 ffmpeg -t 裁到 storyboard 指定时长（优先
    -c copy 无损 remux，remux 失败/产物无效则回退重新编码）。

    返回裁剪后（或本就没超出、无需裁剪时的原始）clip 实际时长——ffprobe 实测值，供
    调用方把 beats[i].seconds 更新成真实值，而不是继续用 storyboard 里的名义值。"""
    probe = ffprobe_json(clip_path)
    actual = get_duration_sec(probe)
    if actual <= storyboard_seconds + 0.01:  # 小容差，避免因浮点误差误判需要裁剪
        return actual

    trimmed_path = clip_path.with_suffix(".trim.mp4")
    remux_cmd = [
        config.FFMPEG, "-y", "-i", str(clip_path), "-t", f"{storyboard_seconds:.3f}",
        "-c", "copy", str(trimmed_path),
    ]
    cp = subprocess.run(remux_cmd, capture_output=True, text=True)
    remux_ok = cp.returncode == 0 and trimmed_path.exists() and trimmed_path.stat().st_size > 0
    if not remux_ok:
        trimmed_path.unlink(missing_ok=True)
        reencode_cmd = [
            config.FFMPEG, "-y", "-i", str(clip_path), "-t", f"{storyboard_seconds:.3f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(trimmed_path),
        ]
        cp = subprocess.run(reencode_cmd, capture_output=True, text=True)
        if cp.returncode != 0 or not trimmed_path.exists():
            raise WorkerError(
                f"裁剪 clip 到 storyboard 时长失败（remux 和重新编码都失败）: "
                f"{clip_path}: {cp.stderr[-2000:]}"
            )

    trimmed_path.replace(clip_path)
    final_probe = ffprobe_json(clip_path)
    return get_duration_sec(final_probe)


# ---------------------------------------------------------------------------
# 拼装：复用 assemble.py 的转码/拼接函数（不重新实现 ffmpeg filter graph）
# ---------------------------------------------------------------------------

def _build_storyboard_segments(beats: list[dict]) -> list[dict]:
    """按 beats[i].seconds（real 模式下已经是 ffprobe 实测的最终时长，dry-run 模式下是
    storyboard 指定的名义值）累计出 differentiated_storyboard 形状的 time_range 段列表，
    全部标 source=generated（worker.py 每个 beat 都是自己生成的，没有 reuse 段）。"""
    segs = []
    start = 0.0
    for b in beats:
        end = start + float(b["seconds"])
        segs.append({"time_range": f"{start:g}-{end:g}s", "source": "generated"})
        start = end
    return segs


def assemble_final(beats: list[dict], clip_paths: dict[int, Path], out_root: Path,
                    canvas: tuple[int, int]) -> Path:
    """把逐镜生成的 clip 拼成成片（画布尺寸由 brief.ratio 决定，见 RATIO_CANVAS），
    30ms 音频淡入淡出——直接调用 assemble.py 里已有的
    load_gen_clip_map/resolve_segments/set_target_canvas/normalize_one/concat_filter，
    不重新实现拼接逻辑。"""
    assemble.set_target_canvas(*canvas)

    segs = _build_storyboard_segments(beats)
    gen_clip_args = [f"{seg['time_range']}={clip_paths[i]}" for i, seg in enumerate(segs)]
    gen_clip_map = assemble.load_gen_clip_map(gen_clip_args)

    resolved, problems = assemble.resolve_segments(segs, None, gen_clip_map)
    if problems:
        raise WorkerError("assemble 前置检查失败: " + "; ".join(problems))

    scratch = out_root / ".assemble_scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    n = len(resolved)
    normalized = []
    for i, seg in enumerate(resolved):
        fade_in = i > 0
        fade_out = i < n - 1
        # dur_hint 直接用 beats[i].seconds（已知的真实/名义时长），不用再对每个 clip
        # 重新跑一次 ffprobe。
        dur_hint = float(beats[i]["seconds"])
        clip_path = assemble.normalize_one(i, seg, scratch, fade_in, fade_out, dur_hint)
        normalized.append(clip_path)

    out_path = out_root / "assembled.mp4"
    assemble.concat_filter(normalized, out_path)
    return out_path


def assemble_t2(segments: list[dict], source_path: Path, clip_paths: dict[int, Path],
                 out_root: Path, canvas: tuple[int, int]) -> Path:
    """T2 分段路由拼装：复用 assemble.py 的 reuse-cut + gen-clip 混合拼装能力
    （load_gen_clip_map/resolve_segments/normalize_one/concat_filter）——它当初就是
    为“一部分段落切原片、一部分段落接生成产物”这种混合场景建的，这里直接按它的接口
    组 segment 列表调用，不重新实现。drop 段直接跳过，不进入 resolved 列表。"""
    assemble.set_target_canvas(*canvas)

    assemble_segs: list[dict] = []
    gen_clip_args: list[str] = []
    dur_hints: list[float] = []

    for i, seg in enumerate(segments):
        if seg["action"] == "drop":
            continue
        time_range_text = f"{seg['start']:g}-{seg['end']:g}s"
        if seg["action"] == "reuse":
            assemble_segs.append({
                "time_range": time_range_text,
                "source": "reuse",
                "reuse_cut": {"start_sec": seg["start"], "end_sec": seg["end"]},
            })
        else:  # remake
            assemble_segs.append({"time_range": time_range_text, "source": "generated"})
            gen_clip_args.append(f"{time_range_text}={clip_paths[i]}")
        dur_hints.append(seg["end"] - seg["start"])

    gen_clip_map = assemble.load_gen_clip_map(gen_clip_args)
    resolved, problems = assemble.resolve_segments(assemble_segs, source_path, gen_clip_map)
    if problems:
        raise WorkerError("assemble 前置检查失败: " + "; ".join(problems))

    scratch = out_root / ".assemble_scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    n = len(resolved)
    normalized = []
    for i, seg in enumerate(resolved):
        fade_in = i > 0
        fade_out = i < n - 1
        clip_path = assemble.normalize_one(i, seg, scratch, fade_in, fade_out, dur_hints[i])
        normalized.append(clip_path)

    out_path = out_root / "assembled.mp4"
    assemble.concat_filter(normalized, out_path)
    return out_path


# ---------------------------------------------------------------------------
# QC：品牌词重扫（复用 scanner.py 的匹配逻辑 + analyze.py 的转写/OCR 抽取函数）
# ---------------------------------------------------------------------------

def qc_scan(video_path: Path, scratch: Path) -> dict:
    """对成片重跑 whisper 转写 + OCR 抽帧，用 scanner.find_brand_hits 扫品牌词，
    返回 {"pass": bool, "hits": [{"t", "source", "brand", "text"}, ...]}。

    品牌匹配逻辑完全复用 scanner.py（含 config.BRANDS 里的 ASR 误识别变体）；
    OCR 抽帧扫描复用 qc.py 的 scan_frames_for_brand_hits（底层是 analyze.ocr_video：单次
    ffmpeg 抽帧 + Apple Vision / tesseract 识别），只是把结构化 hits 整理成 API
    契约要求的形状，而不是 qc.py 那种拼接成一行 detail 字符串的 CSV 行——两边输出
    契约不同，无法直接调用 qc.py 的 qc_audio/qc_ocr，但底层的转写/抽帧/OCR/匹配函数
    （analyze.py + qc.py + scanner.py）原样复用，没有重新实现。"""
    scratch.mkdir(parents=True, exist_ok=True)
    hits: list[dict] = []

    wav_path = scratch / f"{video_path.stem}.wav"
    if extract_audio_wav(video_path, wav_path):
        for seg in whisper_transcribe(wav_path):
            for hit in scanner.find_brand_hits(seg["text"]):
                hits.append({
                    "t": seg["start"], "source": "asr",
                    "brand": hit.brand, "text": hit.matched_text,
                })
        wav_path.unlink(missing_ok=True)

    frame_hits, _frame_count = scan_frames_for_brand_hits(video_path, scratch)
    for h in frame_hits:
        hits.append({"t": h["ts"], "source": "ocr", "brand": h["brand"], "text": h["matched_text"]})

    return {"pass": len(hits) == 0, "hits": hits}


def total_duration_from_beats(beats: list[dict], final_path: Path) -> float:
    """QC 阶段用的成片总时长：优先直接对 beats[i].seconds 求和（real 模式下已经是
    ffprobe 实测的最终时长，dry-run 模式下是 storyboard 指定值），不用再对成片重新
    跑一次 ffprobe；只有 beats 里的 seconds 缺失/不可信（求和 <= 0）时才回退对
    final_path 跑 ffprobe 兜底。"""
    try:
        total = sum(float(b["seconds"]) for b in beats)
        if total > 0:
            return total
    except (KeyError, TypeError, ValueError):
        pass
    probe = ffprobe_json(final_path)
    return get_duration_sec(probe)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _fail(base_url: str, secret: str, job_id: str, claim_token: str, beats: list[dict],
          error_msg: str) -> None:
    for beat in beats:
        if beat.get("status") == "generating":
            beat["status"] = "failed"
    print(f"任务失败: {error_msg}", file=sys.stderr)
    try:
        report(base_url, secret, job_id, claim_token, "failed", beats=beats, error=error_msg)
    except Exception as report_exc:
        print(f"（另外，上报 status=failed 本身也失败了: {report_exc}）", file=sys.stderr)
    sys.exit(1)


def run_t0_5(job: dict, args: argparse.Namespace, secret: str, base_url: str,
             out_root: Path, beats: list[dict]) -> None:
    """T0.5 路径：逐 beat 生成（原有唯一路径，逻辑不动，只是从 run_worker 里搬出来
    腾位置给 T2 分派）。beats 由调用方传入的空列表就地填充，供失败时 _fail() 上报。"""
    job_id = job["id"]
    claim_token = job.get("claimToken")
    if not claim_token:
        raise WorkerError(f"claim 响应缺少 claimToken 字段: {job}")
    brief = job["brief"]
    storyboard = brief["storyboard"]
    ratio = brief.get("ratio") or DEFAULT_RATIO
    canvas = resolve_canvas(ratio)

    beats.extend(
        {"index": i, "role": b["role"], "seconds": b["seconds"], "status": "pending"}
        for i, b in enumerate(storyboard)
    )

    clips_dir = out_root / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    report(base_url, secret, job_id, claim_token, "running", beats=beats)

    # 成本护栏（成本护栏拦截，pre-check）：--confirm 模式下，在发起任何一次 Ark 付费
    # 调用之前，先数一遍 storyboard 里总共有多少个 beat 需要生成；超过 --max-clips
    # 就直接判定失败，零成本退出——不要等跑到第 N+1 条 beat 时才在循环里发现超限
    # （那样前 N 条已经真金白银调用过 Ark 了）。下面循环体里的 per-beat 护栏
    # （clips_generated >= args.max_clips）继续保留，作为兜底安全网，不删除。
    if args.confirm:
        beats_needing_gen = len(storyboard)
        if beats_needing_gen > args.max_clips:
            raise WorkerError(
                f"成本护栏拦截：storyboard has {beats_needing_gen} beats exceeding "
                f"--max-clips={args.max_clips}, pass --max-clips {beats_needing_gen} "
                f"explicitly（未发起任何 Ark 调用，零成本）。"
            )

    api_key = None
    if args.confirm:
        api_key = config.get_ark_api_key()
        if not api_key:
            raise WorkerError(
                "无法获取 Ark API key：hakko-secret 探测的所有密钥名均不可用。"
                "如实报告：真实调用不可能发生，未伪造任何生成结果。"
            )

    clips_generated = 0
    cost_tokens = 0.0
    clip_paths: dict[int, Path] = {}

    for i, b in enumerate(storyboard):
        beats[i]["status"] = "generating"
        out_path = clips_dir / f"beat_{i:02d}_{b['role']}.mp4"

        if args.confirm:
            if clips_generated >= args.max_clips:
                raise WorkerError(
                    f"成本护栏拦截：本次运行已生成 {clips_generated} 条 clip，"
                    f"达到 --max-clips={args.max_clips} 上限，拒绝生成第 {i} 条 "
                    f"beat（role={b['role']}），未发起该条 clip 的生成请求。"
                )
            duration_int = int(round(min(max(float(b["seconds"]), 3), 10)))
            prompt = f"{brief['seedance2Prompt']} {b['description']}"
            task_id = gen_clip_real(api_key, prompt, ratio, duration_int, out_path)
            clips_generated += 1
            cost_tokens += GEN_WIDTH * GEN_HEIGHT * GEN_FPS * duration_int / 1024
            # Ark 实际产出的 clip 时长可能因 clamping/尾帧生成而超过 storyboard
            # 指定的 beat.seconds；超出就裁到 storyboard 时长，job report 里的
            # beats[i].seconds 必须是裁剪后 ffprobe 实测的真实值，不是名义值。
            actual_seconds = trim_clip_to_storyboard_duration(out_path, float(b["seconds"]))
            beats[i].update(status="done", taskId=task_id, videoUrl=str(out_path),
                             seconds=actual_seconds)
        else:
            gen_clip_dryrun(b["role"], i, b["seconds"], out_path, canvas)
            beats[i].update(status="done", videoUrl=str(out_path))

        clip_paths[i] = out_path
        report(base_url, secret, job_id, claim_token, "running", beats=beats)

    report(base_url, secret, job_id, claim_token, "assembling", beats=beats)
    final_path = assemble_final(beats, clip_paths, out_root, canvas)

    qc_report = qc_scan(final_path, out_root / ".qc_scratch")
    qc_report["durationSec"] = round(total_duration_from_beats(beats, final_path), 2)
    report(base_url, secret, job_id, claim_token, "qc", beats=beats,
           qcReport=qc_report, costTokens=round(cost_tokens, 2))

    data = final_path.read_bytes()
    file_url = upload_video(base_url, secret, job_id, claim_token, data)

    report(base_url, secret, job_id, claim_token, "succeeded", beats=beats, qcReport=qc_report,
           costTokens=round(cost_tokens, 2), outputUrl=file_url)

    print(f"\njob {job_id} succeeded")
    print(f"  outputUrl    = {file_url}")
    print(f"  assembled mp4 = {final_path}")
    print(f"  qcReport.pass = {qc_report['pass']}")
    print(f"  costTokens    = {round(cost_tokens, 2)}")


def run_t2(job: dict, args: argparse.Namespace, secret: str, base_url: str,
           out_root: Path, beats: list[dict]) -> None:
    """T2 分段路由路径：下载 job.refs[0].url 的竞品源视频，按 job.segmentPlan
    reuse/remake/drop 三段路由，reuse 段直接切原片、remake 段走占位/真实生成，
    drop 段跳过，最后用 assemble_t2() 混合拼装。beats 语义改成 segments：
    beats=[{index, role: action, status, seconds, videoUrl?}]（role 字段放 action
    值，契约仍是数组，字段名不变）。"""
    job_id = job["id"]
    claim_token = job.get("claimToken")
    if not claim_token:
        raise WorkerError(f"claim 响应缺少 claimToken 字段: {job}")
    brief = job["brief"]
    ratio = brief.get("ratio") or DEFAULT_RATIO
    canvas = resolve_canvas(ratio)

    refs = job.get("refs") or []
    if not refs or not refs[0].get("url"):
        raise WorkerError("T2 job 缺少 refs[0].url（竞品参照源视频公网地址）")
    ref_url = refs[0]["url"]

    raw_plan = job.get("segmentPlan")
    if not raw_plan:
        raise WorkerError("T2 job 缺少 segmentPlan")
    segments = normalize_segment_plan(raw_plan)

    beats.extend(
        {"index": i, "role": seg["action"], "status": "pending",
         "seconds": round(seg["end"] - seg["start"], 3)}
        for i, seg in enumerate(segments)
    )

    clips_dir = out_root / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    report(base_url, secret, job_id, claim_token, "running", beats=beats)

    source_path = out_root / "source_ref.mp4"
    download_ref_video(ref_url, source_path)

    # 成本护栏 pre-check：跟 T0.5 一样，在发起任何 Ark 调用之前先数一遍 remake 段数
    # （只有 remake 段要真实生成，reuse/drop 都不产生 Ark 调用），超过 --max-clips
    # 直接零成本失败退出。
    remake_count = sum(1 for s in segments if s["action"] == "remake")
    if args.confirm and remake_count > args.max_clips:
        raise WorkerError(
            f"成本护栏拦截：segmentPlan has {remake_count} remake 段 exceeding "
            f"--max-clips={args.max_clips}, pass --max-clips {remake_count} "
            f"explicitly（未发起任何 Ark 调用，零成本）。"
        )

    api_key = None
    if args.confirm and remake_count > 0:
        api_key = config.get_ark_api_key()
        if not api_key:
            raise WorkerError(
                "无法获取 Ark API key：hakko-secret 探测的所有密钥名均不可用。"
                "如实报告：真实调用不可能发生，未伪造任何生成结果。"
            )

    clips_generated = 0
    cost_tokens = 0.0
    clip_paths: dict[int, Path] = {}

    for i, seg in enumerate(segments):
        if seg["action"] == "drop":
            # drop 段没有产出，直接标 done，不占用生成配额，也不参与后续拼装。
            beats[i]["status"] = "done"
            report(base_url, secret, job_id, claim_token, "running", beats=beats)
            continue

        if seg["action"] == "reuse":
            # reuse 段本身不需要在这个阶段单独生成——实际切原片发生在 assemble_t2()
            # 拼装阶段（复用 assemble.py 的 reuse_cut 处理），这里只标记就绪。
            beats[i]["status"] = "done"
            report(base_url, secret, job_id, claim_token, "running", beats=beats)
            continue

        # remake 段
        beats[i]["status"] = "generating"
        duration = seg["end"] - seg["start"]
        out_path = clips_dir / f"seg_{i:02d}_remake.mp4"

        if args.confirm:
            if clips_generated >= args.max_clips:
                raise WorkerError(
                    f"成本护栏拦截：本次运行已生成 {clips_generated} 条 clip，"
                    f"达到 --max-clips={args.max_clips} 上限，拒绝生成第 {i} 段 "
                    f"remake，未发起该段生成请求。"
                )
            duration_int = int(round(min(max(duration, 3), 10)))
            prompt = f"{brief['seedance2Prompt']} {seg['description']}"
            task_id = gen_clip_real(api_key, prompt, ratio, duration_int, out_path)
            clips_generated += 1
            cost_tokens += GEN_WIDTH * GEN_HEIGHT * GEN_FPS * duration_int / 1024
            actual_seconds = trim_clip_to_storyboard_duration(out_path, duration)
            beats[i].update(status="done", taskId=task_id, videoUrl=str(out_path),
                             seconds=actual_seconds)
        else:
            gen_clip_dryrun("remake", i, duration, out_path, canvas)
            beats[i].update(status="done", videoUrl=str(out_path))

        clip_paths[i] = out_path
        report(base_url, secret, job_id, claim_token, "running", beats=beats)

    report(base_url, secret, job_id, claim_token, "assembling", beats=beats)
    final_path = assemble_t2(segments, source_path, clip_paths, out_root, canvas)

    total_sec = sum(seg["end"] - seg["start"] for seg in segments if seg["action"] != "drop")

    qc_report = qc_scan(final_path, out_root / ".qc_scratch")
    qc_report["durationSec"] = round(total_sec, 2)
    report(base_url, secret, job_id, claim_token, "qc", beats=beats,
           qcReport=qc_report, costTokens=round(cost_tokens, 2))

    data = final_path.read_bytes()
    file_url = upload_video(base_url, secret, job_id, claim_token, data)

    report(base_url, secret, job_id, claim_token, "succeeded", beats=beats, qcReport=qc_report,
           costTokens=round(cost_tokens, 2), outputUrl=file_url)

    print(f"\njob {job_id} succeeded (tier=t2)")
    print(f"  outputUrl    = {file_url}")
    print(f"  assembled mp4 = {final_path}")
    print(f"  qcReport.pass = {qc_report['pass']}")
    print(f"  costTokens    = {round(cost_tokens, 2)}")


def run_t1(job: dict, args: argparse.Namespace, secret: str, base_url: str,
           out_root: Path, beats: list[dict]) -> None:
    """T1 @视频锚定重生成路径：逐 beat 生成（同 T0.5 的 storyboard 循环），但每个
    beat 的生成请求额外附带一段从 job.refs[0].url 竞品源视频按该 beat 在 storyboard
    时间轴上的累计窗口切出的参照片段（@视频引用）——模型模仿参照片段的运镜/画风/
    节奏，输出零竞品像素。

    跟 run_t0_5 的唯一结构性差异：多了「下载源视频 -> 按 beat 窗口切参照片段 ->
    把参照片段（dry-run 是路径预览，--confirm 是 URL）带进生成请求」这一段；
    生成/拼装/QC/上传/report 流程原样复用 run_t0_5 已验证过的那一套。"""
    job_id = job["id"]
    claim_token = job.get("claimToken")
    if not claim_token:
        raise WorkerError(f"claim 响应缺少 claimToken 字段: {job}")
    brief = job["brief"]
    storyboard = brief["storyboard"]
    ratio = brief.get("ratio") or DEFAULT_RATIO
    canvas = resolve_canvas(ratio)

    refs = job.get("refs") or []
    if not refs or not refs[0].get("url"):
        raise WorkerError("T1 job 缺少 refs[0].url（竞品参照源视频公网地址）")
    ref_url = refs[0]["url"]

    beats.extend(
        {"index": i, "role": b["role"], "seconds": b["seconds"], "status": "pending"}
        for i, b in enumerate(storyboard)
    )

    clips_dir = out_root / "clips"
    refs_dir = out_root / "refs"
    clips_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)

    report(base_url, secret, job_id, claim_token, "running", beats=beats)

    source_path = out_root / "source_ref.mp4"
    download_ref_video(ref_url, source_path)
    source_duration = get_duration_sec(ffprobe_json(source_path))

    # 成本护栏 pre-check：跟 T0.5/T2 一样，先数一遍 storyboard 里总共有多少 beat
    # 需要生成，超过 --max-clips 直接零成本失败退出（未发起任何 Ark 调用之前）。
    if args.confirm:
        beats_needing_gen = len(storyboard)
        if beats_needing_gen > args.max_clips:
            raise WorkerError(
                f"成本护栏拦截：storyboard has {beats_needing_gen} beats exceeding "
                f"--max-clips={args.max_clips}, pass --max-clips {beats_needing_gen} "
                f"explicitly（未发起任何 Ark 调用，零成本）。"
            )

    api_key = None
    if args.confirm:
        api_key = config.get_ark_api_key()
        if not api_key:
            raise WorkerError(
                "无法获取 Ark API key：hakko-secret 探测的所有密钥名均不可用。"
                "如实报告：真实调用不可能发生，未伪造任何生成结果。"
            )

    clips_generated = 0
    cost_tokens = 0.0
    clip_paths: dict[int, Path] = {}
    cum = 0.0

    for i, b in enumerate(storyboard):
        beats[i]["status"] = "generating"
        beat_seconds = float(b["seconds"])
        cum_start, cum_end = cum, cum + beat_seconds
        cum = cum_end

        ref_start, ref_end = compute_ref_window(cum_start, cum_end, source_duration)
        ref_clip_path = cut_beat_ref_clip(source_path, ref_start, ref_end, refs_dir, i)
        beats[i]["refClip"] = str(ref_clip_path)

        out_path = clips_dir / f"beat_{i:02d}_{b['role']}.mp4"
        prompt = f"{brief['seedance2Prompt']} {b['description']}"

        if args.confirm:
            if clips_generated >= args.max_clips:
                raise WorkerError(
                    f"成本护栏拦截：本次运行已生成 {clips_generated} 条 clip，"
                    f"达到 --max-clips={args.max_clips} 上限，拒绝生成第 {i} 条 "
                    f"beat（role={b['role']}），未发起该条 clip 的生成请求。"
                )
            duration_int = int(round(min(max(beat_seconds, 3), 10)))
            task_id = gen_clip_real_t1(api_key, prompt, ratio, duration_int, ref_url, out_path)
            clips_generated += 1
            cost_tokens += GEN_WIDTH * GEN_HEIGHT * GEN_FPS * duration_int / 1024
            actual_seconds = trim_clip_to_storyboard_duration(out_path, beat_seconds)
            beats[i].update(status="done", taskId=task_id, videoUrl=str(out_path),
                             seconds=actual_seconds)
        else:
            gen_clip_dryrun(b["role"], i, beat_seconds, out_path, canvas)
            payload = build_t1_ref_payload_preview(prompt, ratio, beat_seconds, ref_clip_path)
            payload_path = refs_dir / f"beat_{i:02d}_payload.json"
            payload_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            beats[i]["payloadDump"] = str(payload_path)
            beats[i].update(status="done", videoUrl=str(out_path))

        clip_paths[i] = out_path
        report(base_url, secret, job_id, claim_token, "running", beats=beats)

    report(base_url, secret, job_id, claim_token, "assembling", beats=beats)
    final_path = assemble_final(beats, clip_paths, out_root, canvas)

    qc_report = qc_scan(final_path, out_root / ".qc_scratch")
    qc_report["durationSec"] = round(total_duration_from_beats(beats, final_path), 2)
    report(base_url, secret, job_id, claim_token, "qc", beats=beats,
           qcReport=qc_report, costTokens=round(cost_tokens, 2))

    data = final_path.read_bytes()
    file_url = upload_video(base_url, secret, job_id, claim_token, data)

    report(base_url, secret, job_id, claim_token, "succeeded", beats=beats, qcReport=qc_report,
           costTokens=round(cost_tokens, 2), outputUrl=file_url)

    print(f"\njob {job_id} succeeded (tier=t1)")
    print(f"  outputUrl    = {file_url}")
    print(f"  assembled mp4 = {final_path}")
    print(f"  qcReport.pass = {qc_report['pass']}")
    print(f"  costTokens    = {round(cost_tokens, 2)}")


def run_worker(args: argparse.Namespace, secret: str) -> None:
    base_url = args.base_url.rstrip("/")

    try:
        job = claim_job(base_url, secret, args.job_id)
    except WorkerError as e:
        sys.exit(f"claim 失败: {e}")

    if job is None:
        print("adex 当前没有可用的 RemixJob（job=null），退出。")
        sys.exit(0)

    job_id = job["id"]
    claim_token = job.get("claimToken")
    tier = job.get("tier") or "t0_5"
    out_root = args.out_dir / job_id

    beats: list[dict] = []
    try:
        if tier == "t0_5":
            run_t0_5(job, args, secret, base_url, out_root, beats)
        elif tier == "t2":
            run_t2(job, args, secret, base_url, out_root, beats)
        elif tier == "t1":
            run_t1(job, args, secret, base_url, out_root, beats)
        else:
            raise WorkerError(f"未知 job.tier: {tier!r}（应为 t0_5/t1/t2 之一）")
    except StaleClaimError as e:
        # claim 已被别的 worker 抢占：这个 job 不再属于我们，report/upload 只会一直
        # 收到同样的 409，再报没有意义——记日志放弃，不重试。
        print(f"job {job_id} 已被抢占（stale claim），放弃该任务，不重试: {e}", file=sys.stderr)
        sys.exit(1)
    except SystemExit as e:
        # assemble.py 的部分辅助函数（load_gen_clip_map/normalize_one/concat_filter 等）
        # 在出错时直接 sys.exit(msg)，不是抛普通异常——按脚本原有约定不改它们的行为，
        # 这里统一捕获 SystemExit 当成失败处理，确保任何阶段的失败都能上报 status=failed。
        msg = e.code if isinstance(e.code, str) else f"sys.exit({e.code!r})"
        _fail(base_url, secret, job_id, claim_token, beats, str(msg))
    except Exception as e:
        _fail(base_url, secret, job_id, claim_token, beats, str(e))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="remix job 执行客户端（adex worker）")
    ap.add_argument("--base-url", type=str, required=True,
                     help="adex control-plane 的 base URL（必须包含部署时的 basePath，"
                          "例如 https://host/adex，不能只给域名根）")
    ap.add_argument("--secret", type=str, default=None,
                     help="HMAC 签名密钥；不传则读环境变量 WORKER_WEBHOOK_SECRET")
    ap.add_argument("--job-id", type=str, default=None, help="认领指定的 job（可选）")
    ap.add_argument("--confirm", action="store_true", default=False,
                     help="真实 Seedance 调用（默认 dry-run，零成本零网络调用）")
    ap.add_argument("--out-dir", type=Path, default=Path("out/worker"), help="产物输出目录")
    ap.add_argument("--max-clips", type=int, default=config.SEEDANCE_MAX_CLIPS_PER_RUN,
                     help="单次运行允许真实生成的最大 clip 条数（成本护栏，仅 --confirm 下生效）")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    secret = args.secret or os.environ.get("WORKER_WEBHOOK_SECRET")
    if not secret:
        sys.exit("缺少 secret：需要 --secret 或环境变量 WORKER_WEBHOOK_SECRET，二者都未提供。")
    # worker.py 无论 dry-run 还是 --confirm 都要用 ffmpeg（占位生成/裁剪/拼装）+
    # OCR/whisper（QC 品牌预检），实际要用之前显式严格校验（import config 本身
    # 不再退出）。
    config.validate_strict()
    run_worker(args, secret)


if __name__ == "__main__":
    main()
