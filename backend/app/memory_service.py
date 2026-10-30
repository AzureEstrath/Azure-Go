"""长期记忆服务：把配置变成 ``MemoryStore``，并负责召回文本与写入。

职责边界：
- ``memory_enabled=False`` 时**零开销**：不建数据库文件、不调任何接口（构造期直接返回）；
- embedding 由 ``build_embed_fn`` 按配置装配：**本地 GGUF（llama.cpp）/ OpenAI 兼容 HTTP /
  关闭**三态；本地加载失败可降级 HTTP，全部不可用则退回关键词检索（401 / 超时同样只影响
  检索质量，不影响出话）；
- ``recall_for`` 返回可直接注入提示词的文本（包装措辞在 ``prompts.long_term_note``）；
- ``EmbedProvider`` 带模型指纹、查询指令前缀、LRU 缓存与批量编码；``MemoryStore`` 的
  ``vectors`` 表按指纹隔离，启动后台回填当前模型向量；
- 数据库默认落在 ``<backend>/data/memory.db``（``backend/data/`` 已 gitignore，属运行产物），
  目录按需创建；路径留空也走这个默认值。

所有故障都不上抛：本模块只把 ``MemoryStore`` 的失败安全结果透传给编排层。
"""

import hashlib
import math
import os
import threading
from collections import OrderedDict

from langchain_openai import OpenAIEmbeddings

from .memory import MemoryStore, inject_text

# 默认库路径：<backend>/data/memory.db（backend/data/ 已 gitignore，属运行产物）
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "memory.db")

EMBED_TIMEOUT_SEC = 10.0        # 单次 embedding 超时（与工具层单次调用上限一致）；配置 embed_timeout 可收紧

# 本地 GGUF 模型按路径缓存（同一进程只加载一次；首次加载约 1 秒 / 609MB 模型）
_LOCAL_MODELS = {}
_LOCAL_LOCK = threading.Lock()


def _embed_timeout(cfg) -> float:
    """单次 embedding 超时：按 ``embed_timeout`` 收紧；缺字段 / 非正数 / 脏值退回 ``EMBED_TIMEOUT_SEC``。"""
    try:
        t = float(getattr(cfg, "embed_timeout", 0.0) or 0.0)
    except (TypeError, ValueError):
        return EMBED_TIMEOUT_SEC
    return t if t > 0.0 else EMBED_TIMEOUT_SEC


class EmbedProvider:
    """可调用的 embedding 提供者：``provider(text, query=False)``；可附带批量与模型指纹。

    ``model_id`` 写进向量表，用来防止「换了 embedding 模型仍拿旧向量召回」；
    ``query_prefix`` 只作用于查询（Qwen3-Embedding 之类的模型需要指令前缀时用）。
    """

    def __init__(self, fn, model_id="", query_prefix="", batch_fn=None, kind="http"):
        self._fn = fn
        self.model_id = str(model_id or "")
        self.query_prefix = str(query_prefix or "")
        self._batch_fn = batch_fn
        self.kind = str(kind or "")

    def __call__(self, text, query=False):
        prefix = self.query_prefix if query and self.query_prefix else ""
        try:
            return self._fn(prefix + str(text), query=query)
        except TypeError:                   # 旧替身只收一个参数
            return self._fn(prefix + str(text))

    def batch(self, texts, query=False):
        """批量 embedding；提供者没有批量实现时逐条回退。"""
        items = list(texts or [])
        if not items:
            return []
        prefix = self.query_prefix if query and self.query_prefix else ""
        if callable(self._batch_fn):
            try:
                return self._batch_fn([prefix + str(t) for t in items], query=query)
            except TypeError:
                return self._batch_fn([prefix + str(t) for t in items])
        return [self(t, query=query) for t in items]


