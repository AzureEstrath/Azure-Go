"""去本地化后的配置面与 LLM 客户端形状（对应决策 D3）。"""

import importlib.util
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AzureConfig
from app.llm import LLMClient

_REMOVED_CONFIG_FIELDS = ["llm_auto_start", "llm_port", "llm_exe", "llm_model_path"]


class DelocalizedConfigTest(unittest.TestCase):
    def test_local_service_fields_removed(self):
        c = AzureConfig()
        for name in _REMOVED_CONFIG_FIELDS:
            self.assertFalse(hasattr(c, name), "配置项应已删除：%s" % name)

    def test_legacy_config_keys_are_ignored(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"llm_exe": "D:/x/llama-server.exe", "llm_port": 8080,
                           "llm_base_url": "https://api.example.com/v1"}, f)
            c = AzureConfig.load(p)
        self.assertEqual(c.llm_base_url, "https://api.example.com/v1")
        for name in _REMOVED_CONFIG_FIELDS:
            self.assertFalse(hasattr(c, name), name)


class DelocalizedLLMTest(unittest.TestCase):
    def test_no_service_watch(self):
        self.assertNotIn("relaunch_cb", inspect.signature(LLMClient.__init__).parameters)
        self.assertFalse(hasattr(LLMClient(), "watch_loop"))

    def test_services_module_removed(self):
        self.assertIsNone(importlib.util.find_spec("app.services"))


if __name__ == "__main__":
    unittest.main()
