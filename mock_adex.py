#!/usr/bin/env python3
"""
mock_adex.py —— worker.py 的本地离线验收服务器（stdlib http.server 实现，零第三方依赖）。

复刻 adex control-plane 的 3 个 worker 接口：
    POST /api/worker/remix-jobs/claim
    POST /api/worker/remix-jobs/report
    POST /api/worker/remix-jobs/upload?jobId=<id>

用固定 secret 'test' 校验 HMAC 签名（claim/report 的基串是 f"{timestamp}:{rawBody}"，
upload 的基串是 f"{timestamp}:{jobId}:{claimToken}:{content_sha256_hex}"，跟
worker.py 的签名逻辑对齐）；签名缺失/错误一律 401 拒绝。

claim 围栏：claim 响应的 job 带 claimToken（uuid4）+ attempt（自增），report 请求体
必须带匹配的 claimToken，upload 必须带匹配的 x-adex-claim-token header——不匹配
（job 已被 re-claim）一律 409 {"error":"stale claim"}。upload 成功后对象 key 是
remix/{orgId}/{jobId}/v{attempt}.mp4。额外开了一个仅本地测试用的
POST /debug/reclaim（不需要签名），用来在验收时模拟"job 被另一个 worker 抢占"，
不是 adex 真实契约的一部分。

内存里持有一个假 RemixJob：brief.storyboard 恰好 3 镜（hook/3s、scene/5s、
end-card/2s），ratio 9:16。claim 第一次调用返回该 job，此后返回 {"job": null}。
report 记录收到的完整状态序列；upload 记录收到的字节数。收到 status=succeeded
的 report 时打印一份验收摘要到 stdout。

用法：
    python3 mock_adex.py 8765
    python3 mock_adex.py --port 8765
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SECRET = "test"

# T2 fixture 源视频合成用的 ffmpeg 路径——mock_adex.py 本体是零第三方依赖的 stdlib
# 脚本，这里额外 shell 出去调 ffmpeg 只用于 --tier t2 模式现场合成 fixture，不引入
# Python 包依赖。找不到就退化成裸命令名，留给 subprocess 报"找不到命令"的清晰错误。
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

FAKE_JOB = {
    "id": "job-" + uuid.uuid4().hex[:8],
    "orgId": "org-test",
    # job.tier 是 worker.py run_worker() 的路由分派字段（'t0_5'/'t1'/'t2'），
    # 这个默认 job 走 T0.5 逐 beat 路径，跟 T2 分段路由（见 FAKE_JOB_T2）区分开。
    "tier": "t0_5",
    "creativeId": "creative-test",
    "segmentPlan": None,
    "brief": {
        "sourceRef": "src-test.mp4",
        "borrowed": ["hook 结构"],
        "changed": ["产品卖点、CTA"],
        "hookText": "你还在手动配语音吗？",
        "storyboard": [
            {"role": "hook", "seconds": 3, "description": "手机屏幕特写展示来电界面"},
            {"role": "scene", "seconds": 5, "description": "分支剧情选择界面演示"},
            {"role": "end-card", "seconds": 2, "description": "产品 logo + CTA 收尾"},
        ],
        "seedance2Prompt": "竖版 9:16，现代科技感与二次元美学融合风格（mock 占位 prompt，用于本地验收）。",
        "ratio": "9:16",
        "durationSec": 10,
        "copy": {"headline": "占位标题", "primaryText": "占位正文", "cta": "立即体验"},
        "compliance": {},
    },
}


def build_fake_job_t1(port: int) -> dict:
    """构造 --tier t1 模式用的假 T1 job：storyboard 3 beats（复用默认 T0.5 job 的
    hook/scene/end-card 结构，累计窗口 0-3s/3-8s/8-10s，落在 12s fixture 源视频内，
    不会触发 compute_ref_window() 的"源视频短于窗口"兜底分支），refs[0].url 指向
    本进程自己起的 /fixtures/source.mp4 静态端点（源视频复用 t2 的合成源，见
    _build_fixture_source()，同一份 fixture 两个 tier 共用，不用另起一份）。没有
    segmentPlan 字段——T1 是逐 beat 生成 + @视频参照，不是 T2 的分段路由。"""
    return {
        "id": "job-t1-" + uuid.uuid4().hex[:8],
        "orgId": "org-test",
        "tier": "t1",
        "creativeId": "creative-test-t1",
        "segmentPlan": None,
        "refs": [{"url": f"http://127.0.0.1:{port}/fixtures/source.mp4", "kind": "video"}],
        "brief": {
            "sourceRef": "src-test-t1.mp4",
            "borrowed": ["运镜节奏"],
            "changed": ["产品卖点、CTA"],
            "hookText": "你还在手动配语音吗？",
            "storyboard": [
                {"role": "hook", "seconds": 3, "description": "手机屏幕特写展示来电界面"},
                {"role": "scene", "seconds": 5, "description": "分支剧情选择界面演示"},
                {"role": "end-card", "seconds": 2, "description": "产品 logo + CTA 收尾"},
            ],
            "seedance2Prompt": "竖版 9:16，现代科技感与二次元美学融合风格（mock 占位 prompt，用于本地验收）。",
            "ratio": "9:16",
            "durationSec": 10,
            "copy": {"headline": "占位标题", "primaryText": "占位正文", "cta": "立即体验"},
            "compliance": {},
        },
    }


def build_fake_job_t2(port: int) -> dict:
    """构造 --tier t2 模式用的假 T2 job：segmentPlan 覆盖 reuse/remake/drop 三种
    action，refs[0].url 指向本进程自己起的 /fixtures/source.mp4 静态端点（源视频
    在 _build_fixture_source() 里现场合成，见下）。"""
    return {
        "id": "job-t2-" + uuid.uuid4().hex[:8],
        "orgId": "org-test",
        "tier": "t2",
        "creativeId": "creative-test-t2",
        "segmentPlan": [
            {"start": 0, "end": 4, "action": "reuse", "reason": "真人 hook 段，画面声轨均干净，直接复用"},
            {"start": 4, "end": 7, "action": "remake",
             "description": "同款镜头结构，换成本产品卖点画面（mock 占位 description）"},
            {"start": 7, "end": 9, "action": "drop", "reason": "纯品牌下载页 UI，无独立结构价值"},
            {"start": 9, "end": 12, "action": "reuse", "reason": "结尾反转桥段，画面声轨均干净，直接复用"},
        ],
        "refs": [{"url": f"http://127.0.0.1:{port}/fixtures/source.mp4", "kind": "video"}],
        "brief": {
            "sourceRef": "src-test-t2.mp4",
            "borrowed": ["hook 结构", "结尾反转"],
            "changed": ["中段卖点画面"],
            "hookText": "你还在手动配语音吗？",
            "seedance2Prompt": "竖版 9:16，现代科技感与二次元美学融合风格（mock 占位 prompt，用于本地验收）。",
            "ratio": "9:16",
            "durationSec": 10,
            "copy": {"headline": "占位标题", "primaryText": "占位正文", "cta": "立即体验"},
            "compliance": {},
        },
    }


# 每段 1 秒的纯色 fixture 源视频调色板：12 段覆盖 segmentPlan 的 0-12s，每秒换一个
#肉眼可辨的颜色 + drawtext 秒数水印，方便验收时抽帧比对 reuse 段（0-4s/9-12s）
# 与 remake 段（4-7s，占位生成片会覆盖掉这段颜色，不再是源视频调色板）。
FIXTURE_COLORS = [
    "red", "orange", "yellow", "green", "cyan", "blue",
    "purple", "pink", "brown", "gray", "white", "black",
]


def _build_fixture_source() -> bytes:
    """现场用 ffmpeg lavfi 合成一段 ≥12s / 1080x1920 / 带音轨 / 每秒变色的源视频
    （12 段各 1s 纯色+数字水印，concat demuxer 拼接），返回完整 mp4 字节内容。
    只在 --tier t2 启动时调用一次，结果缓存进 _state["fixture_bytes"]。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        seg_paths = []
        for i, color in enumerate(FIXTURE_COLORS):
            seg_path = tmp_path / f"seg_{i:02d}.mp4"
            cmd = [
                FFMPEG_BIN, "-y",
                "-f", "lavfi", "-i", f"color=c={color}:s=1080x1920:d=1:r=30",
                "-f", "lavfi", "-i", f"sine=frequency={220 + i * 40}:duration=1",
                "-vf", f"drawtext=text='{i}s':fontcolor=white:fontsize=120:"
                       f"x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
                "-shortest", str(seg_path),
            ]
            cp = subprocess.run(cmd, capture_output=True, text=True)
            if cp.returncode != 0 or not seg_path.exists():
                raise RuntimeError(f"合成 fixture 源视频第 {i} 段失败: {cp.stderr[-2000:]}")
            seg_paths.append(seg_path)

        concat_list = tmp_path / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{p.name}'\n" for p in seg_paths), encoding="utf-8"
        )
        out_path = tmp_path / "source.mp4"
        cmd = [
            FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", str(out_path),
        ]
        cp = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_path)
        if cp.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"拼接 fixture 源视频失败: {cp.stderr[-2000:]}")
        return out_path.read_bytes()