def _make_local_provider(cfg):
    """本地 GGUF（llama.cpp）提供者：懒加载、L2 归一化、文本缓存、单模型锁。"""
    path = str(getattr(cfg, "embed_local_model", "") or "").strip()
    if path == "":
        return None
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        print("[Memory] 本地 embedding 模型不存在（%s），降级为关键词 / HTTP" % path)
        return None
    try:
        from llama_cpp import Llama
    except Exception as e:                  # noqa: BLE001 —— 未安装依赖时不影响记忆功能
        print("[Memory] 未安装 llama-cpp-python，本地 embedding 不可用（可继续用关键词）：%s" % e)
        return None

    model_id = str(getattr(cfg, "embed_model", "") or "").strip() or (
        "local:" + os.path.basename(path))
    cache_size = max(0, int(getattr(cfg, "embed_cache_size", 0) or 0))
    threads = max(1, int(getattr(cfg, "embed_local_threads", 8) or 8))
    n_ctx = max(64, int(getattr(cfg, "embed_local_ctx", 512) or 512))
    n_batch = max(1, int(getattr(cfg, "embed_batch_size", 16) or 16))
    gpu_layers = int(getattr(cfg, "embed_local_gpu_layers", 0) or 0)

    with _LOCAL_LOCK:
        got = _LOCAL_MODELS.get(path)
        if got is None:
            try:
                got = Llama(model_path=path, embedding=True, n_gpu_layers=gpu_layers,
                            n_ctx=n_ctx, n_threads=threads, n_batch=n_batch, verbose=False)
                _LOCAL_MODELS[path] = got
                print("[Memory] 本地 embedding 已加载：%s（gpu_layers=%d）" % (path, gpu_layers))
            except Exception as e:          # noqa: BLE001
                print("[Memory] 本地 embedding 加载失败（降级关键词）：%s" % e)
                return None
    llm = got
    call_lock = threading.Lock()
    cache = OrderedDict()
    cache_lock = threading.Lock()

    def _norm(vec):
        arr = [float(x) for x in vec]
        n = math.sqrt(sum(x * x for x in arr)) or 1.0
        return [x / n for x in arr]

    def _cache_key(text):
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    def _get_cached(text):
        if cache_size <= 0:
            return None
        key = _cache_key(text)
        with cache_lock:
            hit = cache.get(key)
            if hit is not None:
                cache.move_to_end(key)
            return list(hit) if hit is not None else None

    def _put_cached(text, vec):
        if cache_size <= 0:
            return
        key = _cache_key(text)
        with cache_lock:
            cache[key] = list(vec)
            cache.move_to_end(key)
            while len(cache) > cache_size:
                cache.popitem(last=False)

    def _embed_one(text):
        cached = _get_cached(text)
        if cached is not None:
            return cached
        with call_lock:
            out = llm.create_embedding(str(text))
        vec = _norm(out["data"][0]["embedding"])
        _put_cached(text, vec)
        return vec

    def _embed_many(texts):
        items = [str(t) for t in texts]
        missing = []
        for t in items:
            if _get_cached(t) is None and t not in missing:
                missing.append(t)
        if missing:
            with call_lock:
                out = llm.create_embedding(missing)
            for row in out.get("data", []):
                idx = int(row.get("index", 0))
                if 0 <= idx < len(missing):
                    _put_cached(missing[idx], _norm(row["embedding"]))
        return [_get_cached(t) or _embed_one(t) for t in items]

    return EmbedProvider(_embed_one, model_id=model_id,
                         query_prefix=str(getattr(cfg, "embed_query_prefix", "") or ""),
                         batch_fn=_embed_many, kind="local")


def _make_http_provider(cfg):
    """OpenAI 兼容 HTTP 提供者（也可以指向 llama-server / vLLM 的 /v1/embeddings）。"""
    url = str(getattr(cfg, "embed_base_url", "") or "").strip()
    model = str(getattr(cfg, "embed_model", "") or "").strip()
    key = str(getattr(cfg, "embed_api_key", "") or "").strip()
    if url == "" or model == "":
        return None
    if key == "":
        host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return None
        key = "sk-local"                     # 本机服务通常不校验 key，占位即可
    try:
        client = OpenAIEmbeddings(
            model=model,
            base_url=url,
            api_key=key,
            timeout=_embed_timeout(cfg),
            max_retries=0,
            check_embedding_ctx_length=False,
        )
    except Exception as e:                  # noqa: BLE001 —— 装配失败只降级，不抛回编排层
        print("[Memory] embedding 配置不可用（降级为关键词检索）：%s" % e)
        return None

    def embed(text, query=False):
        return [float(x) for x in client.embed_query(str(text))]

    return EmbedProvider(embed, model_id=model,
                         query_prefix=str(getattr(cfg, "embed_query_prefix", "") or ""),
                         kind="http")


