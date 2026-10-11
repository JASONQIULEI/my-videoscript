# 转文字工坊 · VideoScript

A local-first video-to-text tool with burned-in caption OCR, speech recognition, and TXT / Markdown / SRT / VTT export.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg)](https://www.python.org/downloads/)

将公开音视频链接或本地文件转换成带时间轴的文稿，可预览、复制及下载 TXT、Markdown、SRT、VTT。

默认先读取视频画面中的字幕；字幕不可用时使用本地语音识别。结果会注明实际来源和需复核的提示，不会把模型置信度当成文字准确率。

画面字幕识别使用本机 RapidOCR，语音识别使用 faster-whisper。无需商业转写 API Key，也不需要 Node.js 或前端构建步骤。OCR 模型由 RapidOCR 依赖提供。

> 本工具面向个人电脑本地使用。画面字幕识别目前主要适配视频底部、居中的白色中文字幕；其他字幕样式、无字幕视频及纯音频，建议选择「仅语音识别」。

## 快速开始

需要 **Git、Python 3.10–3.13、FFmpeg 和 ffprobe**，推荐 Python 3.12。完整实测环境为 macOS Apple Silicon + Python 3.12；Linux 可按以下步骤安装，但尚未完成同等端到端验收，Windows 暂未验收。

macOS（已安装 [Homebrew](https://brew.sh/)）：

```bash
brew install git python@3.12 ffmpeg
```

Ubuntu / Debian：

```bash
sudo apt update
sudo apt install git python3 python3-venv python3-pip ffmpeg
```

确认系统 Python 位于支持范围内。FFmpeg 也可从其[官方下载入口](https://ffmpeg.org/download.html)获取。项目内的 `runtime/bin/` 可放置适合当前系统的 FFmpeg 和 ffprobe；没有项目内组件时使用系统已安装的版本。运行程序和模型不随源码仓库分发。

下载项目并启动：

```bash
git clone https://github.com/JASONQIULEI/my-videoscript.git
cd my-videoscript
./start.sh
```

打开 <http://127.0.0.1:8000>。脚本在项目内创建 `venv/`，首次启动或依赖清单变更时安装依赖；之后直接启动。不会使用临时目录中的运行环境，也不会改变系统网络配置。

若 Python 未在搜索路径中，或需要更换端口：

```bash
PYTHON_BIN=/完整路径/python3.12 PORT=8001 ./start.sh
```

`PYTHON_BIN` 用于首次建立环境；已经存在的项目环境会继续使用。`PORT` 默认 `8000`，范围为 1–65535。

首次使用某个语音模型时会下载权重，需要能访问模型下载源的网络及足够磁盘空间。`large-v3-turbo` 权重约 1.6 GB，另需依赖、模型缓存和媒体文件空间。已准备好的 CTranslate2 模型可放在 `runtime/models/<模型名>/`，其中应包含 `model.bin`、配置及分词器文件；否则使用 faster-whisper 默认的模型下载与缓存。语音与字幕推理均在本机运行，CPU 可用，无需 GPU。

## 使用

1. 粘贴单个公开视频或音频链接，或上传文件；上传上限为 2 GiB。链接支持情况以 yt-dlp 能否公开访问、解析该条内容为准。
2. 选择识别方式：**字幕优先**尝试读取画面字幕；**仅语音识别**只识别音轨。无字幕、纯音频或字幕识别组件不可用时会回退语音，并显示原因。
3. 语音语言可选中文、英语或自动识别；这些语言选项针对语音识别，OCR 当前按中文字幕处理。语音模型默认 `large-v3-turbo`；可选择 `large-v3`、`medium`、`small`、`base` 或 `tiny`。
4. 可填写人名、公司名、行业术语，作为语音识别上下文。提示不会强制替换识别结果，也不保证提示中的词一定出现。
5. 完成后查看完整文稿、识别来源及需复核提示，可筛选待复核段落，再复制全文或下载。任务按顺序执行，减少大模型同时占用内存。处理中的任务不能删除。

下载纯音频时保留源格式，由 PyAV 解码，避免额外转换成 MP3。字幕优先模式会下载视频画面和音轨，优先选择 720p 以内的 AVC 视频，并用 FFmpeg 合并。

## 准确率如何验收

“处理完成”只表示已经生成文稿。模型的语言概率、OCR 置信度和低置信段数均**不是**准确率，不得据此显示“99%”。

对中文可使用字符错误率 CER：`(替换数 + 删除数 + 插入数) / 参考稿字符数`；字符准确率口径为 `1 - CER`。评测前应说明是否忽略空格、标点，以及是否进行繁简、数字或英文大小写归一化，避免调整规则掩盖识别错误。

99% 的结论必须来自独立核对的参考稿，说明测试视频、覆盖范围、对齐规则和样本量。一个视频或抽样片段达到目标，不代表其他视频或整个平台均达到相同水平。除字符指标外，应单独检查数字、姓名、公司名、否定关系和关键事实。

画面字幕模式输出的是原片字幕内容。原片可能省略口头语、漏写对白或本身存在错误，因此“与字幕一致”不等于“口播逐字准确”；若目标是逐字口播，参考稿必须通过原音核对。无字幕时的语音结果保留识别原文，系统不会用生成式润色来制造表面通顺。

## 文件位置

每个任务保存在 `data/jobs/<任务编号>/`：

- `job.json`：任务状态、完整文稿、来源及质量信息。
- `segments.json`：完整结构化结果和识别元数据，便于复核。
- `.txt`、`.md`、`.srt`、`.vtt`：四种导出格式。

网页预览不截断段数；中文标题文件可以正常下载。关闭网页不会终止后台任务，停止服务会中断正在处理的任务；重启后中断任务会标记为失败，可重新提交。删除已完成或失败任务会删除其本地文稿与上传文件。

上传文件、任务信息和文稿保存在本机。链接任务会访问对应媒体站点，首次安装及模型下载会访问依赖或模型服务；项目不把音视频发送给云端转写 API。`data/`、`runtime/`、虚拟环境、Cookie 和私人验收材料均已加入 `.gitignore`。

## 主要组件

- `backend.py`：本地 FastAPI 服务及任务队列。
- `pipeline.py`：下载、字幕优先 / 语音回退、质量标记及导出。
- `burned_captions.py`：原片画面字幕提取。
- `frontend/index.html`：任务界面。
- `evaluation/score.py`：独立参考稿的 CER 评测。
- `tests/`：自动回归测试。
- `docs/accuracy.md`：评测格式与适用范围。
- `runtime/bin/`：项目内媒体处理组件。
- `runtime/models/`：可选的本地语音模型。

本工具面向公开可访问内容，不自动读取浏览器 Cookie 或登录账号，不绕过付费或登录限制。登录、权限限制、站点风控和网络失败会作为任务错误显示；不承诺所有 B 站、YouTube 或其他站点链接都能直接转换。

服务采用单进程、串行任务队列，默认仅监听 `127.0.0.1`，没有账号鉴权。请保持本地运行，不要直接暴露到公网或添加多个 Uvicorn workers。

## API

启动后可查看 [交互式 API 文档](http://127.0.0.1:8000/docs)。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/jobs` | 提交 URL 或文件，使用 multipart 表单 |
| `GET` | `/api/jobs` | 获取任务列表 |
| `GET` | `/api/jobs/{id}` | 获取状态与完整文稿 |
| `GET` | `/api/jobs/{id}/download?format=txt` | 下载 `txt`、`md`、`srt` 或 `vtt` |
| `DELETE` | `/api/jobs/{id}` | 删除完成或失败任务 |

## 开发与回归检查

```bash
python3.12 -m venv venv
./venv/bin/python -m pip install -r requirements-dev.txt
./venv/bin/python -m unittest discover -s tests -v
```

现有 23 项自动测试覆盖任务接口、重启恢复、路径保护、中文文件下载、完整文稿、导出、时间戳、字幕回退、参数传递和 CER 规则。测试使用临时目录和模拟结果，不下载语音模型、不访问真实视频，也不代表实际视频准确率验收通过。

欢迎提交 Issue 和 Pull Request。排查问题时请提供系统、Python 版本、识别方式和经脱敏的错误信息；不要提交账号 Cookie、密钥、个人文稿或无权公开的媒体。

## 已验证范围与限制

一条约 13 分 54 秒的中文视频开发样本，按独立核对的画面字幕参考测得 **99.8263%**（4,605 个归一化字符、8 次字符编辑）。这仅表示该样本输出与画面字幕的文字一致程度，不等于纯语音逐字准确率、时间轴准确率或其他视频准确率。该视频也用于调试，不能视为独立留出测试集。

原始视频、完整字幕和私人验收材料未随仓库发布，因此自动测试不能复现这项视频分数。评分格式与规则见 [准确率说明](docs/accuracy.md)。

- OCR 当前依赖底部居中白色字幕的布局假设；仅识别出部分字幕，不保证整片对白覆盖完整。
- “字幕优先”在未取得可用字幕或处理出错时回退，不能保证自动判断出所有漏字。
- 纯语音仍可能有专名、数字或否定词错误；没有复核警告不代表没有错误。
- 较大模型和长视频耗时、占用内存更多；暂不支持任务暂停与断点续写。

## 常见问题

**找不到 FFmpeg / ffprobe**

安装 FFmpeg 后，确认 `ffmpeg -version`、`ffprobe -version` 均可运行；或检查 `runtime/bin/` 中的程序是否适合当前系统且具有执行权限。

**链接下载失败**

确认内容可公开访问，并查看任务显示的具体错误。站点变更可能需要更新 yt-dlp，更新后应重新测试。拥有内容使用权时也可上传本地文件。

**首次语音识别很慢或模型下载失败**

确认模型下载源可访问，或提前准备完整的本地模型目录。资源有限时可选择较小模型，但仍需核对转写质量。

**字幕样式不适配，或画面没有字幕**

改用「仅语音识别」。原片字幕与口播不一致时，应按最终用途选择参考来源。

## 开源许可与致谢

本项目代码以 [MIT License](LICENSE) 开源，Copyright (c) 2026 JASONQIULEI。

感谢 [FastAPI](https://github.com/fastapi/fastapi)、[yt-dlp](https://github.com/yt-dlp/yt-dlp)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[ONNX Runtime](https://github.com/microsoft/onnxruntime) 和 [FFmpeg](https://ffmpeg.org/)。第三方依赖、模型和处理的音视频内容仍适用各自的许可证或权利要求，不因本项目使用 MIT 而改变。
