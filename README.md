# 🎙️ AstrBot IndexTTS 插件

> 基于本地 [IndexTTS](https://www.xcnahida.cn/?p=ey8AUxey) 的文本转语音插件，支持**音色克隆**与 **LLM 回复自动转语音**。

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/平台-AstrBot-orange?style=flat-square" alt="AstrBot">
  <img src="https://img.shields.io/badge/引擎-IndexTTS-green?style=flat-square" alt="IndexTTS">
  <img src="https://img.shields.io/badge/许可-MIT-lightgrey?style=flat-square" alt="License">
</p>

---

## 📁 插件结构

```
astrbot_plugin_indextts/
├── metadata.yaml         # 插件元数据
├── requirements.txt      # 依赖: gradio_client, httpx
├── main.py               # 插件核心代码
└── data/                 # 运行时自动创建
    ├── config.json       # 配置文件
    ├── voices/           # 用户个人参考音频
    └── generated/        # 生成的 TTS 音频
```

## ⌨️ 支持的命令

| 命令 | 说明 |
|---|---|
| `/tts <文本>` | 文本转语音（使用已设置的音色） |
| `/tts_voice <URL 或路径>` | 设置个人参考音频，实现音色克隆 |
| `/tts_reset_voice` | 重置个人音色，恢复默认 |
| `/tts_params` | 查看当前 TTS 配置和连接状态 |

## 🚀 安装方法

将插件目录复制到 AstrBot 的 `plugins` 目录，然后在 WebUI 插件管理页面点击**重载插件**：

```bash
cp -r astrbot_plugin_indextts /path/to/AstrBot/data/plugins/
```

## ⚙️ 配置说明

首次启动后，编辑自动生成的 `data/config.json`：

```json
{
    "index_tts_url": "http://host.docker.internal:7860/",
    "default_reference_audio": "D:/IndexTTS/index-tts/prompts/your_voice.wav",
    "auto_capture_voice": false,
    "infer_mode": "批次推理"
}
```

### 关键配置项

| 配置项 | 说明 |
|---|---|
| `default_reference_audio` | 默认音色克隆参考音频路径（**必填**，否则用户需各自用 `/tts_voice` 设置） |
| `auto_capture_voice` | 设为 `true` 后，用户发送的语音消息会自动保存为该用户的参考音色 |
| `index_tts_url` | IndexTTS Gradio 服务地址 |

> 💡 **关于地址配置**：如果 AstrBot 在 Docker 中运行而 IndexTTS 在宿主机上，使用 `http://host.docker.internal:7860/`；如果两者在同一网络环境中，改为 `http://127.0.0.1:7860/`。

## 🎯 使用流程

1. 启动 IndexTTS（运行 `启动程序.bat`）
2. 配置默认参考音频，或让用户通过 `/tts_voice <URL>` 设置自己的音色
3. 发送 `/tts 你好世界` 即可生成克隆语音

## 🛠️ 技术要点

- 通过 `gradio_client` 调用 IndexTTS 的 `/gen_single` API
- 使用 `asyncio.to_thread()` 包装同步调用，避免阻塞 AstrBot 事件循环
- 支持自动重连：IndexTTS 服务断开后，下次调用自动尝试重连
- 音频以 WAV 格式通过 `Comp.Record` 组件发送，兼容 QQ / NapCat

---

<p align="center">
  <sub>这是作者的第一个作品（AI 主力辅助），欢迎各位大佬指导 🎉</sub>
</p>