def build_embed_fn(cfg):
    """按配置装配 embedding 提供者；不可用时返回 ``None``（记忆退化为关键词检索）。

    模式（v4.3.1，参考 MaiBot 的「本地模型 + 可降级 + 模型一致性」思路）：
    - ``local``：本地 GGUF（llama.cpp，默认 CPU，不抢主 LLM 显存）；
    - ``http``：OpenAI 兼容 ``/v1/embeddings``；
    - ``auto``：本地模型文件存在就用本地，否则完整 HTTP 配置就用 HTTP；
    - ``off``：明确关闭向量，只走关键词。

    任何装配失败都只返回 ``None`` / 降级，绝不让记忆把后端拖垮。
    """
    mode = str(getattr(cfg, "embed_mode", "auto") or "auto").strip().lower()
    if mode not in ("auto", "local", "http", "off"):
        mode = "auto"
    if mode == "off":
        return None
    local_path = str(getattr(cfg, "embed_local_model", "") or "").strip()
    can_local = local_path != "" and os.path.isfile(os.path.abspath(os.path.expanduser(local_path)))
    if mode in ("auto", "local") and can_local:
        got = _make_local_provider(cfg)
        if got is not None:
            return got
    # local 模式加载失败 / 模型文件缺失时，只要 HTTP 三件套可用也允许降级；
    # 这样“配置写 local、实际没装 llama.cpp”不会把记忆直接退回关键词。
    got = _make_http_provider(cfg)
    if got is not None:
        return got
    return None


