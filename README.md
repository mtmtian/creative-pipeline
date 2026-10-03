# creative-pipeline

广告视频素材的分析、改造、生产批次与投放前可用性预检工具链（本地处理，ffmpeg / whisper.cpp / Apple Vision，非 macOS 用 tesseract）。

规格来源：[adex](https://github.com/mtmtian/adex) 仓库 `docs/growth/08-competitor-creative-pipeline.md`。

## 本机配置

素材根目录、whisper 模型和 hakko-secret 的位置因机器而异，不进仓库。复制 `config.local.example.json` 为
`config.local.json`（已 gitignore）后填写，或用同名环境变量覆盖：`HAKKO_ROOT`、`LUDDI_ROOT`、`CUDDLER_ROOT`、`WHISPER_MODEL`、
`HAKKO_SECRET_BIN`。工具路径用 `FFMPEG_BIN` / `FFPROBE_BIN` / `WHISPER_BIN` / `TESSERACT_BIN` 覆盖。
`python3 config.py` 打印并校验当前配置。

## 公开仓库约定

仓库是公开的。提交前跑 `python3 check_public.py`：它拒绝绝对的本机路径、本地配置、产物目录和私钥类内容。
内部笔记、实测记录、素材清单写到素材目录（如 Hakko `00_文档/_归档/creative-pipeline/`），不要提交进来。

## 目录结构

```text
config.py       工具路径（ffmpeg/ffprobe/whisper-cli/tesseract）+ 品牌词表，导入时校验，缺什么直接报错退出
vision_ocr.swift  macOS 画面文字识别（Apple Vision），analyze.ocr_images 首次使用时自动编译
scanner.py      品牌词扫描公共模块（analyze.py 和 qc.py 共享，不复制粘贴）
analyze.py      四件套批处理：manifest / metadata / keyframes / transcript / ocr
classify.py     规则版分级建议：读 analyze.py 产物 -> classification.csv
qc.py           投放前预检：对成片重跑品牌词扫描 -> qc_report.csv，命中即 FAIL 非零退出
remix_brief.py  L3 复刻 brief 生成器：differentiated_storyboard + seedance_prompt + sample_shots
prompt_lint.py  seedance_prompt 纯规则 lint（引用/时段/运镜/音频/废话词）
seedance_gen.py Seedance2（doubao-seedance-2-0）视频生成客户端：默认 dry-run，--confirm 才真实调用
cut_ref.py      从原片切 Seedance @视频 参照片段（ffmpeg 无损快切 + Ark 约束校验）
assemble.py     分段路由成片拼装器：reuse 段切原片 + generated 段接生成产物 + 可选尾帧，
                统一转码到 brief.ratio 对应画布@30fps/aac48k，concat filter 拼接，自动跑 qc.py 预检
worker.py       remix job 执行客户端（adex control-plane 的 worker 端）：认领 job -> 按档路由
                （t0_5 逐段生成 / t1 @视频锚定重生成 / t2 复用实切）-> 拼装 -> 品牌预检 -> 上传，
                逐阶段 HMAC 签名上报；默认 dry-run 用 ffmpeg 占位片，--confirm 才真实调用
mock_adex.py    worker.py 的本地离线验收服务器（stdlib http.server，零第三方依赖），
                复刻 adex 的 claim/report/upload 三接口 + 固定 secret 'test' 校验签名
batch_pipeline.py  生产批次入口（hakko-pc / hakko-mobile）：初始化、素材 ID、分析、转码、字幕、品牌尾帧、
                审核清单、规格 QC、可用性预检、人审、交付 manifest
preflight.py    成片投放前可用性预检（标准是能在广告平台用）：平台硬限制、解码、黑屏/冻帧/静音/响度、音画时长、
                竞品品牌词（按产品）、口播语种与地区、字幕贴边被裁、品牌尾帧；输出 report.html 审片页
test_preflight.py  预检判定规则单测 + 合成缺陷视频端到端
test_batch_pipeline.py  批次契约、素材 ID、审核/QC 哈希绑定、交付 manifest 单测（含真实 ffmpeg 端到端）
test_seedance_gen.py  seedance_gen.py 分时段解析纯函数单测（不调 API，不发网络请求）
check_public.py 公开前检查：拒绝本机绝对路径、本地配置、产物目录、私钥等内容
config.local.example.json  本机配置模板（复制为 config.local.json，不进仓库）
out/            所有产物输出目录（已加入 .gitignore，不进 git）
out/gen/        seedance_gen.py 真实调用产物（mp4 + spend_log.csv 成本记账）
```

## 用法

```bash
# 探测/查看当前工具配置
python3 config.py

# 批量分析一个素材目录，产出四件套
python3 analyze.py <素材目录> [--out out/<批次名>] [--force]

# 对 analyze.py 的产物做规则版分级建议
python3 classify.py --analysis out/<批次名>

# 对成片做投放前预检（品牌词声轨 + 画面扫描）
python3 qc.py <成片文件或目录> [--out out/qc]

# LLM 分级精排（多模态看拼图，补画面维度：真人脸/IP/结构价值 -> level_v2）
python3 classify_llm.py --analysis out/<批次名> [--limit N] [--force] [--file <文件名>]

# 对 L3 素材生成复刻 brief（差异化 storyboard + Seedance prompt，自动过 lint）
python3 remix_brief.py --analysis out/<批次名> [--file <文件名>]

# 单独 lint 一个 brief 的 seedance_prompt
python3 prompt_lint.py <brief.json> [--has-real-face] [--duration-sec N]

# Seedance2 视频生成（默认 dry-run，只打印请求体不花钱；--confirm 才真实调用）
python3 seedance_gen.py --brief out/<批次>/briefs/<x>.json --shot <sample序号|all-samples> [--dry-run] [--confirm]
python3 seedance_gen.py --prompt-text "..." [--ratio 9:16] [--duration 8] [--ref-image <url>] [--dry-run] [--confirm]

# 从原片切 Seedance @视频 参照片段（brief 有 ref_video_cut 字段时可直接读取）
python3 cut_ref.py --brief out/<批次>/briefs/<x>.json [--out out/refs/]
python3 cut_ref.py <源视频路径> <start_sec> <end_sec> [--out out/refs/] [--reencode]

# 分段路由成片拼装：reuse 段切原片 + generated 段接 Seedance 产物 + 可选尾帧
python3 assemble.py --brief out/<批次>/briefs/<x>.json --source <原片路径> \
    --gen-clip <时段>=<mp4路径> [--gen-clip ...] [--endcard <mp4/png路径>] [--out out/assembled/]

# seedance_gen.py 分时段解析纯函数单测（不调 API）
python3 test_seedance_gen.py

# remix job 执行客户端（secret 走 --secret 或环境变量 WORKER_WEBHOOK_SECRET，绝不落盘）
python3 worker.py --base-url <adex 地址，含 basePath> [--secret <key>] [--job-id <id>] \
    [--confirm] [--out-dir out/worker] [--max-clips N]

# 本地离线跑通 worker（另开一个终端起 mock 服务器，secret 固定 'test'）
python3 mock_adex.py 8765 [--tier t0_5|t1|t2] [--ratio 9:16]
python3 worker.py --base-url http://127.0.0.1:8765 --secret test
```

### 生产批次入口（batch_pipeline.py）

`batch_pipeline.py` 只做本地处理，不下载飞书素材，也不调用生成 API。profile 决定批次根目录和素材 ID 前缀：

| profile | 默认批次根目录 | 素材 ID 前缀 | 默认画幅 |
| --- | --- | --- | --- |
| `hakko-pc` | `Hakko/文案&脚本&视频制作-Hakko/PC端/04_agent产出/生产批次` | `hakkopc` | 16:9、1:1 |
| `hakko-mobile` | `Hakko/文案&脚本&视频制作-Hakko/移动端/04_agent产出/生产批次` | `hakkomobile` | 9:16、16:9 |
| `luddi` | `Luddi/文案&脚本&视频制作/04_agent产出/生产批次` | `luddi` | 9:16、16:9 |

三个产品的素材区统一为 `00_文档/01_资产/02_源素材/03_人力产出/04_agent产出`（2026-10-04 重整）。
批次根目录只放 agent 批次；人力与代理交付的成片在各自的 `03_人力产出/<来源>/<日期>_<原名>/`，
需要预检时直接对那个目录跑 `preflight.py`。Cuddler 暂无 agent 产出，没有批次 profile。

批次目录沿用 2026-07-31 的 PC 约定：`00_input/`（源素材或软链接）、`20_plan/`（方案、EDL、
`creatives.json` 标注、审核记录）、`30_source_hooks/`（有则保留）、`50_delivery/<画幅>/<地区>/`。
抽帧、转写、尾帧、QC 快照等过程文件写到本仓库 `out/batches/<批次>/`，不放回批次目录。

**素材 ID**：`{产品}_{批次}_{地区}_{画幅}_{hook}_v{N}`，如
`hakkopc_20260727-ai-girlfriend-hooks_us_16x9_anytime_v1`。字段之间用 `_`，字段内部用 `-`（批次名和 hook
本身含 `-`），全小写、≤100 字符。它同时是成片文件名、YouTube 标题和 TikTok 素材名，投放回收数据靠它对回批次。
批次名必须是 `YYYYMMDD-小写短横线`，8 月以后那些 `9.28`、`0918` 之类的平铺文件夹进不了这套流程。

```bash
# 初始化批次（已有同 profile 的 batch.json 会原样保留；旧批次的 locale_policy 与产品快照照常可读）
python3 batch_pipeline.py init --profile hakko-pc --batch-id 20261004-topic-name

# 给成片取名：打印素材 ID 和应放的交付路径
python3 batch_pipeline.py name <批次目录> --locale US --format 16:9 --hook anytime [--version 2]

# 对 00_input 跑 analyze.py，产物在 out/batches/<批次>/analysis
python3 batch_pipeline.py analyze <批次目录>

# 归一化到投放规格并在最后一步烧录 SRT（缩放 + 补黑边；改画幅请先用 video-reframe）
python3 batch_pipeline.py burn-srt <输入.mp4> <字幕.srt> <输出.mp4> --format 16:9 --audio-strategy source

# 生成 Hakko PC Logo + CTA 尾帧（移动端用 01_资产/尾帧 的现成素材）
python3 batch_pipeline.py make-endcard <批次目录> --format 9:16 --locale US

# 成片直接放进 50_delivery/<画幅>/<地区>/<素材ID>.mp4 后：
python3 batch_pipeline.py review <批次目录>          # 扫描交付目录，写 20_plan/review_manifest.json
python3 batch_pipeline.py qc <批次目录>              # 规格 QC，写 20_plan/qc_report.json
python3 batch_pipeline.py preflight <批次目录> [--channel google|tiktok]   # 可用性预检，写 20_plan/preflight_report.json + 审片页
python3 batch_pipeline.py approve <批次目录> --reviewer <审核人>   # 人看完审片页后才执行
python3 batch_pipeline.py deliver <批次目录>         # 写 50_delivery/manifest.json

# 编码不合规（HEVC、44.1kHz、非恒定 30fps、缺 SAR）时只转编码、不改画面：
python3 batch_pipeline.py normalize <输入.mp4> <输出.mp4>
```

- `review` 拒绝交付目录里任何不是素材 ID 命名的 `.mp4`、ID 与所在画幅/地区/批次/产品不一致的文件、符号链接；
  `50_delivery` 顶层的非画幅目录（如旧批次的 `00_说明/`）只提示忽略。`20_plan/creatives.json` 以 hook 为键
  记录投放标注（建议 `channel`、`selling_point`、`persona`、`audience`、`hypothesis`、`audio_strategy`、
  `source`），缺标注的 hook 会被列出。重新 `review` 会作废旧的 approved.json、qc_report.json 和交付 manifest。
- `qc` 规格：画幅比例精确、最低分辨率（16:9 ≥1920×1080，1:1 ≥1080×1080，9:16 ≥1080×1920，4:5 ≥1080×1350）、
  SAR 1:1、无旋转、avg/r 帧率都等于 30、H.264/yuv420p、AAC 48kHz stereo、恰好一条视频一条音频。
- `approved.json` 绑定 review manifest 的 sha256；`deliver` 要求批准与当前清单一致、QC 通过且不过期、每个文件哈希
  与审核时一致，然后写出 `50_delivery/manifest.json`：批次、profile、审核人、QC 时间，以及每条成片的素材 ID、
  相对路径、画幅、地区、hook、版本、宽高、时长、字节数、sha256、标注。它是交给投放的唯一交接文件；
  全程不复制、不删除成片。`check-approved` 未批准时返回非零。

### preflight.py（投放前可用性预检）

真实花钱前确认成片“可用”：拦住会出事故或白花钱的客观缺陷，并给人一个审片页判断主观项。判断标准是“能不能在广告
平台上用”：编码、采样率、帧率只记录不判定（2026-10-04 决定；新产出的统一规格由 `batch_pipeline.py qc` 和
`normalize` 负责）。可以对任意文件或目录跑（人力/代理交来的平铺文件夹也行），也由 `batch_pipeline.py preflight`
对批次跑并绑定审核清单。

```bash
python3 preflight.py <成片或目录...> --profile hakko-pc [--locale US] [--channel google|tiktok] [--out DIR]
```

| 检查 | FAIL（不可投） | WARN（需人看） |
| --- | --- | --- |
| platform | 读不出视频流；超过 256 GiB 上传上限（与 google-ads 投放 skill 的上传校验一致） | 画幅不在 16:9/1:1/4:5/9:16 的 ±1% 内；编码信息只写进说明 |
| duration / av_sync | 短于 5s；音画时长差 >1.5s | 不在渠道推荐时长（google 10-180s，tiktok 9-60s）；差 >0.5s |
| decode / black / freeze | 解码错误；开场 3 秒内黑屏 ≥0.5s；黑屏累计 ≥30%；画面静止累计 ≥50% | 其它 ≥1s 黑屏；开场静止 ≥2s；其它静止 ≥3s（结尾静态尾帧不算） |
| silence / loudness | 全片无声 | 开场无声 ≥1.5s；中段无声 ≥3s；响度 < -24 或 > -8 LUFS；真峰值 > +1 dBFS |
| speech_brand / screen_brand | 口播或画面文字出现该产品 `competitor_brands` 里的竞品词（hakko-mobile 用 `config.BRANDS`；hakko-pc 暂无竞品列表，不扫） | — |
| speech_language | 口播语种与地区不符（US→en，JP→ja…，少于 5 个词不判） | 地区不在语种表 |
| text_edge | — | ≥2 帧有“至少 3 个词、横跨画面中线的字幕行”顶到画面左右 3% 以内（被裁或没留安全边距；角落 HUD、浮窗不算） |
| endcard | — | 结尾 3 秒 OCR 没认出 profile 的 `endcard_tokens` |

地区优先从素材 ID 文件名或 `<地区>/` 目录识别，其次 `--locale`。产物：`report.json`、`report.html`（每条附开头 3 秒 /
中段 / 结尾 3 秒抽帧，FAIL 排最前）、`sheets/`。任一 FAIL 退出码 1。转写用 whisper-cli 自动识别语种，画面文字用
Apple Vision（按地区选识别语言，日文可识别），全程本地处理。PASS 只代表自动检查没发现问题，hook 吸引力、尺度、
遮标质量仍需人看审片页。

2026-10-04 在真实素材上的结果（规则校准依据）：PC 人力批次 `9.28` 11 条中 10 条 PASS、1 条 WARN（第 23–24 秒字幕行
左右顶边）；7 月 agent 批次 `20260727-ai-girlfriend-hooks` 6 条全部 PASS（编码是 HEVC 或 44.1kHz，但不影响投放）；
今天的横转竖样片 3 条 WARN（跟拍镜头切断烧录字幕、16:9 尾帧缩进竖版后近乎全黑）。单个词贴边（主播 ID 角标、
游戏 HUD）不报；峰值顶在 0 dBFS 的投放母带只在 PASS 说明里提示。

同日改用 Apple Vision 后复跑：Vision 认得出角落里的游戏标签和聊天浮窗，字幕行因此加了“横跨画面中线”的条件。
`9.28` 仍是 10 PASS / 1 WARN；7 月 agent 批次 6 条 PASS；移动端代理批次 15 条 0 FAIL、4 WARN（1 条字幕几乎占满全宽没留
边距，3 条字幕压在 UI 字或水印上被识别成同一行）；日语代理 2 条 1 WARN（游戏顶部标题贴左边，误报）；Cuddler
TikTok 重剪 6 条里有 1 条新加的 hook 文案顶到右边缘，挪到画面中部后全部 PASS。

### analyze.py 产物说明

| 文件 | 列 |
| --- | --- |
| `manifest.csv` | file, rel_path, size_bytes, duration_sec, fingerprint, dup_of |
| `metadata.csv` | file, duration_sec, width, height, fps, video_codec, has_audio, audio_codec, bitrate_kbps |
| `keyframes/<视频名>.jpg` | 0.3s/2s/5s/中点/尾帧 横向拼接 contact sheet |
| `transcript.csv` | file, start, end, text（whisper-cli 转写，逐段；段首段尾按 DTW 词级时间戳校正） |
| `transcript_brand_hits.csv` | file, start, end, brand, matched_text |
| `ocr_frames.csv` | file, ts, text（每 `OCR_INTERVAL_SEC` 秒抽帧识别画面文字） |
| `ocr_brand_hits.csv` | file, ts, brand, matched_text |

覆盖了 Luddi 参考产物（`metadata.csv` / `ocr_frames.csv` / `transcript_summary.csv`）的信息量，
且列更细（逐帧/逐段而非汇总一行，多了指纹去重、码率、编码等字段）。

### classify.py 分级规则

⚠️ **此为规则版分级建议，L3 判定（结构值钱 / 含第三方 IP / 真人脸）需要人工或 LLM 看画面识别，
本版本不做，所有输出最高只到 L1 / L2 / L4。**

- 声轨命中品牌词 且 OCR 命中率 > 30% → **L4**（灵感入库，画面声轨都不能用）
- 声轨命中品牌词 且 OCR 命中率 ≤ 30% → **L2**（抽片段重组，需去声轨）
- 声轨无命中，OCR 命中率 > 30% → **L4**
- 声轨无命中，10% < OCR 命中率 ≤ 30% → **L2**（中等命中，抽片段重组）
- 声轨无命中，0 < OCR 命中率 ≤ 10% → **L1**（疑似固定 logo，delogo/遮标）
- 全干净 → **L1**（只需换尾帧）

### qc.py

对目标视频重跑 whisper 转写 + OCR，用 `scanner.py` 扫品牌词。任何一处命中即整体 `FAIL`，
`sys.exit(1)`；全干净则 `PASS`，`exit(0)`。

### classify_llm.py / remix_brief.py / prompt_lint.py（v2 分级精排与 L3 brief）

- **classify_llm.py**：对每个文件组装 multimodal 消息（关键帧拼图 + 转写 + OCR 命中摘要 + 规则版预分级），
  调 Anthropic API（默认 `claude-sonnet-4-5`，key 运行时经 hakko-secret 获取、零落盘）。产出
  `analysis/<stem>.json`（5-aspect 逐镜拆解 / motion_type / has_real_face / material_type /
  hook_analysis / structure_value / level_v2）与 `classification_v2.csv`。断点续跑同 analyze.py。
  `--file <文件名>` 只重跑单个文件（忽略 `--limit`，需配合 `--force` 才会重新调用
  LLM），只更新 `classification_v2.csv` 里对应那一行，不整表重写、不重复追加。
  `level_v2` 判定规则第 3 条含显式反例：**品牌只在固定角标 + 真人出镜 → 仍是
  `L1`**，真人脸永远不改变分级，只改变生成阶段的输入方式（能不能用 `@视频N` 参照）。
- **remix_brief.py**：对 level_v2=L3 的文件（或 `--file` 强制指定的任意文件）生成
  `briefs/<stem>.json` —— 差异化 storyboard（每段 borrow_structure / swap_content /
  overlay_text 分离）、8 组件 + @引用插槽的 seedance_prompt、sample_shots（hook 首镜 +
  卖点核心镜，sample-first 门禁）、成本粗估。生成后自动过 lint，错误回喂 LLM 修复
  （≤2 轮），仍不过标记 needs_review。
  **样式锚定（`has_real_face` 驱动）**：`has_real_face=false` 时 seedance_prompt
  必须包含 `@视频1` 引用（"完全参考 @视频1 的运镜、剪辑节奏和画面风格"），brief
  JSON 顶层新增 `ref_video_cut: {source_file, start_sec, end_sec}` 字段（从原片选一段
  2-15s、<50MB 的连续片段作运镜/节奏/风格参照，遵守 Ark 视频参照约束）；画面主体
  内容仍然 100% 差异化为 Cuddler，禁止复述原片画面。`has_real_face=true` 时禁用
  `@视频N`、不产出 `ref_video_cut`，走纯文字化结构路线（Seedance 拦截写实真人脸输入）。
  seedance_prompt 必须包含一个字面小节标题「分时段描述：」，格式
  `0-N秒，...；N-M秒，...`，独立于"动作与运动描述"小节——`seedance_gen.py --shot`
  靠这个标题做正则裁剪。
- **prompt_lint.py**：纯规则引擎，错误级非零退出。检查：@引用用途声明、引用数量上限
  （图≤9/视频≤3/总≤12）、>8s 分时段、互斥运镜、单时段动作 ≤3、音频设计段、生成内文字
  指令（应走 overlay）、has_real_face 时禁 @视频 引用、废话词 warn。
- **cut_ref.py**：从原片切 Seedance `@视频` 参照片段。`--brief <brief.json>` 直接读
  `ref_video_cut` 字段（源文件按 `_batches/<批次名>/` 下的软链接查找）；也支持手动
  `<源视频路径> <start_sec> <end_sec>`。用 `config.FFMPEG` 无损快切（`-c copy`），
  `--reencode` 可换精确起止点但更慢。切完用 ffprobe 校验 Ark 约束（单段 2-15s、
  <50MB、mp4），任何一项不满足直接报错并保留产物供人工检查。产物命名
  `<源文件stem>_<start>-<end>s.mp4`，落在 `out/refs/`。打印提示："需上传公网 URL
  后经 `--ref-video` 传入"——本地 mp4 还不能直接喂给 `seedance_gen.py`，Ark 只吃
  公网可访问 URL（见 `seedance_gen.py` 的 `build_reference_content()`）。

### 分段路由（segment-level routing）

素材常是混合体——"真人梗 hook（干净可复用）+ 品牌化主体（需重制）"，整条素材一个
`level_v2` 太粗，分段路由把它拆成可执行的分段方案：

- **`classify_llm.py` 产出 `segment_plan`**：`analysis/<stem>.json` 新增
  `segment_plan` 数组（`time_range`/`action`/`reason`），`action` 三选一：
  `reuse`（画面声轨都干净、有 hook 价值 → 直接复用原片，不 AI 重制）/
  `remake`（品牌贯穿剪不掉，但结构值钱 → 换内容重制）/ `drop`（纯品牌 UI/
  下载页/尾帧 → 丢弃，换成 Cuddler 尾帧插槽）。全片时间轴无缝覆盖，段与段
  相接不留空洞。判定依据是带时间戳的转写（含品牌命中标注）+ OCR 品牌命中
  时间戳 + `five_aspect` 逐镜结构——这些时间戳这一轮改动前只在 CSV 里，
  没喂给 LLM 做切段依据，本轮 `build_file_context()` 补上了带时间戳的转写
  （`transcript.csv` 逐段 `start-end秒: text`，命中品牌的段落标
  `[命中品牌:xxx]`）和素材总时长。`classification_v2.csv` 新增
  `segment_summary` 列（`reuse:0-11.12秒|remake:11.12-23秒|drop:23-25.61秒`
  这种紧凑格式）。整条素材的 `level_v2` 判定逻辑不变，`segment_plan` 是它的
  细化执行计划，两者要逻辑自洽。
- **`remix_brief.py` 消费 `segment_plan`**：`differentiated_storyboard` 直接
  对齐 `segment_plan` 的 `reuse`/`remake` 边界（`remake` 段内部还可以按"时间段
  分档规则"再切细，但不能跨越 `segment_plan` 划的边界合并/拆散；`drop` 段不进
  storyboard，交给 `assemble.py --endcard` 处理）。每段新增 `source`
  字段（`"reuse"`/`"generated"`）；`reuse` 段新增 `reuse_cut`
  `{start_sec, end_sec}`（和 `segment_plan` 一致，精确到 0.1s）+ 保留原样的
  理由；`generated` 段照旧走"借结构换内容"。`seedance_prompt` 的「分时段
  描述：」小节**只覆盖 `generated` 段**——`reuse` 段不生成，不写进 prompt，
  不产生生成成本；`sample_shots` 也只从 `generated` 段里挑；`est_cost_usd.full`
  只按 `generated` 段总时长估算。`segment_plan` 为空数组时退化为旧行为（整条
  素材当一个 `generated` 段，等同于分段路由改动前的行为，向后兼容旧 brief）。
- **`assemble.py` 拼装成片**：按 `differentiated_storyboard` 顺序，`reuse`
  段从 `--source` 原片按 `reuse_cut` 精确切（重新编码，不用 `-c copy`，避免
  关键帧对不上导致切点漂移），`generated` 段从 `--gen-clip <时段>=<mp4路径>`
  按 `time_range`（0.5s 容差，同 `seedance_gen.py --shot` 的裁剪容差）找对应
  素材，缺了直接报错列出缺哪些段，不静默跳过、不拿别的段顶替、不执行任何
  ffmpeg 调用。每段先各自转码统一到 1080×1920@30fps（`scale+pad`
  保守方案：等比缩放后居中加黑边，不裁切）+ aac 48kHz，非首/尾段在片头/
  片尾各加 30ms 音频淡入淡出（防音爆），再用 `concat` filter（不是 concat
  demuxer）拼接成最终产物。`--endcard` 可选：mp4 直接 append，png 转 2s
  静帧视频再 append；不传时明确打印"缺尾帧，产物为无尾帧版本"，不假装有
  尾帧。拼完自动跑 `qc.py` 品牌预检，结论打印到 stdout（qc FAIL 不会让
  `assemble.py` 本身非零退出，qc 结果是给人看的信号）。

**良率闸门顺序**：prompt lint（静态免费）→ sample-first（先生成 2 镜过审再铺全片）→
qc.py 成片品牌复扫。设计依据见 adex `docs/growth/08-competitor-creative-pipeline.md` §4.3。

### seedance_gen.py（火山方舟 Ark 视频生成客户端）

调用火山方舟 Ark `doubao-seedance-2-0`（`POST/GET .../api/v3/contents/generations/tasks`），
请求体/端点/轮询方式对齐 adex `src/lib/platforms/seedance2.ts` 与
`src/app/api/seedance2/{generate,status}/route.ts` 的编排逻辑。

**红线（务必先读）**：

- **默认 dry-run**：不传 `--confirm` 绝不发起真实 API 调用，只打印将发送的完整请求体
  （含 prompt 全文、参数、参考素材清单）+ 预估成本，退出码 0。
- **条数/时长上限**：`config.SEEDANCE_MAX_CLIPS_PER_RUN = 2`、
  `config.SEEDANCE_MAX_DURATION_SEC = 12`。`--confirm` 真实调用时超限直接拒绝执行
  （`GuardrailError`，非零退出）；纯 `--dry-run` 下只警告不拦截，方便预览超限场景。
- **key 来源**：`config.get_ark_api_key()`，运行时经 `hakko-secret cc-volcengine-ark-key`
  获取，零打印零落盘。找不到 key 时如实报错退出，不伪造响应。
- **sample-first 门禁**：`--brief` 模式默认只允许生成 brief 里 `sample_shots` 列出的镜；
  要生成 `sample_shots` 之外的镜（含全片）需显式加 `--full`，仍受 `MAX_CLIPS` 限制。

**分时段裁剪逻辑**：brief 的 `seedance_prompt` 是整条素材（如 15.9s）的全片描述，其中
「分时段描述：」小节按 `N-M秒，...；...` 的格式逐段拆解各时间区间的画面内容。
`--shot <time_range>` 会解析该小节、按 0.5s 容差匹配 `sample_shots[].time_range`
对应的区间，只取该区间的文本作为这条 clip 的生成 prompt（不是把 15s 全片描述整段传给
一个 3s 的生成请求）。

**分时段解析容错（fallback）**：优先找字面小节标题「分时段描述：」（严格路径）；
真实批跑中出现过 LLM 把分时段内容揉进「动作与运动描述」小节、或干脆不用 `\n\n`
分隔小节的情况（两种失败模式都在真实批跑产出的 brief 里发生过），这时严格
路径会返回 `None`，`_extract_per_segment_texts()` 自动 fallback 到全文扫描
`N-M秒，` 模式（不限定小节），把落在同一时间区间（0.5s 容差）的文本跨小节聚合拼接。
`test_seedance_gen.py` 用固化自真实失败案例的文本验证 fallback
能正确裁出非空、非全文的子 prompt。

**参考素材**：Ark 请求体的 `image_url`/`video_url`/`audio_url` 字段值是 `{"url": "..."}`
（对齐 `ContentItem` 定义），只认公网可访问 URL，**没有 base64/binary 上传字段**。
`--ref-image/--ref-video/--ref-audio` 传本地文件路径会被明确拒绝并提示「需先上传到可公网
访问的存储」，不会静默忽略。

**真实调用路径**（`--confirm`）：创建任务 → 轮询（10s 间隔，10min 超时）→ 下载 mp4 到
`out/gen/<UTC时间戳>_<shot>.mp4` → 自动跑 `qc.py` 品牌预检 → 打印产物路径/任务 id/实际耗时 →
成本记账追加写入 `out/gen/spend_log.csv`（timestamp, shot, duration_sec, ratio,
generate_audio, n_ref_images/videos/audios, task_id, est_cost_usd）。

### worker.py / mock_adex.py（remix job 执行客户端）

adex control-plane 派活、本仓库执行的 worker 端。向 adex 的 `claim`/`report`/`upload`
三个接口发带 HMAC 签名的请求，认领一个 `RemixJob` 并跑完整条链路。

- **档位路由**：`job.tier` 三选一 —— `t0_5`（逐段 text2video 重生成）/ `t1`（带竞品切片
  作 @视频 运镜参照的重生成）/ `t2`（干净片段实切复用，不经生成）。其余值直接
  `WorkerError` 报错退出。T0（整条 text2video）不在本 worker 范围内，走 adex 的 in-app
  引擎。
- **画布尺寸**：由 `brief.ratio` 决定（`RATIO_CANVAS`：9:16 / 16:9 / 1:1 / 4:3 / 3:4），
  不写死竖版；拼装层复用 `assemble.py` 的转码/拼接/30ms 音频淡入淡出。
- **默认 dry-run**：不传 `--confirm` 时用 ffmpeg 合成占位片段，零成本零网络；
  `--confirm` 才复用 `seedance_gen.py` 发起真实 Ark 调用（仍受 `--max-clips` 限制）。
- **secret**：只来自 `--secret` 或环境变量 `WORKER_WEBHOOK_SECRET`，绝不打印/落盘。
  签名基串 `f"{timestamp}:{rawBody}"`；`upload` 接口特殊，用
  `f"{timestamp}:{content_sha256_hex}"`（二进制体不适合直接喂 HMAC）。
- **退出语义**：任何阶段抛异常都先尽力上报 `status=failed` + error 明细再 `exit 1`；
  `job=null`（没有可用任务）打印提示后 `exit 0`，不算失败。
- **本地验收**：`mock_adex.py` 用 stdlib `http.server` 复刻那三个接口，固定 secret
  `test`，内存里持有一个 3 镜假 job（hook/3s + scene/5s + end-card/2s）。`--tier` /
  `--ratio` 可切换要验的档位和画幅，收到 `status=succeeded` 时打印验收摘要。

## 已知取舍 / 限制

- **画面文字识别**：macOS 上用系统自带的 Apple Vision（`vision_ocr.swift`，首次使用时用 `swiftc` 编译到
  `~/.cache/creative-pipeline/`），其他平台回退 tesseract。2026-10 在 Cuddler / Hakko 真实成片上对比：
  烧录字幕（白字描边）tesseract 几乎全漏、Vision 基本全对；结尾风格化 logo Vision 6/6、tesseract 2/6；
  日文字幕只有 Vision 认得出；每帧约 70ms，tesseract 150–350ms，所以抽帧间隔统一为 1s。Vision 只按一个语言识别，
  preflight 按地区传语言（JP→ja），analyze.py / qc.py 默认英文。
- **whisper 模型**：用多语言 `ggml-base.bin`（`WHISPER_MODEL`）。没有 English-only 的 base.en 模型，
  英文素材也走多语言模型，转写效果可用。背景音乐下的轻声口播 base 偶尔漏听，做日文素材时再考虑换
  `large-v3-turbo`（文件名决定 DTW 预设，换模型不用改代码）。
- **whisper 时间戳**：段落 `offsets` 常卡在上一段结尾或整秒，比实际开口早 0.3–3 秒，段尾还会切掉最后一个词；
  按它剪会剪错。现在转写带 `-dtw <预设> -nfa`（DTW 必须关 flash attention），用词级时间戳校正段首段尾，
  32 段实测与人声能量起点相差中位 0.1 秒。需要 whisper-cpp ≥ 1.8。
- **whisper-cli 参数**：不能加 `-nt`（no-timestamps）。实测 `-nt` 会让 json 输出的每段
  `offsets` 退化成固定的 `0~30000ms` 占位值，不是真实分段时间戳；本项目转写时不传 `-nt`。
- **L3 判定不做**：见 classify.py 文件头注释，需要人工或未来接入 LLM 视觉判断。
- **指纹/去重**：用文件前 4MB 的 md5 + 时长拼接做简单指纹，不是真正的 phash（任务约束里
  不允许引入零标准库以外依赖，视觉感知哈希需要额外库，用这个轻量方案代替）。
