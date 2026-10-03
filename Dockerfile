# worker.py 运行时镜像：debian slim + ffmpeg + tesseract-ocr + whisper.cpp（多阶段构建，
# 第一阶段编译 whisper.cpp 产出 whisper-cli 二进制，第二阶段只拷贝运行所需产物，不带编译
# 工具链）。仅供参考，构建不要求在本地跑通/验收。
#
# 运行时注入的环境变量（不写进镜像，由部署方在 `docker run -e` 或编排系统里注入）：
#   WORKER_WEBHOOK_SECRET  —— worker.py 对 adex 的 HMAC 签名密钥（worker.py --secret 的兜底来源）
#   ADEX_BASE_URL          —— adex control-plane 的 base URL（对应 worker.py --base-url）。
#                              必须含部署 basePath（如 https://host/adex），不是裸 host，
#                              worker.py 会直接拼 /api/worker/... 路径，缺 basePath 会 404。
#   ARK_API_KEY            —— 火山方舟 Ark API key（--confirm 真实调用时用；本地跑时经
#                              config.get_ark_api_key() 走 hakko-secret 取，容器里没有
#                              hakko-secret 时 config.get_ark_api_key() 会优先读这个环境
#                              变量做兜底）
#
# 绝不在镜像里写死任何密钥；ENTRYPOINT 运行 worker.py，实际 --base-url/--secret 等参数
# 由 `docker run` 的 CMD 或编排系统传入。

# ---------------------------------------------------------------------------
# 阶段 1：编译 whisper.cpp（产出 whisper-cli 二进制 + ggml 模型下载脚本）
# ---------------------------------------------------------------------------
FROM debian:12-slim AS whisper-build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git whisper.cpp
WORKDIR /build/whisper.cpp
RUN cmake -B build && cmake --build build --config Release -j "$(nproc)"

# whisper-cli 实际产出路径取决于 whisper.cpp 版本（build/bin/whisper-cli 是较新版本的
# 布局），第二阶段按这个路径拷贝；如果上游改了产物路径，这里要跟着更新。

# ---------------------------------------------------------------------------
# 阶段 2：运行时镜像
# ---------------------------------------------------------------------------
FROM debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        ffmpeg \
        tesseract-ocr \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 从编译阶段拷贝 whisper-cli 二进制到运行时镜像的 PATH 里。
COPY --from=whisper-build /build/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli

WORKDIR /app

# 只拷贝运行 worker.py 需要的脚本；out/、_batches/、_testset_links/ 等本地产物/中间数据
# 目录不进镜像。
COPY worker.py mock_adex.py config.py assemble.py seedance_gen.py scanner.py qc.py analyze.py ./

# config.py 里的 FFMPEG/FFPROBE/TESSERACT/WHISPER_BIN/WHISPER_MODEL 现在走三级回退
# （env 覆盖 -> shutil.which -> macOS 硬编码路径兜底，见 config.py _resolve_bin），
# debian 镜像里 apt/编译产出的真实路径显式写成环境变量，不依赖 which 探测结果：
ENV FFMPEG_BIN=/usr/bin/ffmpeg
ENV FFPROBE_BIN=/usr/bin/ffprobe
ENV TESSERACT_BIN=/usr/bin/tesseract
ENV WHISPER_BIN=/usr/local/bin/whisper-cli
# WHISPER_MODEL 未设置环境变量默认值——ggml 模型不在本镜像里（阶段 1 只编译了
# whisper-cli 二进制，没有下载模型文件），需要部署方另外挂载模型文件后设置
# WHISPER_MODEL 指向真实路径；未设置时 config.py 会 fallback 到开发机固化路径，
# 在容器里必定不存在，validate_strict() 会在入口脚本实际用到 whisper 时报错退出
# （不是导入时崩溃，也不会静默用错路径跑下去）。

ENTRYPOINT ["python3", "worker.py"]