class MemoryService:
    """长期记忆的门面：配置 → ``MemoryStore``，对外只暴露召回与写入两个动作。"""

    def __init__(self, cfg):
        """按配置装配；``memory_enabled=False`` 时直接返回（不建库、不调 embedding）。"""
        self.store = None                   # MemoryStore | None：Task 4 的查询 / 统计 / 删除直接用它
        self.embed_provider = None          # EmbedProvider | None：本地 GGUF / HTTP / 关键词降级
        self._top_k = max(0, int(getattr(cfg, "memory_top_k", 0) or 0))
        self._max_chars = max(0, int(getattr(cfg, "memory_max_inject_chars", 0) or 0))
        try:
            self._min_score = max(0.0, min(1.0, float(getattr(cfg, "memory_min_score", 0.0) or 0.0)))
        except (TypeError, ValueError):
            self._min_score = 0.0
        if not bool(getattr(cfg, "memory_enabled", False)):
            return
        db = str(getattr(cfg, "memory_db_path", "") or "").strip() or DEFAULT_DB_PATH
        try:
            d = os.path.dirname(os.path.abspath(db))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
        except Exception as e:              # noqa: BLE001 —— 目录建不出来由 MemoryStore 再兜一次
            print("[Memory] 数据目录不可用：%s" % e)
        provider = build_embed_fn(cfg)
        self.embed_provider = provider
        model_id = str(getattr(provider, "model_id", "") or "")
        self.store = MemoryStore(db, embed_fn=provider, embed_model=model_id)
        # v4.3.1（参考 MaiBot 的向量一致性 / 回填思路）：切到本地模型后，
        # 旧向量（别的模型 / 没向量）不算数；后台补齐当前模型的向量，不阻塞启动与聊天。
        if (provider is not None and self.store is not None
                and bool(getattr(cfg, "embed_backfill", True))):
            batch = max(1, int(getattr(cfg, "embed_batch_size", 16) or 16))
            t = threading.Thread(target=self._backfill_worker, args=(batch,),
                                 name="memory-embed-backfill", daemon=True)
            t.start()

    @property
    def available(self) -> bool:
        """功能是否启用（默认关闭时为 ``False``；库本身打不开时 store 内部还会降级）。"""
        return self.store is not None

    def _backfill_worker(self, batch_size=16) -> None:
        """后台补齐当前 embedding 模型的向量；只打日志，绝不把异常抛回启动/聊天路径。"""
        if self.store is None:
            return
        try:
            n = self.store.backfill_vectors(batch_size=batch_size)
            if n:
                print("[Memory] embedding 回填完成：%d 条" % n)
        except Exception as e:                  # noqa: BLE001
            print("[Memory] embedding 回填失败（已忽略，继续关键词检索）：%s" % e)

    def recall_for(self, query: str, k: int | None = None, mood=None) -> str:
        """召回最相关的几条记忆，拼成可注入的文本；关闭 / 无命中 / 任何故障返回 ``""``。

        ``k`` 缺省用 ``memory_top_k``（配置只能收紧）；召回在出话路径上，这里再兜一层
        异常保护——绝不让记忆把这一回合的 ``busy`` 卡住。``mood``（v4.3.0）会转给存储层，
        让「和此刻心情更接近的旧事」稍微靠前。
        """
        if self.store is None:
            return ""
        try:
            kk = self._top_k if k is None else max(0, int(k))
            if kk == 0:
                return ""
            return inject_text(self.store.recall(query, kk, mood=mood, min_score=self._min_score),
                               self._max_chars)
        except Exception as e:              # noqa: BLE001
            print("[Memory] 召回失败（已忽略）：%s" % e)
            return ""

    def recall_items_for(self, query: str, k: int | None = None, mood=None) -> list:
        """召回原始条目（供主动回忆选具体旧事）；关闭 / 无命中 / 故障返回 ``[]``。

        ``k`` 缺省用 ``memory_top_k``；主动回忆需要看到 ``id`` / ``text`` / ``score``，
        不能只拿拼好的注入字符串，否则无法去重与判断“这条是否值得提”。
        ``mood`` / ``emo_v`` / ``emo_a`` / ``emo_tag``（v4.3.0）会一起返回，
        让主动回忆能挑「和此刻心情相合」的那一条。
        """
        if self.store is None:
            return []
        try:
            kk = self._top_k if k is None else max(0, int(k))
            if kk == 0:
                return []
            rows = self.store.recall(str(query or ""), kk, mood=mood, min_score=self._min_score)
            return list(rows or [])
        except Exception as e:              # noqa: BLE001
            print("[Memory] 主动回忆召回失败（已忽略）：%s" % e)
            return []

    def remember_game(self, moves: list, result: str, summary: str) -> None:
        """落一条对局归档，并把总结另存为可召回的记忆。

        ``MemoryStore.save_game`` 只写 ``games`` 归档、不写 ``memories``；要让这盘的总结
        下次能被想起来，必须再写一条 ``kind="game"`` 的记忆（同 tag + 同 text 自动去重）。
        ``moves`` 原样交给 ``save_game``（它自己带异常保护），这里不预转换。
        """
        if self.store is None:
            return
        text = " ".join(str(summary or "").split())
        self.store.save_game(moves, str(result or ""), text)
        if text != "":
            self.store.write(text, kind="game", tag="game")

    def remember_fact(self, text: str, tag: str = "", meta: dict | None = None) -> int | None:
        """写一条显式事实 /玩家偏好，返回记忆 id；关闭或写失败返回 ``None``。

        ``tag='schedule'`` 且 meta 带 ``title`` / ``start_ts`` 时（v4.3.0，F）：
        同一天、同一标题的旧日程记忆会被自动标为 superseded（改时间 / 重排），
        新记忆成为唯一有效的那条。不同日期的同名课程不受影响。
        """
        if self.store is None:
            return None
        m = meta if isinstance(meta, dict) else None
        mid = self.store.write(text, kind="fact", tag=str(tag or ""), meta=m)
        if mid is not None and str(tag or "") == "schedule" and m:
            try:
                self.store.supersede_schedule_conflicts(m, keep_id=mid)
            except Exception as e:              # noqa: BLE001
                print("[Memory] 日程记忆对照失败（已忽略）：%s" % e)
        return mid

    def supersede_schedule_item(self, item) -> int:
        """日程被取消时使对应旧记忆失效（v4.3.0，F）；关闭 / 故障返回 0。"""
        if self.store is None:
            return 0
        try:
            return int(self.store.supersede_schedule_item(item) or 0)
        except Exception as e:                  # noqa: BLE001
            print("[Memory] 取消日程记忆失效失败（已忽略）：%s" % e)
            return 0

    def remember_cancellation(self, item) -> int | None:
        """把「某条日程已取消」写成一条不可再被当成有效安排的记忆（tag 独立，便于审计）。"""
        if self.store is None:
            return None
        it = item if isinstance(item, dict) else {}
        title = str(it.get("title", "") or "").strip()
        if title == "":
            return None
        when = str(it.get("when_text", "") or "").strip()
        text = "日程已取消：%s%s" % (when + " " if when else "", title)
        try:
            return self.store.write(
                text, kind="fact", tag="schedule:cancelled",
                meta={"schedule_id": int(it.get("id", 0) or 0), "title": title,
                      "start_ts": float(it.get("start_ts", 0) or 0)})
        except Exception as e:                  # noqa: BLE001
            print("[Memory] 取消记忆写入失败（已忽略）：%s" % e)
            return None

    # —— 桌宠 / 围棋共用的事件记忆（MaiBot 式「episode + 可召回摘要」的轻量实现） ——

    def recent_chats(self, limit: int = 50) -> list:
        """最近聊天流水（启动恢复 / 桌宠聊天面板共用）；关闭或失败返回 ``[]``。"""
        if self.store is None:
            return []
        try:
            return list(self.store.recent_chats(int(limit or 50)) or [])
        except Exception as e:                  # noqa: BLE001
            print("[Memory] 最近聊天读取失败（已忽略）：%s" % e)
            return []

    def remember_chat(self, user_text: str, reply_text: str, mode: str = "", mood=None) -> int | None:
        """记一轮聊天：写原文流水 + 一条可召回的 ``kind=chat`` 记忆。

        原文流水服务「最近聊了什么」的连续性；可召回记忆服务跨重启 / 跨模式的长期回忆。
        任何失败只打日志，绝不影响聊天回包。
        """
        if self.store is None:
            return None
        u = " ".join(str(user_text or "").split())
        r = " ".join(str(reply_text or "").split())
        if u == "" and r == "":
            return None
        m = str(mode or "")
        self.store.append_chat(u, r, mode=m, mood=mood if isinstance(mood, dict) else {})
        try:
            self.store.append_episode_turn(m, u, r, mood=mood if isinstance(mood, dict) else {})
        except Exception as e:                  # noqa: BLE001
            print("[Memory] episode 归并失败（已忽略）：%s" % e)
        text = "玩家：%s；Azure：%s" % (u[:100], r[:100])
        return self.store.write(
            text, kind="chat", tag="chat:%s" % str(mode or ""),
            meta={"mode": str(mode or ""), "mood": mood if isinstance(mood, dict) else {}})

    def recent_episodes(self, limit: int = 3) -> list:
        """最近 episode 摘要（启动 continuity / 调试查看用）；关闭或失败返回 ``[]``。"""
        if self.store is None:
            return []
        try:
            return list(self.store.recent_episodes(int(limit or 3)) or [])
        except Exception as e:                  # noqa: BLE001
            print("[Memory] episode 读取失败（已忽略）：%s" % e)
            return []

    def close_episode(self, source: str = "") -> None:
        """模式切换 / 退出前收束当前 episode，把这段相处写成可召回摘要。"""
        if self.store is None:
            return
        try:
            self.store.close_episode(str(source or ""))
        except Exception as e:                  # noqa: BLE001
            print("[Memory] episode 收束失败（已忽略）：%s" % e)

    def remember_event(self, kind: str, text: str, mode: str = "", mood=None, tag: str = "") -> int | None:
        """记一条桌宠 / 围棋的短事件（摸头、走神、窥屏搭话等）为可召回记忆。"""
        if self.store is None:
            return None
        body = " ".join(str(text or "").split())
        if body == "":
            return None
        tag2 = str(tag or ("%s:%s" % (kind, mode or "")))
        return self.store.write(
            body, kind=str(kind or "event"), tag=tag2,
            meta={"mode": str(mode or ""), "mood": mood if isinstance(mood, dict) else {}})

    def close(self) -> None:
        """关闭底层库；可重复调用。"""
        if self.store is not None:
            self.store.close()
