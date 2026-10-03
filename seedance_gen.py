#!/usr/bin/env python3
"""
seedance_gen.py —— 火山方舟 Ark（doubao-seedance-2-0）视频生成客户端。

两种输入模式：

    # brief 模式：从 remix_brief.py 产出的 briefs/<stem>.json 取 seedance_prompt
    # 和 sample_shots，--shot 选一镜（裁剪该镜时段对应的 prompt 文本）或 all-samples
    # （sample_shots 里全部镜，仍受 MAX_CLIPS 门禁）。生成全片需要显式 --full。
    python3 seedance_gen.py --brief out/<批次>/briefs/<x>.json --shot <sample序号|all-samples> [--dry-run] [--confirm]

    # 自由 prompt 模式：直接给文本 + 参数
    python3 seedance_gen.py --prompt-text "..." [--ratio 9:16] [--duration 8] [--ref-image <path或url> ...] [--dry-run] [--confirm]

硬性红线：
- 默认 dry-run（不传 --confirm 就绝不发起真实 API 调用），dry-run 打印完整请求体 + 预估成本。
- 成本护栏：单次运行生成条数 > config.SEEDANCE_MAX_CLIPS_PER_RUN 或任一 clip 时长 >
  config.SEEDANCE_MAX_DURATION_SEC，直接拒绝执行（--dry-run 下只警告不拦截，方便预览；
  --confirm 真实调用下强制拦截）。
- API key 运行时经 config.get_ark_api_key()（hakko-secret）获取，零打印零落盘。

参考图/参考视频：Ark contents/generations/tasks 请求体里 image_url / video_url 字段吃的是
url（本客户端未在文档/adex ts 客户端中发现 base64/binary 上传字段）。本地文件路径会被
拒绝并明确报错，不静默忽略、不假装能用（见 build_reference_content()）。

真实调用路径：创建任务 -> 轮询（10s 间隔，10min 超时）-> 下载 mp4 到
out/gen/<时间戳>_<shot>.mp4 -> 自动跑 qc.py 品牌预检 -> 打印产物路径/耗时/任务id ->
成本记账写入 out/gen/spend_log.csv。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import config

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_TASKS_PATH = "/contents/generations/tasks"
ARK_MODEL = "doubao-seedance-2-0-260128"

VALID_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}

POLL_INTERVAL_SEC = 10
POLL_TIMEOUT_SEC = 600

GEN_OUT_DIR = Path("out/gen")
SPEND_LOG_PATH = GEN_OUT_DIR / "spend_log.csv"
SPEND_LOG_FIELDS = [
    "timestamp", "shot", "duration_sec", "ratio", "generate_audio",
    "n_ref_images", "n_ref_videos", "n_ref_audios", "task_id", "est_cost_usd",
]

# 每秒成本估算（token 口径，2026-07-10 用真实任务校准）：
# Ark 视频生成按 video token 计费，token ≈ 宽×高×fps×时长/1024。
# 实测任务 cgt-20260710153030-s5t5x：5s @720p(720×1280) 9:16 24fps
# → usage.completion_tokens = 108,900，即 720p 下 ≈ 21,780 tokens/s，
# 与公式吻合（720×1280×24×5/1024 = 108,000）。
# 单价：doubao-seedance-2-0 的 ¥/百万token 官方页为动态渲染抓不到，先按
# Seedance 1.0 pro 公开价 ¥15/M 占位（PRICE_PER_M_TOKENS_CNY），
# **拿到该任务的真实账单后把这个数改成 实付¥×1e6/108900**。
TOKENS_PER_SEC_720P = 21_780
PRICE_PER_M_TOKENS_CNY = 15.0   # ← 占位价，待控制台账单校准
CNY_PER_USD = 7.2
ESTIMATED_COST_PER_SEC_USD = round(
    TOKENS_PER_SEC_720P / 1e6 * PRICE_PER_M_TOKENS_CNY / CNY_PER_USD, 4
)  # ¥15/M 下 ≈ $0.045/s，5s ≈ $0.23

TIME_SEGMENT_LABEL_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–—~]\s*(\d+(?:\.\d+)?)\s*秒\s*[，,]"
)
SHOT_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–—~]\s*(\d+(?:\.\d+)?)\s*s", re.IGNORECASE
)


class GuardrailError(Exception):
    """成本护栏拦截，非法参数等——直接拒绝执行，不静默降级。"""


class RefMaterialError(Exception):
    """参考素材（图/视频/音频）不满足 Ark 要求（如本地文件路径）时抛出。"""


# ---------------------------------------------------------------------------
# brief 解析 + 分时段裁剪
# ---------------------------------------------------------------------------

def load_brief(path: Path) -> dict:
    if not path.exists():
        raise GuardrailError(f"brief 文件不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise GuardrailError(f"brief JSON 解析失败: {path}: {e}")


def _parse_shot_range(time_range: str) -> tuple[float, float]:
    """解析 sample_shots[].time_range（如 "0-3s" "8-12s"）为 (start, end) 秒。"""
    m = SHOT_RANGE_RE.search(time_range)
    if not m:
        raise GuardrailError(f"无法解析 time_range: {time_range!r}（期望形如 \"0-3s\"）")
    return float(m.group(1)), float(m.group(2))


def _segments_from_section_body(body: str) -> list[tuple[float, float, str]]:
    """从任意一段文本里切出「N-M秒，...」模式的 (start, end, text) 列表（不判断来源小节）。"""
    matches = list(TIME_SEGMENT_LABEL_RE.finditer(body))
    out = []
    for i, m in enumerate(matches):
        start, end = float(m.group(1)), float(m.group(2))
        text_start = m.end()
        text_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[text_start:text_end].strip().strip("；;").strip("。").strip()
        if text:
            out.append((start, end, text))
    return out


def _extract_per_segment_texts_strict(seedance_prompt: str) -> list[tuple[float, float, str]] | None:
    """优先路径：只认字面小节标题「分时段描述：」。找不到这个小节返回 None（不抛异常，
    留给调用方决定是否走 fallback）。"""
    sections = seedance_prompt.split("\n\n")
    target_section = None
    for sec in sections:
        if sec.strip().startswith("分时段描述"):
            target_section = sec
            break
    if target_section is None:
        return None
    # 去掉 "分时段描述：" 前缀
    body = target_section.split("：", 1)[-1] if "：" in target_section else target_section
    return _segments_from_section_body(body) or None


def _extract_per_segment_texts_fallback(seedance_prompt: str) -> list[tuple[float, float, str]]:
    """容错路径：当 prompt 里没有独立的「分时段描述：」小节标题时（例如 LLM 把分时段
    内容揉进了「动作与运动描述」或其他小节），全文按「\\n\\n」切成若干小节，逐节扫描
    「N-M秒，」模式，把落在同一时间区间（0.5s 容差）的文本从各小节聚合拼接起来，
    按 start 排序返回。真实失败案例：一次批跑产出的 brief
    的 seedance_prompt 把分时段描述写进了「动作与运动描述：」小节，没有单独的
    「分时段描述：」标题，靠这个 fallback 才能裁出 --shot 需要的子 prompt。
    """
    sections = seedance_prompt.split("\n\n")
    # (start, end, [text, ...])，按插入顺序聚合，最后再排序
    buckets: list[list] = []  # each: [start, end, [texts]]

    def find_bucket(start: float, end: float):
        for b in buckets:
            if abs(b[0] - start) < 0.5 and abs(b[1] - end) < 0.5:
                return b
        return None

    for sec in sections:
        for start, end, text in _segments_from_section_body(sec):
            bucket = find_bucket(start, end)
            if bucket is None:
                buckets.append([start, end, [text]])
            elif text not in bucket[2]:
                bucket[2].append(text)

    if not buckets:
        raise GuardrailError(
            "seedance_prompt 中既没有「分时段描述：」小节，全文扫描也未找到任何 "
            "「N-M秒，」形式的分时段标记，无法按镜裁剪 prompt"
        )

    buckets.sort(key=lambda b: b[0])
    return [(b[0], b[1], "；".join(b[2])) for b in buckets]


def _extract_per_segment_texts(seedance_prompt: str) -> list[tuple[float, float, str]]:
    """从 seedance_prompt 里切出 (start, end, text) 分时段列表。

    优先用「分时段描述：」这个字面小节标题（remix_brief.md 模板的硬性约束）；
    找不到这个小节时（历史 brief 或手工构造的 brief 可能没有），fallback 到全文
    扫描「N-M秒，」模式、跨小节聚合同一时间区间的文本，见
    `_extract_per_segment_texts_fallback`。
    """
    strict = _extract_per_segment_texts_strict(seedance_prompt)
    if strict is not None:
        return strict
    return _extract_per_segment_texts_fallback(seedance_prompt)


def extract_shot_prompt(brief: dict, time_range: str) -> str:
    """裁剪 seedance_prompt，取指定 time_range 对应的分时段文本作为该 clip 的 prompt。"""
    seedance_prompt = brief.get("seedance_prompt", "")
    if not seedance_prompt:
        raise GuardrailError("brief 中缺少 seedance_prompt 字段")

    want_start, want_end = _parse_shot_range(time_range)
    segments = _extract_per_segment_texts(seedance_prompt)

    for start, end, text in segments:
        # 允许小数点误差（sample_shots 用整数秒，seedance_prompt 分时段可能是小数，
        # 如 "12-15.9秒"），按 0.5s 容差匹配起止点。
        if abs(start - want_start) < 0.5 and abs(end - want_end) < 0.5:
            if not text:
                raise GuardrailError(f"time_range={time_range} 命中的分时段文本为空")
            return text

    available = ", ".join(f"{s:g}-{e:g}秒" for s, e, _ in segments)
    raise GuardrailError(
        f"「分时段描述：」中未找到与 time_range={time_range!r} 匹配的分段"
        f"（容差 0.5s）。可用分段: {available}"
    )


def resolve_shots(brief: dict, shot_arg: str, full: bool) -> list[dict]:
    """返回要生成的镜头列表：[{time_range, prompt, duration_sec, note}, ...]

    sample-first 门禁：默认只允许 sample_shots 里列出的镜；--full 才允许全片，
    且仍受 MAX_CLIPS 限制（全片镜数超限直接拒绝，不做"只生成前 N 个"的静默截断）。
    """
    sample_shots = brief.get("sample_shots") or []
    if not sample_shots:
        raise GuardrailError("brief 中缺少 sample_shots，无法用 --shot 选镜")

    if full:
        segments = _extract_per_segment_texts(brief.get("seedance_prompt", ""))
        shots_pool = [{"time_range": f"{s:g}-{e:g}s", "note": "(--full 全片镜)"} for s, e, _ in segments]
    else:
        shots_pool = sample_shots

    if shot_arg == "all-samples":
        chosen = shots_pool
    else:
        chosen = None
        for s in shots_pool:
            if s.get("time_range") == shot_arg:
                chosen = [s]
                break
        if chosen is None:
            available = ", ".join(s.get("time_range", "?") for s in shots_pool)
            pool_label = "全片分段" if full else "sample_shots"
            raise GuardrailError(
                f"--shot {shot_arg!r} 不在 {pool_label} 里（可用: {available}）。"
                + ("" if full else " 若要生成 sample_shots 之外的镜，需加 --full。")
            )

    out = []
    for s in chosen:
        tr = s["time_range"]
        start, end = _parse_shot_range(tr)
        prompt_text = extract_shot_prompt(brief, tr)
        out.append({
            "time_range": tr,
            "prompt": prompt_text,
            "duration_sec": end - start,
            "note": s.get("note", ""),
        })
    return out


# ---------------------------------------------------------------------------
# 参考素材（图/视频/音频）
# ---------------------------------------------------------------------------

def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def build_reference_content(kind: str, refs: list[str]) -> list[dict]:
    """把 --ref-image/--ref-video/--ref-audio 列表转成 Ark content items。

    Ark contents/generations/tasks 请求体（对齐 adex ts 客户端
    src/lib/platforms/seedance2.ts 的 ContentItem 定义）里 image_url/video_url/
    audio_url 字段的值是 {"url": "..."}——只认得公网可访问 URL，请求体里没有
    base64/binary 字段。本地文件路径直接拒绝，不静默忽略、不假装能用。
    """
    type_map = {"image": "image_url", "video": "video_url", "audio": "audio_url"}
    role_map = {
        "image": "reference_image", "video": "reference_video", "audio": "reference_audio",
    }
    items = []
    for ref in refs:
        if not _is_url(ref):
            raise RefMaterialError(
                f"参考{kind}「{ref}」不是 URL：Ark contents/generations/tasks 请求体的 "
                f"{type_map[kind]} 字段只接受公网可访问的 url（未发现 base64/binary 上传字段，"
                f"已核对 adex ts 客户端 src/lib/platforms/seedance2.ts 的 ContentItem 定义），"
                f"需先把本地文件上传到可公网访问的存储（如 GCS/OSS），再传其 URL。"
            )
        items.append({
            "type": type_map[kind],
            type_map[kind]: {"url": ref},
            "role": role_map[kind],
        })
    return items


# ---------------------------------------------------------------------------
# 请求体组装 + 成本护栏
# ---------------------------------------------------------------------------

def build_request_body(
    prompt: str,
    ratio: str,
    duration_sec: float,
    generate_audio: bool,
    ref_images: list[str],
    ref_videos: list[str],
    ref_audios: list[str],
) -> dict:
    content = [{"type": "text", "text": prompt}]
    content += build_reference_content("image", ref_images)
    content += build_reference_content("video", ref_videos)
    content += build_reference_content("audio", ref_audios)

    return {
        "model": ARK_MODEL,
        "content": content,
        "generate_audio": generate_audio,
        "ratio": ratio,
        # Ark 只接受整数秒（实测 5.0 浮点返回 InvalidParameter 400）
        "duration": int(round(duration_sec)),
        "watermark": False,
    }


def estimate_cost_usd(duration_sec: float) -> float:
    return round(duration_sec * ESTIMATED_COST_PER_SEC_USD, 2)


def check_cost_guardrails(clips: list[dict], enforce: bool) -> None:
    """clips: [{"duration_sec": float, ...}, ...]。enforce=True 时超限直接抛异常拒绝执行。"""
    problems = []
    if len(clips) > config.SEEDANCE_MAX_CLIPS_PER_RUN:
        problems.append(
            f"本次请求生成 {len(clips)} 条 clip，超过 config.SEEDANCE_MAX_CLIPS_PER_RUN="
            f"{config.SEEDANCE_MAX_CLIPS_PER_RUN}"
        )
    for c in clips:
        if c["duration_sec"] > config.SEEDANCE_MAX_DURATION_SEC:
            problems.append(
                f"clip [{c.get('time_range', c.get('shot', '?'))}] 时长 "
                f"{c['duration_sec']:.1f}s 超过 config.SEEDANCE_MAX_DURATION_SEC="
                f"{config.SEEDANCE_MAX_DURATION_SEC}"
            )
    if problems:
        msg = "成本护栏拦截:\n" + "\n".join(f"  - {p}" for p in problems)
        if enforce:
            raise GuardrailError(msg)
        else:
            print(f"[WARN][dry-run 下仅警告，--confirm 真实调用时会被拦截]\n{msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Ark HTTP 调用
# ---------------------------------------------------------------------------

def _ark_request(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    url = f"{ARK_BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ark API 错误 {e.code}: {err_body}") from e


def create_task(api_key: str, body: dict) -> dict:
    return _ark_request("POST", ARK_TASKS_PATH, api_key, body)


def get_task(api_key: str, task_id: str) -> dict:
    return _ark_request("GET", f"{ARK_TASKS_PATH}/{task_id}", api_key)


def poll_task(api_key: str, task_id: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while True:
        task = get_task(api_key, task_id)
        status = task.get("status")
        if status in ("succeeded", "failed"):
            return task
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"轮询超时（{POLL_TIMEOUT_SEC}s）：任务 {task_id} 仍处于 {status} 状态"
            )
        print(f"    轮询中... status={status}（{POLL_INTERVAL_SEC}s 后重试）")
        time.sleep(POLL_INTERVAL_SEC)


def download_video(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)


def run_qc(video_path: Path) -> int:
    """跑 qc.py 品牌预检，返回其退出码（不拦截 seedance_gen.py 本身的退出码，
    只是把 QC 结果打印出来，QC FAIL 由使用者决定后续处理）。"""
    cp = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "qc.py"), str(video_path)],
    )
    return cp.returncode


def append_spend_log(row: dict) -> None:
    GEN_OUT_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not SPEND_LOG_PATH.exists()
    with open(SPEND_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SPEND_LOG_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------------------
# 单条 clip 的 dry-run / 真实执行
# ---------------------------------------------------------------------------

def print_dry_run(shot_label: str, body: dict, est_cost: float) -> None:
    print(f"\n{'=' * 70}")
    print(f"[DRY-RUN] shot={shot_label}")
    print(f"{'=' * 70}")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    print(f"\n预估成本: ${est_cost:.2f} USD（粗估，见 ESTIMATED_COST_PER_SEC_USD 注释，非官方核价）")
    print("未发起真实 API 调用（默认 dry-run；需 --confirm 才会真实计费执行）。")


def execute_real(api_key: str, shot_label: str, body: dict, est_cost: float) -> None:
    print(f"\n{'=' * 70}")
    print(f"[REAL] shot={shot_label} — 创建任务中...")
    print(f"{'=' * 70}")
    t0 = time.monotonic()

    task = create_task(api_key, body)
    task_id = task.get("id")
    if not task_id:
        raise RuntimeError(f"创建任务响应中没有 id 字段: {task}")
    print(f"  任务已创建: id={task_id}")

    final_task = poll_task(api_key, task_id)
    elapsed = time.monotonic() - t0

    if final_task.get("status") != "succeeded":
        err = final_task.get("error", {})
        raise RuntimeError(
            f"任务 {task_id} 未成功完成: status={final_task.get('status')} "
            f"error={err.get('code')}: {err.get('message')}"
        )

    # 实测 Ark 响应把结果放在 content.video_url（adex ts 客户端假设的 output.video_url
    # 在 doubao-seedance-2-0-260128 实际响应里不存在），两个位置都兼容。
    video_url = (final_task.get("content") or {}).get("video_url") or (
        final_task.get("output") or {}
    ).get("video_url")
    if not video_url:
        raise RuntimeError(f"任务 {task_id} succeeded 但响应中没有 content/output.video_url: {final_task}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_shot = re.sub(r"[^\w.-]", "_", shot_label)
    dest = GEN_OUT_DIR / f"{ts}_{safe_shot}.mp4"
    print(f"  下载中: {video_url} -> {dest}")
    download_video(video_url, dest)

    print(f"  跑 qc.py 品牌预检...")
    qc_code = run_qc(dest)

    print(f"\n产物: {dest}")
    print(f"任务 id: {task_id}")
    print(f"实际耗时: {elapsed:.1f}s")
    print(f"qc.py 退出码: {qc_code}（0=PASS，非0=FAIL，详见上方 qc.py 输出）")

    n_image = sum(1 for c in body["content"] if c["type"] == "image_url")
    n_video = sum(1 for c in body["content"] if c["type"] == "video_url")
    n_audio = sum(1 for c in body["content"] if c["type"] == "audio_url")
    append_spend_log({
        "timestamp": ts,
        "shot": shot_label,
        "duration_sec": body["duration"],
        "ratio": body["ratio"],
        "generate_audio": body["generate_audio"],
        "n_ref_images": n_image,
        "n_ref_videos": n_video,
        "n_ref_audios": n_audio,
        "task_id": task_id,
        "est_cost_usd": est_cost,
    })
    print(f"成本记账已写入: {SPEND_LOG_PATH}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Seedance2 (doubao-seedance-2-0) 视频生成客户端")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--brief", type=Path, help="remix_brief.py 产出的 brief JSON 路径")
    mode.add_argument("--prompt-text", type=str, help="自由 prompt 文本（不走 brief 模式）")

    ap.add_argument("--shot", type=str, default=None,
                     help="brief 模式：sample_shots 里的 time_range（如 0-3s）或 all-samples")
    ap.add_argument("--full", action="store_true",
                     help="brief 模式：允许生成 sample_shots 之外的全片镜（仍受 MAX_CLIPS 门禁）")

    ap.add_argument("--ratio", type=str, default="9:16", choices=sorted(VALID_RATIOS))
    ap.add_argument("--duration", type=float, default=5.0, help="--prompt-text 模式下的时长（秒）")
    ap.add_argument("--generate-audio", action="store_true", default=False)
    ap.add_argument("--ref-image", action="append", default=[], dest="ref_images",
                     help="参考图（本地路径或 URL，可重复传）")
    ap.add_argument("--ref-video", action="append", default=[], dest="ref_videos",
                     help="参考视频（本地路径或 URL，可重复传）")
    ap.add_argument("--ref-audio", action="append", default=[], dest="ref_audios",
                     help="参考音频（本地路径或 URL，可重复传）")

    ap.add_argument("--dry-run", action="store_true", default=True,
                     help="（默认开启）只打印请求体+预估成本，不发起真实调用")
    ap.add_argument("--confirm", action="store_true", default=False,
                     help="显式确认真实调用（会真实计费）；不传则始终 dry-run")
    args = ap.parse_args()

    # --confirm 才是真正的开关；--dry-run 只是显式声明默认行为，不做实际拦截依据。
    do_real = args.confirm

    try:
        if args.brief:
            if not args.shot:
                raise GuardrailError("--brief 模式需要 --shot <time_range|all-samples>")
            brief = load_brief(args.brief)
            shots = resolve_shots(brief, args.shot, args.full)
            duration_sec_default = None  # 由每个 shot 自己的时长决定
            ratio = args.ratio
            clips = []
            for s in shots:
                clips.append({
                    "time_range": s["time_range"],
                    "shot": s["time_range"],
                    "prompt": s["prompt"],
                    "duration_sec": s["duration_sec"],
                })
        else:
            clips = [{
                "time_range": "prompt-text",
                "shot": "prompt-text",
                "prompt": args.prompt_text,
                "duration_sec": args.duration,
            }]
            ratio = args.ratio

        if args.ratio not in VALID_RATIOS:
            raise GuardrailError(f"--ratio {args.ratio!r} 不在支持范围 {sorted(VALID_RATIOS)}")

        # 成本护栏：dry-run 下只警告（方便预览超限场景），真实调用下强制拦截。
        check_cost_guardrails(clips, enforce=do_real)

        api_key = None
        if do_real:
            api_key = config.get_ark_api_key()
            if not api_key:
                sys.exit(
                    "无法获取 Ark API key：hakko-secret 探测的所有密钥名均不可用。"
                    "如实报告：真实调用不可能发生，未伪造任何响应。"
                )

        for clip in clips:
            body = build_request_body(
                prompt=clip["prompt"],
                ratio=ratio,
                duration_sec=clip["duration_sec"],
                generate_audio=args.generate_audio,
                ref_images=args.ref_images,
                ref_videos=args.ref_videos,
                ref_audios=args.ref_audios,
            )
            est_cost = estimate_cost_usd(clip["duration_sec"])

            if do_real:
                execute_real(api_key, clip["shot"], body, est_cost)
            else:
                print_dry_run(clip["shot"], body, est_cost)

        if not do_real:
            print(f"\n共 {len(clips)} 条 clip，dry-run 完成，exit 0。加 --confirm 才会真实调用。")

    except (GuardrailError, RefMaterialError) as e:
        sys.exit(f"拒绝执行: {e}")
    except RuntimeError as e:
        sys.exit(f"执行失败: {e}")


if __name__ == "__main__":
    main()
