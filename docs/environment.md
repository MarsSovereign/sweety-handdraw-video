# 环境检查表

适用 v1.3。支持 macOS 与 Linux，宿主为 Codex 与 Claude Code；Windows 暂不承诺。安装 Skill、准备本地运行环境、准备图像生成能力、部署声音模型是不同步骤。

## 一条命令准备与检查

```bash
bash <skill 目录>/scripts/setup.sh          # 安装 FFmpeg、中文字体（Linux）、Python 虚拟环境与依赖
bash <skill 目录>/scripts/setup.sh --check  # 只检查，不安装
python <skill 目录>/scripts/handdraw_video.py doctor JOB
```

Skill 目录：Claude Code 为 `~/.claude/skills/sweety-handdraw-video`；Codex 为 `~/.agents/skills/sweety-handdraw-video` 或 `~/.codex/skills/sweety-handdraw-video`。虚拟环境默认在 `~/.local/share/sweety-handdraw-video/venv`，不写进 Skill 目录。只有显式传入 `--voxcpm` 才会下载约 3.2GB 的本地配音模型（仅 Apple Silicon）。

`doctor` 把能力拆开报告，并对每个缺失项给出修复命令：

| 字段 | 含义 |
|---|---|
| `capabilities.render` | FFmpeg、字体覆盖、画面输入与美术记录均就绪，可以渲染画面（不看旁白） |
| `capabilities.generate_narration` | 本地 VoxCPM2 模型已安装、VoiceStudio 可连通，或用户配置了自己的云端 TTS 密钥 |
| `voice_backends` | 逐项列出本地 VoxCPM2、VoiceStudio、降噪 / ASR 工具和云端密钥是否就绪（只报有无，不输出密钥） |
| `capabilities.narration_ready` | 旁白音频与对齐文件已就绪；与 `render` 同时为真时 `ready` 才为真 |
| `capabilities.generate_images` | 脚本无法探测宿主是否能生图，只给提示，由 Agent 自行确认 |
| `fonts` | 手写、字幕、水印各自选中的字体，或缺字说明 |
| `fixes` | 缺失项对应的安装或配置命令 |

## 制作前让 Agent 逐项检查

```text
检查 sweety-handdraw-video 的当前版本和完整目录。
运行 setup.sh --check 和 doctor，报告 render / narration 能力与 fixes。
确认本任务能调用图像生成；Claude Code 需要已接入的生图工具 / MCP，
或由我提供插画。都没有时停下说明，不做只有文字的视频。
确认一条真实可用的配音路径：本地 VoxCPM2、兼容 VoiceStudio，
我自己配置密钥的火山豆包 / Gemini TTS，或我提供的完整旁白和时间戳。
不自动下载大模型，不购买服务。
环境通过后，使用我的内容先做一个代表场景，再完成视频。
```

## 各部分分别需要什么

| 部分 | 依赖 | 不是这一步提供的东西 |
|---|---|---|
| Skill 安装 | `npx skills add`，或手动复制完整目录；手部、画风预览、试听已随包提供 | 不包含 Agent 订阅、生图权限或 TTS 模型权重 |
| 本地渲染 | Python 3.9+；Pillow / NumPy / SciPy / requests / fontTools；FFmpeg/ffprobe | 不负责替你开通模型服务 |
| 文字 | macOS 自带 Hannotate、冬青黑体等；Linux 用 fontconfig + Noto CJK（`setup.sh` 自动装）；可选霞鹜文楷作手写字 | 系统商业字体不随包分发 |
| 字形覆盖 | 构建前按手写、字幕、水印逐字检查；缺字时依次换备用字体，全部缺字则构建失败并列出缺失字符与已尝试字体 | 不会用方框凑合出片；明确指定的字体不会被替换 |
| 图像隐私 | 内置纯 Python 清理（`scripts/clean_image.py`），不需要 Bun 或 ExifTool | 清理元数据不保证通过 AI 图片检测 |
| 图像生成 | Codex 内置生图；Claude Code 需要生图工具 / MCP 或用户提供的插画 | 仅有文字能力时不能完成新插画 |
| VoxCPM2 适配器 | 当前为 Apple Silicon / MLX 路径；已安装模型 | 不能直接当成 CUDA / Windows 的部署教程 |
| VoiceStudio 适配器 | 兼容的本机 HTTP API 与已配置模型 | 同名桌面软件不必然兼容 |
| 现成旁白 | 已检查的音频、全文与时间戳 | 没有时间戳时，仍须完成对齐 |
| 云端 TTS（可选） | 用户自己的火山引擎或 Gemini 密钥，放在环境变量 | 不是默认，本地失败时不自动改用；本仓库只做了模拟 HTTP 测试，未真实调用 |
| 可选声音清理 | 已安装的人声分离 / 降噪模型 | 不保证不损伤音色，需要前后试听 |

## 硬件与支持状态

- 完整样片制作环境：作者的 M3 Max、36GB 内存、macOS、Codex、本地声音工具。
- Linux：Ubuntu 24.04 容器内通过 `setup.sh`、全部回归、离线短片合成与安装包核验，字体自动选用 Noto Sans CJK SC；CI 在 ubuntu-latest 与 macos-latest 上走同一套 `setup.sh`。本地 MLX 配音不适用于 Linux，需用 VoiceStudio、现成旁白或自己密钥的云端 TTS。
- Claude Code：已验证 `npx skills add --agent claude-code` 安装与本地脚本；整片流程依赖所接入的生图能力。
- VoxCPM2-8bit 权重约 3.23GB；这不是最低内存。8GB / 16GB 机器未在本项目中完成最低配置测试。
- 已有图片、核对后的声音与时间戳，可以减少生成步骤；能否完全离线取决于这些输入是否齐全。

## 常见卡点

| 现象 | 优先检查 |
|---|---|
| 能写脚本，不能出图 | 宿主是否真的有可用的图像生成能力；Claude Code 是否接入了生图工具 |
| VoiceStudio 连接失败 | 本机服务是否运行，端口和接口是否匹配 |
| VoxCPM2 import 失败 | 是否使用装有 MLX / mlx-audio 的专用 Python，而不是渲染虚拟环境 |
| 构建报 Font coverage check failed | 安装覆盖缺失字符的字体，或在 `font` / `captions.font` / `watermark.font` 指定字体 |
| 声音尾句缺失 | 完整母带是否漏读，不能只延长静帧掩盖缺字 |
| 没配乐仍有杂音 | 检查原始参考与生成旁白本身，而不只查混音配置 |
| 指定片尾声音不可选 | 是否导入了真实原片及准确逐字稿，不能拿旧试听代替 |
| 内置素材缺失或损坏 | 会自动按镜像顺序修复；网络受限时设置 `HANDDRAW_MIRROR`，或按报错提示手动下载到指定路径 |
| 安装后选项较少 | 检查 VERSION；公开旧包不包含本地未发布改动 |

详细 API、参数与导入命令见 [本地运行环境](../skills/sweety-handdraw-video/references/local-runtime.md)，素材托管见 [托管与缓存](../skills/sweety-handdraw-video/references/hosted-assets.md)。

女声听感暂未最终验收；原片录音保留在作者本地。
