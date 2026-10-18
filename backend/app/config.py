"""后端配置（对应 ``AzureConfig.gd`` 中与后端相关的字段）。

前后端分离后，config.json 仍只有一份：客户端只读它需要的（渲染 / 语音 TTS / VRM 等），
后端只读引擎与 LLM、提示词自定义部分。这里只保留后端需要的字段，避免两边职责重叠。

KataGo 由独立的云服务提供（见 ``katago_service/``）：这里只存服务地址、鉴权 token 与超时，
不再解析引擎可执行文件 / 模型 / 配置的路径。
"""

import json
import os


class AzureConfig:
    def __init__(self):
        self.katago_url = "http://127.0.0.1:8100"   # KataGo 云服务地址（与 katago_service 默认端口一致）
        self.katago_token = ""                      # Bearer token；留空＝不鉴权（本机回环调试）
        self.katago_timeout_ms = 30000              # 单次 /analyze 超时
        self.max_visits = 500
        self.joseki_teach = True
        self.joseki_rate = 0.5                      # 定式提示频率（0~1）：与开关同一格；门控唯一路径 p=clamp(rate+adj,0,1)
        self.llm_base_url = "https://api.deepseek.com/v1"   # 云端 OpenAI 兼容地址（与 game/config.json 模板一致）
        self.llm_model = "deepseek-chat"
        self.llm_api_key = ""
        self.llm_think_mode = 0
        # —— 请求体扩展字段（透传给 OpenAI 兼容服务，如 top_k / reasoning_effort）——
        self.llm_extra_body = {}            # 始终并入
        self.llm_think_extra_body = {}      # 仅思考档位开启（think_mode > 0）时并入
        # —— 发言侧工具（她自己查盘/复算/查定式；只影响怎么说，不影响下哪里）——
        self.tools_enabled = False          # 默认关闭：行为与没有工具时完全一致
        self.max_tool_calls = 2             # 一次发言的工具次数上限（agent 侧还有硬上限）
        # —— 流式输出（默认关闭）：开启后 speak 阶段逐块推 `token` 事件，最终仍以完整 message 收口 ——
        self.stream_tokens = False
        # —— 长期记忆（跨局记得玩家；默认关闭，关闭时不建库、不调 embedding）——
        self.memory_enabled = False
        self.memory_db_path = ""            # 留空＝<backend>/data/memory.db（由 memory_service 决定）
        self.embed_base_url = ""            # 云端 OpenAI 兼容 embedding 地址；三者任一为空则降级为关键词检索
        self.embed_model = ""
        self.embed_api_key = ""
        self.memory_top_k = 3               # 每次召回条数上限
        self.memory_max_inject_chars = 240  # 注入提示词的总字数上限（单条另截到 120 字）
        self.embed_timeout = 10.0           # 单次 embedding 超时（秒）；可收紧，memory_service 用它装配云端客户端
        # —— 云端语音合成（后端合成 + 缓存；Task 2 起）：默认关闭、默认系统语音兜底 ——
        self.tts_enabled = False            # 关闭时零开销：不发请求、不发 speech 事件，文字照常
        self.tts_mode = "system"            # "openai"＝云端合成；"system"＝让前端用系统语音念
        self.tts_base_url = ""              # OpenAI 兼容地址；openai 模式未配置时视为未启用
        self.tts_model = "tts-1"
        self.tts_api_key = ""               # 留空＝不带 Authorization（本机回环调试）
        self.tts_voice = ""
        self.tts_speed = 1.0
        self.tts_volume = 1.0
        self.tts_timeout_sec = 30           # 单次合成超时；超时整条降级为系统语音
        self.tts_long_chunk = False         # 打开后单块字数上限放宽（templates 里该键已在）
        # —— 提示词自定义（留空则用内置 Azure 人设）——
        self.prompt_system = ""
        self.prompt_user_name = "Estarth"
        self.prompt_phase_notes = {}

        self._path = ""
        self._raw = {}

    @classmethod
    def load(cls, path=None):
        c = cls()
        if path is None:
            env = os.environ.get("AZURE_CONFIG", "")
            if env and os.path.isfile(env):
                path = env
            else:
                for cand in (os.path.join(os.getcwd(), "config.json"),
                             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")):
                    if os.path.isfile(cand):
                        path = cand
                        break
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                d = {}
            if isinstance(d, dict):
                c._path = path
                c._raw = d
                c._apply(d)
        return c

    def _apply(self, d):
        self.katago_url = str(d.get("katago_url", self.katago_url))
        self.katago_token = str(d.get("katago_token", self.katago_token))
        self.katago_timeout_ms = int(d.get("katago_timeout_ms", self.katago_timeout_ms))
        self.max_visits = int(d.get("max_visits", self.max_visits))
        self.joseki_teach = bool(d.get("joseki_teach", self.joseki_teach))
        try:
            self.joseki_rate = max(0.0, min(1.0, float(d.get("joseki_rate", self.joseki_rate))))
        except (TypeError, ValueError):     # 脏值退回默认：不因一个旋钮把启动搞崩
            self.joseki_rate = 0.5
        self.llm_base_url = str(d.get("llm_base_url", self.llm_base_url))
        self.llm_model = str(d.get("llm_model", self.llm_model))
        self.llm_api_key = str(d.get("llm_api_key", self.llm_api_key))
        if "llm_think_mode" in d:
            self.llm_think_mode = max(0, min(2, int(d.get("llm_think_mode", self.llm_think_mode))))
        else:
            self.llm_think_mode = 2 if bool(d.get("llm_think_strong", False)) else 0   # 旧键（bool）迁移
        eb = d.get("llm_extra_body")
        if isinstance(eb, dict):
            self.llm_extra_body = dict(eb)
        tb = d.get("llm_think_extra_body")
        if isinstance(tb, dict):
            self.llm_think_extra_body = dict(tb)
        self.tools_enabled = bool(d.get("tools_enabled", self.tools_enabled))
        self.max_tool_calls = max(0, int(d.get("max_tool_calls", self.max_tool_calls)))
        self.stream_tokens = bool(d.get("stream_tokens", self.stream_tokens))
        self.memory_enabled = bool(d.get("memory_enabled", self.memory_enabled))
        self.memory_db_path = str(d.get("memory_db_path", self.memory_db_path))
        self.embed_base_url = str(d.get("embed_base_url", self.embed_base_url))
        self.embed_model = str(d.get("embed_model", self.embed_model))
        self.embed_api_key = str(d.get("embed_api_key", self.embed_api_key))
        self.memory_top_k = max(0, int(d.get("memory_top_k", self.memory_top_k)))
        self.memory_max_inject_chars = max(0, int(d.get("memory_max_inject_chars", self.memory_max_inject_chars)))
        try:
            self.embed_timeout = max(0.0, float(d.get("embed_timeout", self.embed_timeout)))
        except (TypeError, ValueError):     # 脏值退回默认：不因一个旋钮把启动搞崩
            self.embed_timeout = 10.0
        self.tts_enabled = bool(d.get("tts_enabled", self.tts_enabled))
        self.tts_mode = str(d.get("tts_mode", self.tts_mode))
        self.tts_base_url = str(d.get("tts_base_url", self.tts_base_url))
        self.tts_model = str(d.get("tts_model", self.tts_model))
        self.tts_api_key = str(d.get("tts_api_key", self.tts_api_key))
        self.tts_voice = str(d.get("tts_voice", self.tts_voice))
        try:
            self.tts_speed = max(0.0, float(d.get("tts_speed", self.tts_speed)))
        except (TypeError, ValueError):     # 脏值退回默认：不因一个旋钮把后端启动搞崩
            self.tts_speed = 1.0
        try:
            self.tts_volume = max(0.0, float(d.get("tts_volume", self.tts_volume)))
        except (TypeError, ValueError):
            self.tts_volume = 1.0
        try:
            self.tts_timeout_sec = max(1.0, float(d.get("tts_timeout_sec", self.tts_timeout_sec)))
        except (TypeError, ValueError):
            self.tts_timeout_sec = 30.0
        self.tts_long_chunk = bool(d.get("tts_long_chunk", self.tts_long_chunk))
        self.prompt_system = str(d.get("prompt_system", self.prompt_system))
        self.prompt_user_name = str(d.get("prompt_user_name", self.prompt_user_name))
        pn = d.get("prompt_phase_notes")
        if isinstance(pn, dict):
            self.prompt_phase_notes = dict(pn)

    def has_katago(self) -> bool:
        """是否**显式配置**了 KataGo 服务地址（判据：``katago_url`` 非空）。

        注意「留空」不等于「不接入」：引擎客户端会把空地址回退到 ``DEFAULT_URL``
        （``http://127.0.0.1:8100``，见 ``engine.py``），装配处照样会构造它并 ``start()``。
        所以本判据只回答「用户有没有写地址」，**不代表** KataGo 相关功能已关闭。
        """
        return bool(self.katago_url)
