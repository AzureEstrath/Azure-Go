"""长期记忆服务：把配置变成 ``MemoryStore``，并负责召回文本与写入。

职责边界：
- ``memory_enabled=False`` 时**零开销**：不建数据库文件、不调任何接口（构造期直接返回）；
- 云端 embedding 由 ``build_embed_fn`` 按配置装配，三个字段任一为空就返回 ``None``——
  ``MemoryStore`` 随即降级为关键词检索；401 / 超时同样只影响检索质量，不影响出话；
- ``recall_for`` 返回可直接注入提示词的文本（包装措辞在 ``prompts.long_term_note``）；
- 数据库默认落在 ``<backend>/data/memory.db``（``backend/data/`` 已 gitignore，属运行产物），
  目录按需创建；路径留空也走这个默认值。

所有故障都不上抛：本模块只把 ``MemoryStore`` 的失败安全结果透传给编排层。
"""

import os

from langchain_openai import OpenAIEmbeddings

from .memory import MemoryStore, inject_text

# 默认库路径：<backend>/data/memory.db（backend/data/ 已 gitignore，属运行产物）
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "memory.db")

EMBED_TIMEOUT_SEC = 10.0        # 单次 embedding 超时（与工具层单次调用上限一致）；配置 embed_timeout 可收紧


def _embed_timeout(cfg) -> float:
    """单次 embedding 超时：按 ``embed_timeout`` 收紧；缺字段 / 非正数 / 脏值退回 ``EMBED_TIMEOUT_SEC``。"""
    try:
        t = float(getattr(cfg, "embed_timeout", 0.0) or 0.0)
    except (TypeError, ValueError):
        return EMBED_TIMEOUT_SEC
    return t if t > 0.0 else EMBED_TIMEOUT_SEC


def build_embed_fn(cfg):
    """按配置装配云端 embedding；``embed_base_url / embed_model / embed_api_key`` 任一为空 → ``None``。

    返回 ``embed_fn(text) -> list[float]``，即 ``MemoryStore`` 的注入点；装配失败（地址非法等）
    也返回 ``None``：宁可降级成关键词检索，也不让记忆功能把服务拖垮。

    三个刻意的参数：``max_retries=0`` 不让 SDK 重试拖住出话（与 ``llm_chain`` 同一理由）；
    ``timeout`` 取 ``embed_timeout``（默认 10 秒，可收紧）；``check_embedding_ctx_length=False``
    原样发文本、不猜 tokenizer——``embed_model`` 可能是任意 OpenAI 兼容模型名，tiktoken 认不出来。
    """
    url = str(getattr(cfg, "embed_base_url", "") or "").strip()
    model = str(getattr(cfg, "embed_model", "") or "").strip()
    key = str(getattr(cfg, "embed_api_key", "") or "").strip()
    if url == "" or model == "" or key == "":
        return None
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

    def embed(text):
        return [float(x) for x in client.embed_query(str(text))]

    return embed


class MemoryService:
    """长期记忆的门面：配置 → ``MemoryStore``，对外只暴露召回与写入两个动作。"""

    def __init__(self, cfg):
        """按配置装配；``memory_enabled=False`` 时直接返回（不建库、不调 embedding）。"""
        self.store = None                   # MemoryStore | None：Task 4 的查询 / 统计 / 删除直接用它
        self._top_k = max(0, int(getattr(cfg, "memory_top_k", 0) or 0))
        self._max_chars = max(0, int(getattr(cfg, "memory_max_inject_chars", 0) or 0))
        if not bool(getattr(cfg, "memory_enabled", False)):
            return
        db = str(getattr(cfg, "memory_db_path", "") or "").strip() or DEFAULT_DB_PATH
        try:
            d = os.path.dirname(os.path.abspath(db))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
        except Exception as e:              # noqa: BLE001 —— 目录建不出来由 MemoryStore 再兜一次
            print("[Memory] 数据目录不可用：%s" % e)
        self.store = MemoryStore(db, embed_fn=build_embed_fn(cfg))

    @property
    def available(self) -> bool:
        """功能是否启用（默认关闭时为 ``False``；库本身打不开时 store 内部还会降级）。"""
        return self.store is not None

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
            return inject_text(self.store.recall(query, kk, mood=mood), self._max_chars)
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
            rows = self.store.recall(str(query or ""), kk, mood=mood)
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
