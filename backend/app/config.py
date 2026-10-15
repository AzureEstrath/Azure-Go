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
        self.llm_base_url = "https://api.deepseek.com/v1"   # 云端 OpenAI 兼容地址（与 game/config.json 模板一致）
        self.llm_model = "deepseek-chat"
        self.llm_api_key = ""
        self.llm_think_mode = 0
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
        self.llm_base_url = str(d.get("llm_base_url", self.llm_base_url))
        self.llm_model = str(d.get("llm_model", self.llm_model))
        self.llm_api_key = str(d.get("llm_api_key", self.llm_api_key))
        if "llm_think_mode" in d:
            self.llm_think_mode = max(0, min(2, int(d.get("llm_think_mode", self.llm_think_mode))))
        else:
            self.llm_think_mode = 2 if bool(d.get("llm_think_strong", False)) else 0   # 旧键（bool）迁移
        self.prompt_system = str(d.get("prompt_system", self.prompt_system))
        self.prompt_user_name = str(d.get("prompt_user_name", self.prompt_user_name))
        pn = d.get("prompt_phase_notes")
        if isinstance(pn, dict):
            self.prompt_phase_notes = dict(pn)

    def has_katago(self) -> bool:
        """KataGo 云服务地址是否已配置（留空即视为未接入，引擎相关功能自行降级）。"""
        return bool(self.katago_url)
