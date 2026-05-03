# 基于本地 IndexTTS 的文本转语音astrbot插件，支持音色克隆和自动将 LLM 回复转为语音

IndexTTS的安装方法详见：https://www.xcnahida.cn/?p=ey8AUxey

###   插件结构

  astrbot_plugin_indextts/
  ├── metadata.yaml       # 插件元数据
  ├── requirements.txt    # 依赖: gradio_client, httpx
  ├── main.py             # 插件核心代码
  └── data/               # 运行时自动创建
      ├── config.json     # 配置文件
      ├── voices/         # 用户个人参考音频
      └── generated/      # 生成的 TTS 音频

### 支持的命令

  ┌────────────────────────┬────────────────────────────────┐
  │          命令           │              说明               │
  ├────────────────────────┼────────────────────────────────┤
  │ /tts <文本>             │ 文本转语音（使用已设置的音色）       │
  ├────────────────────────┼────────────────────────────────┤
  │ /tts_voice <URL或路径>  │ 设置个人参考音频，实现音色克隆       │
  ├────────────────────────┼────────────────────────────────┤
  │ /tts_reset_voice       │ 重置个人音色，恢复默认             │
  ├────────────────────────┼────────────────────────────────┤
  │ /tts_params            │ 查看当前 TTS 配置和连接状态        │
  └────────────────────────┴────────────────────────────────┘

###   安装方法

将插件目录复制到 AstrBot 的 plugins 目录：

cp -r /d/IndexTTS/astrbot_plugin_indextts /path/to/AstrBot/data/plugins/

然后在 AstrBot WebUI 的插件管理页面点击 重载插件。

###   配置说明

由于本人的情况是astrbot在docker中运行，而IndexTTS服务运行在主机上，所以config.json文件中默认的是：
"index_tts_url": "http://host.docker.internal:7860/",
如果astrbot和IndexTTS运行在同一网络环境中（大多数人的情况）需要把：
"index_tts_url": "http://host.docker.internal:7860/",
改为：
"index_tts_url": "http://127.0.0.1:7860/",
首次启动后，编辑 data/config.json（自动生成）设置默认参考音频：
{
    "index_tts_url": "http://host.docker.internal:7860/",
    "default_reference_audio": "D:/IndexTTS/index-tts/prompts/your_voice.wav",
    "auto_capture_voice": false,
    "infer_mode": "批次推理",
    ...
}

###   关键配置项：
  - default_reference_audio: 设置默认的音色克隆参考音频路径（必填，否则用户需各自用 /tts_voice 设置）
  - auto_capture_voice: 设为 true 后，用户发送的语音消息会自动保存为该用户的参考音色
  - index_tts_url: IndexTTS Gradio 服务地址，默认 http://host.docker.internal:7860/

###   使用流程

  1. 确保 IndexTTS 已启动（运行 启动程序.bat）（本人技术有限，暂时只能这样了）
  2. 配置默认参考音频，或让用户通过 /tts_voice <URL> 设置自己的音色
  3. 发送 /tts 你好世界 即可生成克隆语音

###   技术要点

  - 通过 gradio_client 调用 IndexTTS 的 /gen_single API，与现有 index_tts.py 适配器使用相同方式
  - 使用 asyncio.to_thread() 包装同步 Gradio 调用，避免阻塞 AstrBot 事件循环
  - 支持自动重连：当 IndexTTS 服务断开后，下次调用会自动尝试重连
  - 音频以 WAV 格式通过 Comp.Record 组件发送，与 QQ/NapCat 兼容

另：本人是初学者，这是我的第一个作品（AI是主力），难免会有很多问题，欢迎各位大佬指导QQ：2446548274