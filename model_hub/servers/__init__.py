"""模型服务的执行器（全部集中在此，不再散落在 ``scripts/`` 与插件目录）。

- ``embed_server.py``：向量嵌入 sidecar（stdlib HTTP，加载 bge-m3 吐向量）。
- ``voice_engine.py``：语音引擎常驻服务（TTS/STT/sing，OpenAI 音频兼容形状）。

两者都由 ``model_hub.services.ServiceManager`` 按 ``~/.omniagent/model_hub.yaml``
拉起，统一以 ``python -m model_hub.servers.<name> ...`` 运行。
"""
