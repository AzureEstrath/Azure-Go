"""后端配置（对应 ``AzureConfig.gd`` 中与后端相关的字段）。

前后端分离后，config.json 仍只有一份：客户端只读它需要的（渲染 / 语音 TTS / VRM 等），
后端只读引擎与 LLM、提示词自定义部分。这里只保留后端需要的字段，避免两边职责重叠。

相对路径解析顺序：config.json 所在目录 → backend/ → D:/KataGo（前端不再解析路径，这一条只服务后端的引擎配置）。
"""

import json
import os

_DEFAULT_DIRS = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "D:/KataGo"]


class AzureConfig:
    def __init__(self):
        self.katago_bin = r"D:\KataGo\bin\katago\katago.exe"
        self.katago_model = r"D:\KataGo\models\model-b18c384nbt.bin.gz"
        self.katago_config = r"D:\KataGo\bin\katago\default_gtp.cfg"
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
        c._resolve_paths(os.path.dirname(os.path.abspath(path)) if path else "")
        return c

    def _apply(self, d):
        self.katago_bin = str(d.get("katago_bin", self.katago_bin))
        self.katago_model = str(d.get("katago_model", self.katago_model))
        self.katago_config = str(d.get("katago_config", self.katago_config))
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

    def _resolve_paths(self, exe_dir):
        if exe_dir:
            self.katago_bin = _resolve(self.katago_bin, exe_dir)
            self.katago_model = _resolve(self.katago_model, exe_dir)
            self.katago_config = _resolve(self.katago_config, exe_dir)

    def has_paths(self) -> bool:
        """KataGo 三件套是否用户显式配置过（都指向存在的文件）。"""
        return all(os.path.isfile(p) for p in (self.katago_bin, self.katago_model, self.katago_config))


def _resolve(p, exe_dir):
    if os.path.isabs(p):
        return p
    for base in ([exe_dir] if exe_dir else []) + _DEFAULT_DIRS:
        cand = os.path.join(base, p)
        if os.path.isfile(cand):
            return cand
    return os.path.join(exe_dir, p) if exe_dir else p
