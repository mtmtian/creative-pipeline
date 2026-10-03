"""
工具路径、本机配置、品牌词表与产品 profile。

- 工具（ffmpeg/ffprobe/whisper-cli/tesseract）按 env -> PATH -> Homebrew 默认路径解析；容器/CI 用
  FFMPEG_BIN/FFPROBE_BIN/TESSERACT_BIN/WHISPER_BIN 覆盖。
- 因机器而异的路径（素材根目录、whisper 模型、hakko-secret）不进仓库：按环境变量 -> 仓库根目录
  config.local.json（已 gitignore，模板见 config.local.example.json）读取。
- 运行前用 `python3 config.py` 或 validate_strict() 检查；缺工具或模型直接报错，不静默降级。
- whisper 用多语言 ggml-base 模型；本机没有 English-only base.en 模型，英文素材也走多语言模型。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# 本机配置：环境变量优先，其次 config.local.json；两处都没有时为空字符串。
# ---------------------------------------------------------------------------
_LOCAL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")


def _load_local_config() -> dict:
    try:
        with open(_LOCAL_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{_LOCAL_CONFIG_PATH} 必须是 JSON 对象")
    return data


_LOCAL = _load_local_config()


def local_setting(name: str) -> str:
    return os.environ.get(name) or str(_LOCAL.get(name) or "")

# ---------------------------------------------------------------------------
# 工具真实路径：env 覆盖；ffmpeg/ffprobe 优先 ffmpeg-full，再回退 PATH；其它工具
# 依次按 env -> shutil.which -> 构建时固化路径解析。
# （Homebrew 默认路径只作为 macOS 开发机的最终兜底，不代表其它机器上有效）。
# 容器/CI 等非 macOS 环境请设置 FFMPEG_BIN/FFPROBE_BIN/TESSERACT_BIN/WHISPER_BIN
# 环境变量覆盖，不需要改这份文件。
# ---------------------------------------------------------------------------


def _resolve_bin(env_name: str, which_name: str, hardcoded: str) -> str:
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val
    found = shutil.which(which_name)
    if found:
        return found
    return hardcoded


def _resolve_ffmpeg_full_first(env_name: str, which_name: str, full_path: str) -> str:
    """env 显式覆盖优先；否则优先带 subtitles/drawtext 的 ffmpeg-full。"""
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val
    if os.path.isfile(full_path):
        return full_path
    return shutil.which(which_name) or full_path


FFMPEG = _resolve_ffmpeg_full_first("FFMPEG_BIN", "ffmpeg", "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
FFPROBE = _resolve_ffmpeg_full_first("FFPROBE_BIN", "ffprobe", "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe")
WHISPER_CLI = _resolve_bin("WHISPER_BIN", "whisper-cli", "/opt/homebrew/bin/whisper-cli")
TESSERACT = _resolve_bin("TESSERACT_BIN", "tesseract", "/opt/homebrew/bin/tesseract")

# whisper 模型：多语言 ggml-base.bin，路径因机器而异（WHISPER_MODEL）。
WHISPER_MODEL = local_setting("WHISPER_MODEL") or None

# ---------------------------------------------------------------------------
# 品牌词表（大小写不敏感，用于 OCR 文本 / whisper 转写文本的关键词匹配）
# 可按产品线扩展，追加即可 —— 不需要改扫描逻辑，scanner.py 只认这个列表。
# 除官方拼写外，必须包含 ASR/OCR 常见误识别变体：whisper 会把口播里的
# "PolyBuzz" 转成 "Polybus"（真实案例：一条竞品素材的转写是 "Download Polybus now!"），
# 仅按官方拼写匹配会在 qc 预检时漏报声轨残留。
# 新增品牌时先听/看一遍样本转写，把误识别形态一并补进来。
# ---------------------------------------------------------------------------

BRANDS = [
    "PolyBuzz",
    "Polybus",       # PolyBuzz 的 whisper 误识别变体
    "Poly Buzz",     # PolyBuzz 的分词变体
    "Talkie",
    "Talky",         # Talkie 的 ASR 变体
    "Emochi",
    "Emoji Chi",     # Emochi 的 ASR 分词变体
    "Honey",
    "Character.AI",
    "Character AI",  # Character.AI 的无点变体（ASR 不会输出点号）
    "Loopit",
    "Loop It",       # Loopit 的分词变体
    "Aippy",
    "Rezona",
    "Sekai",
]

# ---------------------------------------------------------------------------
# OCR 抽帧间隔（秒）。规格默认 1s，真实素材批跑非常慢（tesseract 每帧几百 ms + ffmpeg
# 抽帧开销），本轮验收为了在可接受时间内跑完 5 个真实文件，放宽到 2s。
# 需要更高召回率时改回 1.0 即可，不用改扫描逻辑。
# ---------------------------------------------------------------------------
OCR_INTERVAL_SEC = 2.0


def _check_problems() -> list[str]:
    problems = []

    if not os.path.isfile(FFMPEG):
        problems.append(
            f"ffmpeg 未找到（期望路径: {FFMPEG}）。请运行: brew install ffmpeg-full "
            "（或设置环境变量 FFMPEG_BIN 指向真实路径）。"
        )
    if not os.path.isfile(FFPROBE):
        problems.append(
            f"ffprobe 未找到（期望路径: {FFPROBE}）。请运行: brew install ffmpeg-full "
            "（或设置环境变量 FFPROBE_BIN 指向真实路径）。"
        )
    if not os.path.isfile(WHISPER_CLI):
        problems.append(
            f"whisper-cli 未找到（期望路径: {WHISPER_CLI}）。请运行: brew install whisper-cpp"
            "（或设置环境变量 WHISPER_BIN 指向真实路径）。"
        )
    if not os.path.isfile(TESSERACT):
        problems.append(
            f"tesseract 未找到（期望路径: {TESSERACT}）。请运行: brew install tesseract"
            "（或设置环境变量 TESSERACT_BIN 指向真实路径）。"
        )
    if not WHISPER_MODEL or not os.path.isfile(WHISPER_MODEL):
        problems.append(
            "whisper 模型文件未找到（期望路径: "
            f"{WHISPER_MODEL}）。在 config.local.json 或环境变量 WHISPER_MODEL 里指向多语言 ggml-base.bin，"
            "没有的话下载: bash /opt/homebrew/share/whisper-cpp/models/download-ggml-model.sh base"
        )

    return problems


def validate() -> None:
    """非强制校验：只在 stderr 打印警告，不 sys.exit。

    仅仅 `import config` 不应该让缺工具的机器上所有脚本连启动都启动不了——这个函数
    在模块导入时调用一次，只是尽早给出信号，不代表脚本一定会用到这些工具（比如只
    调 config.get_anthropic_api_key() 的脚本根本不需要 ffmpeg）。真正要用到工具、
    需要强约束「缺就别跑」的入口脚本，请显式调用 validate_strict()。
    """
    problems = _check_problems()
    if problems:
        sys.stderr.write("config.py 校验警告，以下依赖可能缺失（若当前脚本用不到可忽略）：\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")


def validate_strict() -> None:
    """严格校验：任何一项缺失就明确报错退出（sys.exit(1)）。

    行为等同旧版 validate()——各入口脚本在实际要用 ffmpeg/ffprobe/tesseract/whisper
    之前显式调用这个函数，而不是依赖 `import config` 的隐式副作用。
    """
    problems = _check_problems()
    if problems:
        sys.stderr.write("config.py 严格校验失败，缺少以下依赖：\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        sys.exit(1)


# 模块导入时校验一次，但只警告不退出（见 validate() 说明）。
validate()


# ---------------------------------------------------------------------------
# 产品上下文（LLM 精排 / L3 remix brief 生成用）
# ---------------------------------------------------------------------------

PRODUCT = {
    "name": "Cuddler — Playable Stories",
    "audience": "CAI 难民 / RP 创作者",
    "selling_points": ["分支剧情瞬间", "chat→film 一键成片", "角色语音通话"],
    "brand_assets_pending": True,  # logo/尾帧/UI 截图后补，brief 里用占位插槽 @图片N 表示
}

# 素材根目录（本机配置）；未配置时 profile 里的路径退化为相对路径，只能跑不依赖素材的单测。
HAKKO_ROOT = local_setting("HAKKO_ROOT")
LUDDI_ROOT = local_setting("LUDDI_ROOT")

# 带 code / batch_root 的 profile 可以建生产批次（batch_pipeline.py）；code 是成片素材 ID 的产品前缀。
# 各产品素材区统一为 00_文档/01_资产/02_源素材/03_人力产出/04_agent产出（2026-10-04 重整），
# agent 批次只放 04_agent产出/生产批次/，人力与代理交付在 03_人力产出/。
PRODUCT_PROFILES = {
    "cuddler": PRODUCT,
    "hakko-pc": {
        "name": "HakkoAI PC",
        "code": "hakkopc",
        "batch_root": os.path.join(HAKKO_ROOT, "PC端/04_agent产出/生产批次"),
        "audience": "18+ PC 游戏玩家，尤其需要实时攻略与情感陪伴的年轻用户",
        "selling_points": ["实时读取游戏画面给攻略", "吐槽玩家或队友操作", "游戏场景情感陪伴"],
        "logo_path": os.path.join(HAKKO_ROOT, "PC端/01_资产/logo/PC/LOGO+ICON.png"),
        "endcard_logo_path": os.path.join(HAKKO_ROOT, "PC端/01_资产/logo/PC/hakkoai_logo_white.png"),
        "cta": {"US": "Download Now", "JP": "今すぐダウンロード"},
        # preflight 在结尾几秒的 OCR 文本里找这些词，判断有没有品牌尾帧（大小写不敏感，子串匹配）。
        "endcard_tokens": ["hakko", "download", "ダウンロード"],
        # PC 端暂无竞品参考列表、也很少做竞品改造（2026-10-04），预检不扫竞品词。
        "competitor_brands": [],
        "default_formats": ["16:9", "1:1"],
        "audio_strategies": ["source", "seedance", "voiceover", "silent"],
        "default_audio_strategy": "source",
    },
    "hakko-mobile": {
        "name": "Hakko",
        "code": "hakkomobile",
        "batch_root": os.path.join(HAKKO_ROOT, "移动端/04_agent产出/生产批次"),
        # 受众、卖点、渠道和尺度以 brief 为准，不在这里复制一份。
        "brief_path": os.path.join(HAKKO_ROOT, "AGENTS.md"),
        "endcard_assets": os.path.join(HAKKO_ROOT, "移动端/01_资产/尾帧"),
        "endcard_tokens": ["hakko", "download", "app store", "google play"],
        "competitor_brands": BRANDS,
        "default_formats": ["9:16", "16:9"],
        "audio_strategies": ["source", "seedance", "voiceover", "silent"],
        "default_audio_strategy": "source",
    },
    "luddi": {
        "name": "Luddi",
        "code": "luddi",
        "batch_root": os.path.join(LUDDI_ROOT, "04_agent产出/生产批次"),
        # 受众、卖点、渠道风格和路径 A/B/C 以 brief 与 SOP（00_文档/广告视频产出流程与优化总结.md）为准。
        "brief_path": os.path.join(LUDDI_ROOT, "AGENTS.md"),
        "endcard_assets": os.path.join(LUDDI_ROOT, "01_资产/尾帧"),
        "endcard_tokens": ["luddi", "download", "app store", "google play"],
        "competitor_brands": ["Loopit", "Loop It", "Aippy", "Rezona", "Sekai"],
        "default_formats": ["9:16", "16:9"],
        "audio_strategies": ["source", "seedance", "voiceover", "silent"],
        # 路径 A/B 默认整段 VO + BGM；路径 C 保留原声时在 creatives.json 逐条标 source。
        "default_audio_strategy": "voiceover",
    },
}


def batch_profiles() -> list[str]:
    """能建生产批次的 profile 名。"""
    return [name for name, profile in PRODUCT_PROFILES.items() if "code" in profile and "batch_root" in profile]


def get_product_profile(name: str | None = None) -> dict:
    """返回产品上下文；不传时保持历史 Cuddler 默认行为。"""
    if name is None:
        return PRODUCT
    try:
        return PRODUCT_PROFILES[name]
    except KeyError as e:
        raise ValueError(f"未知产品 profile: {name}") from e

# ---------------------------------------------------------------------------
# LLM（Anthropic）配置
# ---------------------------------------------------------------------------
# 模型：默认 claude-sonnet-4-5，可用环境变量 ANTHROPIC_MODEL 覆盖。
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# 每次调用的 max_tokens。multimodal（拼图图片 + 长转写/OCR 上下文）场景需要更大的输出空间，
# 分级精排/brief 生成都要求输出完整 JSON 结构，8000 留足余量。
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8000"))

# 依次尝试的 hakko-secret 密钥名（探测顺序见任务书第二步）。
_HAKKO_SECRET_BIN = local_setting("HAKKO_SECRET_BIN") or shutil.which("hakko-secret") or ""
_ANTHROPIC_SECRET_NAMES = [
    "anthropic-api-key",
    "anthropic_api_key",
    "claude-api-key",
    "anthropic",
]


def get_anthropic_api_key() -> str | None:
    """运行时通过 hakko-secret 取 Anthropic API key，依次探测已知的几个密钥名。

    绝不打印/落盘 key 本身。找不到可用密钥时返回 None（调用方负责如实报告，
    不允许伪造 LLM 响应）。
    """
    if not os.path.isfile(_HAKKO_SECRET_BIN):
        return None
    for name in _ANTHROPIC_SECRET_NAMES:
        try:
            cp = subprocess.run(
                [_HAKKO_SECRET_BIN, name], capture_output=True, text=True
            )
        except OSError:
            continue
        if cp.returncode == 0:
            key = cp.stdout.strip()
            if key:
                return key
    return None


# ---------------------------------------------------------------------------
# 火山方舟 Ark（Seedance2 视频生成）配置
# ---------------------------------------------------------------------------
# 依次尝试的 hakko-secret 密钥名（"cc-volcengine-ark-key" 已手动确认存在，
# 其余是防止改名的兜底探测项，写法同 _ANTHROPIC_SECRET_NAMES）。
_ARK_SECRET_NAMES = [
    "cc-volcengine-ark-key",
    "volcengine-ark-key",
    "ark-api-key",
]

# 成本护栏：seedance_gen.py 超过这两条硬性上限直接拒绝执行，不静默降级、不绕过。
SEEDANCE_MAX_CLIPS_PER_RUN = 2
SEEDANCE_MAX_DURATION_SEC = 12


def get_ark_api_key() -> str | None:
    """取火山方舟 Ark API key：优先读环境变量 ARK_API_KEY（容器等无 hakko-secret 的
    部署环境用），没有则走 hakko-secret 依次探测已知的几个密钥名。

    绝不打印/落盘 key 本身。找不到可用密钥时返回 None（调用方负责如实报告，
    不允许伪造生成响应）。
    """
    env_key = os.environ.get("ARK_API_KEY")
    if env_key:
        return env_key
    if not os.path.isfile(_HAKKO_SECRET_BIN):
        return None
    for name in _ARK_SECRET_NAMES:
        try:
            cp = subprocess.run(
                [_HAKKO_SECRET_BIN, name], capture_output=True, text=True
            )
        except OSError:
            continue
        if cp.returncode == 0:
            key = cp.stdout.strip()
            if key:
                return key
    return None


if __name__ == "__main__":
    print("config OK")
    print(f"  FFMPEG           = {FFMPEG}")
    print(f"  FFPROBE          = {FFPROBE}")
    print(f"  WHISPER_CLI      = {WHISPER_CLI}")
    print(f"  TESSERACT        = {TESSERACT}")
    print(f"  WHISPER_MODEL    = {WHISPER_MODEL}")
    print(f"  BRANDS           = {BRANDS}")
    print(f"  PRODUCT          = {PRODUCT}")
    print(f"  ANTHROPIC_MODEL  = {ANTHROPIC_MODEL}")
    print(f"  ANTHROPIC_MAX_TOKENS = {ANTHROPIC_MAX_TOKENS}")
    print(f"  anthropic key available = {get_anthropic_api_key() is not None}")
    print(f"  SEEDANCE_MAX_CLIPS_PER_RUN = {SEEDANCE_MAX_CLIPS_PER_RUN}")
    print(f"  SEEDANCE_MAX_DURATION_SEC  = {SEEDANCE_MAX_DURATION_SEC}")
    print(f"  ark key available = {get_ark_api_key() is not None}")