# 进程级内存状态：仅供单次本地验收跑用，不做并发/持久化。
_state = {
    "claimed": False,
    "claim_token": None,  # 当前有效的 claimToken；report/upload 必须带这个值才通过围栏
    "attempt": 0,          # 自增计数，随每次（真实 claim 或 /debug/reclaim 模拟的抢占）刷新
    "reports": [],       # 收到的完整 report body 列表，按到达顺序
    "upload_bytes": 0,
    "fixture_bytes": None,  # --tier t2 模式下现场合成的 fixture 源视频字节内容
}


def _verify_signature(timestamp: str, base_body: str, signature: str) -> bool:
    if not timestamp or not signature:
        return False
    expected = "sha256=" + hmac.new(
        SECRET.encode("utf-8"), f"{timestamp}:{base_body}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class Handler(BaseHTTPRequestHandler):
    server_version = "MockAdex/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _unauthorized(self, detail: str) -> None:
        self._send_json(401, {"error": "unauthorized", "detail": detail})

    def do_GET(self) -> None:  # noqa: N802 (http.server 约定的方法名)
        # --tier t2 模式的 job.refs[0].url 指向这个端点：现场合成的 fixture 源视频
        # （见 _build_fixture_source()），T0.5 默认模式下不会用到，未合成时 404。
        if self.path == "/fixtures/source.mp4":
            data = _state.get("fixture_bytes")
            if not data:
                return self._send_json(404, {"error": "not_found", "detail": "fixture 未合成（非 --tier t2 模式？）"})
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send_json(404, {"error": "not_found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802 (http.server 约定的方法名)
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        # 本地测试用的抢占模拟端点，不是 adex 真实契约的一部分：不需要签名，用来在
        # 验收 409 stale claim 负例时，把当前 claimToken 换掉（模拟job被另一个 worker
        # re-claim），从而让手上还拿着旧 token 的 worker 调用 report/upload 时被拒。
        if path == "/debug/reclaim":
            return self._handle_debug_reclaim()

        raw_body = self._read_body()
        timestamp = self.headers.get("x-adex-timestamp", "")
        signature = self.headers.get("x-adex-signature", "")

        if path == "/api/worker/remix-jobs/claim":
            if not _verify_signature(timestamp, raw_body.decode("utf-8", errors="replace"), signature):
                return self._unauthorized("bad or missing HMAC signature on claim")
            return self._handle_claim(raw_body)

        if path == "/api/worker/remix-jobs/report":
            if not _verify_signature(timestamp, raw_body.decode("utf-8", errors="replace"), signature):
                return self._unauthorized("bad or missing HMAC signature on report")
            return self._handle_report(raw_body)

        if path == "/api/worker/remix-jobs/upload":
            job_id = ""
            for part in query.split("&"):
                if part.startswith("jobId="):
                    job_id = part.split("=", 1)[1]
                    break
            claim_token_header = self.headers.get("x-adex-claim-token", "")
            content_sha256 = self.headers.get("x-adex-content-sha256", "")
            actual_sha256 = hashlib.sha256(raw_body).hexdigest()
            base_body = f"{job_id}:{claim_token_header}:{content_sha256}"
            sig_ok = _verify_signature(timestamp, base_body, signature)
            if content_sha256 != actual_sha256 or not sig_ok:
                return self._unauthorized("bad/missing HMAC signature or content-sha256 mismatch on upload")
            if claim_token_header != _state["claim_token"]:
                return self._send_json(409, {"error": "stale claim"})
            return self._handle_upload(raw_body, job_id)

        self._send_json(404, {"error": "not_found", "path": path})

    def _handle_debug_reclaim(self) -> None:
        _state["claim_token"] = uuid.uuid4().hex
        _state["attempt"] += 1
        print(f"mock_adex: [debug] job 被强制 re-claim，模拟被其他 worker 抢占，"
              f"新 claimToken={_state['claim_token']} attempt={_state['attempt']}")
        sys.stdout.flush()
        self._send_json(200, {"claimToken": _state["claim_token"], "attempt": _state["attempt"]})

    def _handle_claim(self, raw_body: bytes) -> None:
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            payload = {}

        if _state["claimed"]:
            return self._send_json(200, {"job": None})

        requested_id = payload.get("jobId")
        if requested_id and requested_id != FAKE_JOB["id"]:
            return self._send_json(200, {"job": None})

        _state["claimed"] = True
        _state["claim_token"] = uuid.uuid4().hex
        _state["attempt"] = 1
        job = {**FAKE_JOB, "claimToken": _state["claim_token"], "attempt": _state["attempt"]}
        self._send_json(200, {"job": job})

    def _handle_report(self, raw_body: bytes) -> None:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            return self._send_json(400, {"error": f"invalid json body: {e}"})

        if payload.get("claimToken") != _state["claim_token"]:
            return self._send_json(409, {"error": "stale claim"})

        _state["reports"].append(payload)

        if payload.get("status") == "succeeded":
            qc = payload.get("qcReport") or {}
            status_seq = [r.get("status") for r in _state["reports"]]
            print("\n=== mock_adex: RemixJob succeeded ===")
            print(f"job id        : {payload.get('jobId')}")
            print(f"outputUrl     : {payload.get('outputUrl')}")
            print(f"qcReport.pass : {qc.get('pass')}")
            print(f"upload bytes  : {_state['upload_bytes']}")
            print(f"status seq    : {status_seq}")
            print("======================================\n")
            sys.stdout.flush()

        self._send_json(200, {"ok": True})

    def _handle_upload(self, raw_body: bytes, job_id: str) -> None:
        _state["upload_bytes"] = len(raw_body)
        file_url = (
            f"https://mock-storage.local/remix/{FAKE_JOB['orgId']}/"
            f"{job_id or 'unknown'}/v{_state['attempt']}.mp4"
        )
        self._send_json(200, {"fileUrl": file_url})

    def log_message(self, fmt: str, *fmt_args) -> None:  # 静默默认访问日志，改走 stderr 前缀
        sys.stderr.write("mock_adex: " + (fmt % fmt_args) + "\n")


def main() -> None:
    global FAKE_JOB

    ap = argparse.ArgumentParser(description="worker.py 本地离线验收用的 mock adex 服务器")
    ap.add_argument("port_positional", type=int, nargs="?", default=None, help="监听端口（位置参数）")
    ap.add_argument("--port", type=int, default=None, help="监听端口（--port 形式，优先级更高）")
    ap.add_argument("--ratio", type=str, default=None,
                     help="覆盖 FAKE_JOB.brief.ratio（默认 9:16），用于验收 worker.py 的"
                          "画布尺寸按 ratio 切换（如 --ratio 16:9）")
    ap.add_argument("--tier", type=str, default="t0_5", choices=["t0_5", "t1", "t2"],
                     help="mock 哪种 job.tier：t0_5（默认，逐 beat 生成）、t1（@视频锚定"
                          "重生成，逐 beat 生成 + 参照片段）或 t2（分段路由）——t1/t2 都会"
                          "额外起 /fixtures/source.mp4 静态端点供 worker.py 下载")
    args = ap.parse_args()

    port = args.port or args.port_positional
    if not port:
        sys.exit("需要指定端口：python3 mock_adex.py <port> 或 python3 mock_adex.py --port <port>")

    if args.tier in ("t1", "t2"):
        FAKE_JOB = build_fake_job_t1(port) if args.tier == "t1" else build_fake_job_t2(port)
        print(f"现场合成 {args.tier.upper()} fixture 源视频（ffmpeg lavfi，12 段各 1s 变色+水印）...")
        sys.stdout.flush()
        _state["fixture_bytes"] = _build_fixture_source()
        print(f"  fixture 合成完成，{len(_state['fixture_bytes'])} 字节")
        if args.ratio:
            FAKE_JOB["brief"]["ratio"] = args.ratio
    elif args.ratio:
        FAKE_JOB["brief"]["ratio"] = args.ratio

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock_adex 监听 http://127.0.0.1:{port}  (job id={FAKE_JOB['id']}, tier={FAKE_JOB['tier']}, secret='test')")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
