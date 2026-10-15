"""对局编排（后端权威：对局、聊天、闲置搭话、情绪与定式都在这一层）。

职责：玩家落子 → Azure 点评 → Azure 思考并落子；提示/分析/聊天/闲置/总结/悔棋。
结果不直接返回给界面，而是交给 EventBus（事件契约见 ``events.py``），由 WS 推给客户端。

音画同步：``ai_move`` 事件带 ``move`` 坐标，客户端等她「落子宣告」的语音开始播放时本地揭示棋子；
后端把这一手**已落到内部棋谱**、可见更新延后（``reveal_move()`` 供需要 round-trip 的客户端调用）。
"""

import asyncio
import random
import re
import time

from . import coords, events, prompt_chains, prompts
from .board import SIZE as BOARD_SIZE
from .emotion import EmotionModel
from .joseki import JosekiRecognizer
from .state import GameState

# LLM「情绪变化」小调用：限频 + 忙时跳过，保护主对话的响应速度与 KV 前缀缓存
MOOD_LLM_GAP_MS = 15000
MOOD_LLM_MAX_TOKENS = 32

# 限制回复长度时追加的约束（在不动原人设的前提下，单独附在末尾）
LEN_NOTE = ("\n\n【本次额外要求 · 优先遵守】\n"
            "- 点评 / 提示：只回 1 句，尽量 20 字以内\n"
            "- 普通聊天：最多 2 句，尽量 40 字以内\n"
            "- 不复述局面、不客套，直接说结论就好")

# 「短回复」模式下，定式讲解用的替代约束：讲解要讲清要点，允许 2~3 句，不被一句式限制压扁
LEN_TEACH_NOTE = ("\n\n【本次额外要求 · 优先遵守】\n"
                  "- 这是定式讲解：把名称与要点讲清楚，2~3 句即可，别长篇大论\n"
                  "- 不复述局面、不客套，直接开讲就好")

# 括号里带「着棋点位」的舞台提示不需要显示，例如（白棋落在 C4）/（第7手 Q16）/（C4）
_PAREN_GROUP_PATTERN = "[（(][^（()）\\n]{0,40}[）)]"
_MOVE_HINT_PATTERN = "[A-HJ-Ta-hj-t]\\s?\\d{1,2}|落子|落在|虚着|pass|着子|第\\s?\\d+\\s?手"

# 闲置搭话兜底台词
IDLE_LINES = [
    "唔…还在吗？我在这里等你哦。",
    "在想什么呢？要不要再来一手？",
    "嗯…安静得都能听到风了呢。",
    "要不要聊聊天？我有点无聊了~",
]

# LLM 不可用（断网 / 401 / 429 / 超时 / 根本没配地址）时的兜底台词：
# 「棋局类」用于点评、提示、分析、落子宣告；「闲聊类」用于聊天与总结。
# 口吻与 IDLE_LINES 一致；多备几句随机挑，配合客户端的防复读窗口。
GAME_FALLBACK_LINES = [
    "唔…我脑子有点转不过来，这一手先不细说了。",
    "嗯…这盘棋我一时看不透，先接着下吧。",
    "唔…刚才那手我还没想明白，等会儿再跟你说。",
    "嗯…思路有点断了，先陪你摆着，一会儿再讲。",
]
CHAT_FALLBACK_LINES = [
    "唔…我脑子有点转不过来，这次就先说到这儿好不好？",
    "嗯…我现在有点迷糊，回头再好好陪你聊。",
    "唔…思路断了，等会儿再慢慢跟你说。",
]


class AzureAgent:
    def __init__(self, engine, llm, bus=None, joseki_teach=True, limit_len=False):
        self.st = GameState()
        self.katago = engine
        self.llm = llm
        self.bus = bus if bus is not None else events.EventBus()
        self.mood = EmotionModel()

        self.joseki_teach = joseki_teach
        self.limit_len = limit_len
        self.busy = False

        self._mood_region = -1
        self._mood_ms = 0
        self._mood_desc = ""
        self._reveal_pending = False

        self._group_re = re.compile(_PAREN_GROUP_PATTERN)
        self._hint_re = re.compile(_MOVE_HINT_PATTERN)

    # ================= 事件 =================

    def _emit(self, event):
        self.bus.publish(event)

    # ================= 工具 =================

    @staticmethod
    def _py_list(arr) -> str:
        """渲染成 Python 风格的列表（与网页版提示词里的取值格式保持一致）。"""
        return "[" + ", ".join("'%s'" % str(v) for v in arr) + "]"

    @staticmethod
    def _pct(v) -> str:
        return "%.1f%%" % (v * 100.0)

    @staticmethod
    def _llm_failed(t) -> bool:
        """LLM 调用失败的统一判据（与闲置搭话的兜底判据一致）。"""
        t = (t or "").strip()
        return t == "" or t.startswith("调用出错") or "脑子有点转不过来" in t

    @staticmethod
    def _fallback_line(kind="game") -> str:
        """LLM 不可用时露给玩家的兜底台词（kind：``game`` 棋局类 / ``chat`` 闲聊类）。

        云端失败在 ``LLMClient`` 里表现为「调用出错：…」文案（断网 / 401 / 429 / 超时），
        也可能意外抛出；两种情况下都必须换成这句玩家能读的话，绝不把错误文案当成她的台词。
        """
        lines = CHAT_FALLBACK_LINES if kind == "chat" else GAME_FALLBACK_LINES
        return str(lines[random.randrange(len(lines))])

    def _update_katago_brief(self, analysis, num_moves):
        """把 KataGo 形势数据整理成一段文字，存进 st.last_katago，供 Azure 上下文使用。"""
        if not analysis or "error" in analysis:
            return
        conv = prompts.to_black_perspective(analysis, num_moves)
        wr = float(conv[0])
        sc = float(conv[1])
        who = "Estarth（黑）稍优" if sc > 0.5 else ("Azure（白）稍优" if sc < -0.5 else "双方接近")
        s = "黑方（Estarth）胜率 %.1f%%；目差 %+.1f 目（正数=黑/Estarth 领先，含贴目）；整体 %s。" % (wr * 100.0, sc, who)
        own = analysis.get("ownership", [])
        if own:
            b = 0.0
            w = 0.0
            for v in own:
                f = float(v)
                if f > 0.0:
                    b += f
                else:
                    w -= f
            s += " 实地估算：Estarth（黑）约 %.0f 目，Azure（白）约 %.0f 目（按逐点归属估算，仅供参考）。" % (b, w)
        self.st.last_katago = s

    def _think_hint(self) -> str:
        """思考风格半句：只在思考档开启时注入（判据与旧 ``_sys_content`` 一致）。"""
        if self.llm is not None and getattr(self.llm, "think_mode", 0) > 0:
            return self.mood.think_hint()
        return ""

    def _sys_content(self, len_note) -> str:
        """系统消息文本：经 ``prompt_chains`` 的模板链构造（人设 + 心情 + 思考风格 + 长度约束）。

        系统消息与阶段无关——阶段提示只在「无 context」的回退路径里补进系统消息，这里既没有
        context 也没有 prompt，固定按首个阶段取链，产出与旧实现逐字符一致（test_prompt_chains 锁定）。
        """
        return prompt_chains.build_messages(
            prompt_chains.PHASES[0], len_note=len_note,
            mood_name=self.mood.region_name(), think_hint=self._think_hint(),
        )[0]["content"]

    def _sys_msg(self):
        return {"role": "system", "content": self._sys_content(LEN_NOTE if self.limit_len else "")}

    def _sys_teach_msg(self):
        return {"role": "system", "content": self._sys_content(LEN_TEACH_NOTE if self.limit_len else "")}

    @staticmethod
    def _user_msg(content):
        return {"role": "user", "content": content}

    def _moves_with(self, color, gtp):
        out = list(self.st.moves)
        out.append([color, gtp])
        return out

    def _set_busy(self, v):
        """设置忙碌状态；值没变就不推事件（嵌套的编排外壳会各自兜一次归位）。"""
        if self.busy == v:
            return
        self.busy = v
        self._emit(events.busy(v))

    def clean_reply(self, text) -> str:
        """去掉带点位的舞台提示；清洗后为空则退回原文。"""
        if text == "" or text is None:
            return text
        out = text
        matches = list(self._group_re.finditer(out))
        for m in reversed(matches):              # 从后往前删，避免偏移
            if self._hint_re.search(m.group(0)):
                out = out[:m.start()] + out[m.end():]
        out = out.strip()
        while "\n\n\n" in out:
            out = out.replace("\n\n\n", "\n\n")
        return out if out != "" else text

    async def _reply(self, messages, max_tokens=300) -> str:
        """所有对外展示的 LLM 回复统一过一遍清洗。

        云端失败时 ``LLMClient.chat`` 返回「调用出错：…」文案（不抛异常）；这里再兜住
        「真抛出来」的情况（返回空串），由调用方的兜底台词顶上——她该下的棋、该念的
        落子宣告都不会因此丢掉。
        """
        try:
            return self.clean_reply(await self.llm.chat(messages, max_tokens))
        except Exception as e:                  # noqa: BLE001
            print("[Agent] LLM 调用异常：%s" % e)
            return ""

    # ================= 三维情绪 =================

    def _mood_event(self, ev, desc_extra=""):
        """事件结算入口：本地增量立即生效，区域变化才推给界面。"""
        self.mood.apply_event(ev)
        self._mood_desc = EmotionModel.event_desc(ev) + desc_extra
        self.sync_mood(False)

    def sync_mood(self, force=False, quiet=False):
        """把当前情绪区域推给界面；区域没变时不产生任何事件。"""
        r = self.mood.region()
        if not force and r == self._mood_region:
            return
        self._mood_region = r
        info = self.mood.info(r)
        self._emit(events.mood({
            "region": r,
            "v": self.mood.v, "c": self.mood.c, "a": self.mood.a,
            "name": str(info.get("name", "")),
            "morphs": info.get("morphs", {}),
            "filler": "" if quiet else str(info.get("filler", "")),
            "burst": "" if quiet else str(info.get("burst", "")),
            "pitch": float(info.get("pitch", 0.0)),
        }))

    def tick_mood(self):
        """客户端每 2 秒的心跳：只结算「向基线回落」。"""
        self.sync_mood(False)

    def player_interaction(self, what):
        """玩家亲密互动（摸头等）：本地增量 + 静默同步（语气词让位给互动音效）。"""
        self.mood.apply_event(what)
        self._mood_desc = EmotionModel.event_desc(what)
        self.sync_mood(False, True)

    async def _mood_llm_refine(self):
        """单独的「情绪变化」小调用：异步、限频、失败静默；绝不等它。"""
        if self.llm is None or self._mood_desc == "" or self.busy:
            return
        now = int(time.monotonic() * 1000)
        if now - self._mood_ms < MOOD_LLM_GAP_MS:
            return
        self._mood_ms = now
        desc = self._mood_desc
        self._mood_desc = ""
        try:
            text = await self.llm.chat([{
                "role": "user",
                "content": prompts.mood_delta_prompt((self.mood.v, self.mood.c, self.mood.a), self.mood.region_name(), desc),
            }], MOOD_LLM_MAX_TOKENS, True)
        except Exception as e:                      # 失败静默：情绪小调用绝不打扰主流程
            print("[Agent] 情绪小调用异常：%s" % e)
            return
        if self._llm_failed(text):
            return
        parsed = EmotionModel.parse_delta(text)
        if not parsed[0]:
            return
        d = parsed[1]
        self.mood.apply_delta(d[0], d[1], d[2])
        self.sync_mood(False)

    @staticmethod
    def _chat_event(kind) -> str:
        return {
            "laugh": "chat_laugh",
            "delight": "chat_praise",
            "sad": "chat_sad",
            "puzzled": "chat_question",
        }.get(kind, "chat_normal")

    @staticmethod
    def chat_emotion(text) -> str:
        """聊天内容的粗分类 → 表情（本地关键词判断，不额外打扰 LLM）。"""
        t = (text or "").lower()
        for kw in ["哈哈", "hh", "笑", "有趣", "好玩", "233", "草"]:
            if kw in t:
                return "laugh"
        for kw in ["喜欢", "爱你", "谢谢", "温柔", "可爱", "厉害", "真棒", "漂亮", "好看", "帅", "辛苦了"]:
            if kw in t:
                return "delight"
        for kw in ["难过", "伤心", "累", "烦", "不开心", "生气", "讨厌", "输了", "失败", "糟糕", "唉", "哭"]:
            if kw in t:
                return "sad"
        for kw in ["？", "?", "吗", "怎么", "为什么", "什么", "如何", "能不能", "是不是"]:
            if kw in t:
                return "puzzled"
        return "pleased"

    # ================= 定式导学 =================

    def _joseki_check(self) -> dict:
        """定式识别（纯本地、零引擎调用，每手一次）。"""
        if not self.joseki_teach:
            return {}
        r = JosekiRecognizer.scan(self.st.moves)
        if not r:
            return {}
        kind = str(r["kind"])
        # 去重防刷屏：成型「每角每类一次」；偏离「每角一次、全局最多 2 次」（悔棋回滚后可重新识别）
        dedup = ("F:%s:%s" % (r["corner"], r["cat"])) if kind == "formed" else ("D:%s" % r["corner"])
        if self._joseki_logged(dedup):
            return {}
        if kind == "deviated":
            dcount = sum(1 for e in self.st.joseki_log if str(e.get("key", "")).startswith("D:"))
            if dcount >= 2:
                return {}
        self.st.joseki_log.append({"key": dedup, "move_no": len(self.st.moves)})
        return r

    def _joseki_logged(self, key) -> bool:
        return any(str(e.get("key", "")) == key for e in self.st.joseki_log)

    def _joseki_emit(self, r):
        """棋盘高亮轨迹：成型=按手顺点亮；偏离=只点「常规一手」那个位置。"""
        if not r:
            return
        pts = list(r.get("points", []))
        if str(r["kind"]) == "deviated":
            ex = r.get("expect", (-1, -1))
            if ex[0] < 0:
                return
            pts = [ex]
        if not pts:
            return
        self._emit(events.joseki({"kind": str(r["kind"]), "corner": str(r["corner"]), "points": pts}))

    # ================= 玩家落子 =================

    async def player_move(self, x, y):
        if self.busy:
            return
        legal = self.st.board.is_legal(x, y, "black")
        if not legal["ok"]:
            self._emit(events.message(events.AI, "这手不成立：%s" % legal["err"]))
            self._emit(events.state(self.st.payload()))   # 让界面撤销乐观落子
            return
        self._set_busy(True)
        try:
            gtp = coords.xy_to_gtp(x, y)
            n = len(self.st.moves)

            # ① 玩家落子前局面：缓存复用，否则现算（黑方视角，用于给这一手排名）
            analysis = {}
            if self.st.pre_analysis is not None and int(self.st.pre_analysis["n"]) == n:
                analysis = self.st.pre_analysis["a"]
            else:
                analysis = await self.katago.analyze(self.st.moves)
                if "error" in analysis:
                    print("[KataGo] " + str(analysis["error"]))
                    analysis = {}
                else:
                    self.st.pre_analysis = {"n": n, "a": analysis}
            winrate_before = 0.5
            move_infos = []
            if analysis:
                winrate_before = float((analysis.get("rootInfo") or {}).get("winrate", 0.5))
                move_infos = analysis.get("moveInfos", [])
            player_rank = -1
            for i in range(min(len(move_infos), 5)):
                if str(move_infos[i].get("move", "")) == gtp:
                    player_rank = i
                    break

            # ② 玩家落子后局面（轮到白方）：用于选择 Azure 的应手
            after = await self.katago.analyze(self._moves_with("B", gtp))
            after_infos = []
            after_black_wr = 0.5
            after_black_score = 0.0
            if "error" in after:
                print("[KataGo] " + str(after["error"]))
            else:
                after_infos = after.get("moveInfos", [])
                conv = prompts.to_black_perspective(after, n + 1)
                after_black_wr = float(conv[0])
                after_black_score = float(conv[1])
                self._update_katago_brief(after, n + 1)       # 当前棋面的引擎形势，供 Azure 上下文

            # ③ 提交玩家落子（先存快照，供俗手回档）
            self.st.snapshots.append(self.st.make_snapshot())
            self.st.board.play(x, y, "black")
            self.st.add_move("B", gtp)
            turn_index = len(self.st.snapshots) - 1

            # ④ 俗手判定：不在我算的前5，或胜率明显下滑
            drop = winrate_before - after_black_wr
            if player_rank < 0 or drop > 0.10:
                reason = "不在我算的前5候选" if player_rank < 0 else "胜率下滑%d%%" % int(round(drop * 100.0))
                self.st.record_bad_move(turn_index, gtp, reason)
            self._emit(events.state(self.st.payload()))

            # ④″ 定式识别（纯本地计算、零引擎调用）
            jk = self._joseki_check()
            if jk:
                self._joseki_emit(jk)

            # ④′ 情绪反应：按这一手的好坏结算三维情绪（即时反馈不等 LLM）
            ev = "player_ok"
            if str(jk.get("kind", "")) == "formed" or player_rank == 0:
                ev = "player_good"
            elif player_rank < 0 or drop > 0.10:
                ev = "player_bad"
            elif player_rank >= 3:
                ev = "player_off"
            self._mood_event(ev)

            # ⑤ Azure 点评这一手（定式成型时换成讲解，不额外增加 LLM 调用）
            eval_prompt = ""
            eval_sys = self._sys_msg()
            if str(jk.get("kind", "")) == "formed":
                eval_prompt = prompts.joseki_teach_prompt(str(jk["name"]), str(jk["key"]), str(jk["corner"]))
                eval_sys = self._sys_teach_msg()
            else:
                if player_rank == 0:
                    eval_prompt = "Estarth下了 %s，这是我算的最优手！胜率%s。轻描淡写夸一下，1-2句。" % (gtp, self._pct(winrate_before))
                elif 0 < player_rank < 3:
                    eval_prompt = "Estarth下了 %s，Top%d，胜率%s。肯定一下，1-2句。" % (gtp, player_rank + 1, self._pct(winrate_before))
                elif player_rank >= 0:
                    eval_prompt = "Estarth下了 %s，不在前5。委婉指出，给点鼓励，1-2句。" % gtp
                else:
                    eval_prompt = "Estarth下了 %s，没进我算的前5。慵懒表示不太理解，1-2句。" % gtp
                eval_prompt += " 用你自己的口吻评，1句就好，别像解说员那样说「黑方/白棋」；先结合【当前棋面】确认这手落在哪、周围有没有子再说；不要提你自己接下来打算下哪里，不要复述胜率数值；开头和收尾都换一种新的说法，别复读你最近用过的口头禅。"
                # 防复读：上一手点评的原话拿出来明确避开
                prev_eval = ""
                for i in range(len(self.st.memory_log) - 1, -1, -1):
                    if str(self.st.memory_log[i].get("tag", "")) == "E":
                        prev_eval = str(self.st.memory_log[i].get("text", ""))
                        break
                if prev_eval != "":
                    angles = ["这手的方向/意图", "厚薄与轻重", "形状好不好", "和附近棋子的呼应", "对以后势力的影响", "和刚才几手的联系"]
                    eval_prompt += " 你上一手点评的原话是「%s」：这次不要复用里面出现过的形容词和句式，也别用同样的夸奖方式；从%s这个角度换个说法，还是一句话。" % (prev_eval, angles[random.randrange(len(angles))])
                # 定式偏离：在常规点评后轻轻提一句常规形状
                if str(jk.get("kind", "")) == "deviated":
                    want = jk.get("expect", (-1, -1))
                    if want[0] >= 0:
                        eval_prompt += " " + prompts.joseki_hint_clause(str(jk["name"]), coords.xy_to_gtp(want[0], want[1]))
            player_eval = await self._reply([
                eval_sys,
                self._user_msg(prompts.with_context(self.st.moves, self.st.memory_log, self.st.chat_history, eval_prompt, "after_player", False, self.st.last_katago, self.st.board)),
            ], 300)
            if str(jk.get("kind", "")) == "formed" and self._llm_failed(player_eval):
                player_eval = prompts.joseki_fallback(str(jk["name"]), str(jk["key"]))
            elif self._llm_failed(player_eval):     # 云端失败：点评退化为兜底台词
                player_eval = self._fallback_line("game")
            self.st.log_memory("E", player_eval, len(self.st.moves))
            self._emit(events.message(events.AI_COMMENT, player_eval))

            # ⑥ Azure 思考并落子（把刚才那手点评一并传给她）
            await self._ai_turn(after_infos, after_black_wr, after_black_score, player_eval)
            await self._mood_llm_refine()
        except Exception as e:                      # 编排意外抛出（LLM/引擎）：不得让 busy 永久为真
            print("[Agent] 落子编排异常：%s" % e)
        finally:
            self._set_busy(False)

    async def _ai_turn(self, after_infos, fallback_wr, fallback_score, player_eval=""):
        try:
            pick = self._pick_ai_move(after_infos)
            ai_gtp, ai_x, ai_y = pick[0], pick[1], pick[2]

            # 用意：落子前说明「我准备下在哪、想做什么」
            think_hint = "你准备虚着（pass）" if ai_gtp == "pass" else "你准备下在 %s" % ai_gtp
            think_prompt = "轮到你下了。%s。用你自己的口吻（我/你）一句话说出这一手的用意：打算下在哪、想在当前盘面上达成什么、跟最近这几手有什么呼应，直接说。注意：你刚点评过Estarth那一手，这里不要再评价它、不要改口说相反的话，也别复述点评；不要用「黑方/白棋」的旁观口吻。换一个全新的说法，别总用「嗯~」「唔」开头，句式也别和刚才那条点评一样。" % think_hint
            await asyncio.sleep(0.35)
            think_msg = await self._reply([
                self._sys_msg(),
                self._user_msg(prompts.with_context(self.st.moves, self.st.memory_log, self.st.chat_history, think_prompt, "own_turn", False, self.st.last_katago, self.st.board)),
            ], 80)
            if self._llm_failed(think_msg):         # 云端失败：落子宣告退化为兜底台词（坐标照常揭示）
                think_msg = self._fallback_line("game")
            # 这条语音开始播放时，界面才把她的子放上棋盘（事件带坐标，客户端本地揭示）
            self._emit(events.message(events.AI_MOVE, think_msg, move={
                "x": ai_x, "y": ai_y, "gtp": ai_gtp, "pass": ai_gtp == "pass",
            }))

            # 真正落子：先落到内部棋谱；棋盘可见的更新延后到语音开始时（reveal_move）
            if ai_gtp == "pass":
                self.st.board.pass_move()
                self.st.add_move("W", "pass")
            else:
                self.st.board.play(ai_x, ai_y, "white")
                self.st.add_move("W", ai_gtp)
            self._reveal_pending = True
            if ai_gtp == "pass":
                self._emit(events.message(events.AI, "（Azure本手选择了虚着）"))

            jk2 = self._joseki_check()

            # ⑦ 先算「Azure 落子后局面」（轮到黑方）→ 供信息栏与下面「局势」的上下文使用
            post = await self.katago.analyze(self.st.moves)
            wr = fallback_wr
            sc = fallback_score
            if "error" in post:
                print("[KataGo] " + str(post["error"]))
            else:
                ri = post.get("rootInfo") or {}
                wr = float(ri.get("winrate", 0.5))
                sc = float(ri.get("scoreLead", 0.0))
                self._update_katago_brief(post, len(self.st.moves))
                # 这份「她落子后」的分析即下一手玩家落子前的局面：缓存复用
                self.st.pre_analysis = {"n": len(self.st.moves), "a": post}
            self._emit(events.info(wr, sc))

            if str(jk2.get("kind", "")) == "formed":
                ai_eval_prompt = prompts.joseki_short_prompt(str(jk2["name"]), str(jk2["key"]), ai_gtp)
            elif ai_gtp == "pass":
                ai_eval_prompt = "你（Azure）这手选择虚着。本回合你已经说过：①对Estarth那手的点评「%s」；②你的落子用意「%s」。换一句新话，用你自己的口吻（我/你）说说你现在对局面的感觉、心里在琢磨什么；不要重复上面两层意思、不要用相同句式，也不要用「感觉这盘棋会很有趣」「接下来看看你如何应对」这类套话。注意：上面引号里是你自己刚说过的话——整句和半句都不许原样搬回来，开头词和句式都必须换新的。" % (player_eval, think_msg)
            else:
                ai_eval_prompt = "你（Azure）刚下了 %s。本回合你已经说过：①对Estarth那手的点评「%s」；②你的落子用意「%s」。换一句新话，用你自己的口吻（我/你）说说：现在这盘棋我感觉怎么样、接下来我打算怎么走；不要重复上面两层意思、不要用相同句式，也不要用「感觉这盘棋会很有趣」「接下来看看你如何应对」这类套话，别用「黑方/白棋」这种旁观口吻。注意：上面引号里是你自己刚说过的话——整句和半句都不许原样搬回来，开头词和句式都必须换新的。" % (ai_gtp, player_eval, think_msg)
            ai_eval = await self._reply([
                self._sys_msg(),
                self._user_msg(prompts.with_context(self.st.moves, self.st.memory_log, self.st.chat_history, ai_eval_prompt, "just_moved", False, self.st.last_katago, self.st.board)),
            ], 120)
            if str(jk2.get("kind", "")) == "formed" and self._llm_failed(ai_eval):
                ai_eval = prompts.joseki_fallback(str(jk2["name"]), str(jk2["key"]))
            elif self._llm_failed(ai_eval):         # 云端失败：落子后那句退化为兜底台词
                ai_eval = self._fallback_line("game")
            self.st.log_memory("A", ai_eval, len(self.st.moves))
            self._emit(events.message(events.AI_COMMENT, ai_eval))
            self._joseki_emit(jk2)
            # 不在这里 emit state：落子的可见更新由 reveal_move() 在她这段语音开始时触发
        finally:
            self._set_busy(False)       # 编排外壳：异常也要让棋盘解锁

    def _pick_ai_move(self, move_infos) -> list:
        """按引擎候选顺序取第一个在当前棋盘合法的点；不允许虚着时绝不虚着。"""
        for m in move_infos:
            if not isinstance(m, dict):
                continue
            mv = str(m.get("move", ""))
            if mv == "":
                continue
            if mv == "pass":
                if self.st.allow_pass:
                    return ["pass", -1, -1]
                continue
            xy = coords.gtp_to_xy(mv)
            if xy[0] >= 0 and self.st.board.is_legal(xy[0], xy[1], "white")["ok"]:
                return [mv, xy[0], xy[1]]
        if self.st.allow_pass:
            return ["pass", -1, -1]
        # 候选全部不可用：就近找任意合法点，保证 Azure 一定落子
        near = self.st.last_played_xy()
        best = (-1, -1)
        best_d = 1e18
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                if self.st.board.at(x, y) != "":
                    continue
                dx = x - (near[0] if near[0] >= 0 else 9)
                dy = y - (near[1] if near[1] >= 0 else 9)
                d = float(dx * dx + dy * dy)
                if d < best_d and self.st.board.is_legal(x, y, "white")["ok"]:
                    best_d = d
                    best = (x, y)
        if best[0] >= 0:
            return [coords.xy_to_gtp(best[0], best[1]), best[0], best[1]]
        return ["pass", -1, -1]

    # ================= 其余功能 =================

    async def hint(self):
        """💡 提示：给方向性提示，不直接说坐标。"""
        if self.busy:
            return
        self._set_busy(True)
        try:
            n = len(self.st.moves)
            analysis = await self.katago.analyze(self.st.moves)
            if "error" in analysis:
                print("[KataGo] " + str(analysis["error"]))
                self._emit(events.message(events.AI, "嗯...我这边一时算不清，稍等再试吧~"))
                return
            self.st.pre_analysis = {"n": n, "a": analysis}
            tops = [str(m.get("move", "")) for m in (analysis.get("moveInfos", []) or [])[:3]]
            self._update_katago_brief(analysis, n)
            prompt = "当前局面：%s。我算到的好点：%s。Estarth还没落子，结合【当前棋面】给个方向性提示，1-2句；不要直接说坐标，提到某片区域前先确认那里确实有子（没有的子别乱说）；换个说法开场，别重复你上一次提示的句式和口头禅。" % (
                prompts.board_summary(self.st.move_seq), self._py_list(tops))
            msg = await self._reply([
                self._sys_msg(),
                self._user_msg(prompts.with_context(self.st.moves, self.st.memory_log, self.st.chat_history, prompt, "observe", False, self.st.last_katago, self.st.board)),
            ], 300)
            if self._llm_failed(msg):               # 云端失败：提示退化为兜底台词
                msg = self._fallback_line("game")
            self._emit(events.message(events.AI_COMMENT, msg))
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 提示异常：%s" % e)
            self._emit(events.message(events.AI_COMMENT, self._fallback_line("game")))
        finally:
            self._set_busy(False)

    async def analyze_position(self):
        """局面分析：以 Azure（执白）的视角解读局势。"""
        if self.busy:
            return
        if not self.st.moves:
            self._emit(events.message(events.AI, "棋盘还空着呢~ 先落几手，我再帮你看看形势。"))
            return
        self._set_busy(True)
        try:
            n = len(self.st.moves)
            analysis = await self.katago.analyze(self.st.moves)
            if "error" in analysis:
                print("[KataGo] " + str(analysis["error"]))
                self._emit(events.message(events.AI, "唔…我这边一时算不清，稍后再让我看看吧。"))
                return
            conv = prompts.to_black_perspective(analysis, n)
            black_wr = float(conv[0])
            score_black = float(conv[1])
            azure_wr = 1.0 - black_wr
            tops = [str(m.get("move", "")) for m in (analysis.get("moveInfos", []) or [])[:5]]
            self._update_katago_brief(analysis, n)
            lead = "你略占上风" if azure_wr > 0.5 else ("局面胶着" if azure_wr > 0.45 else "Estarth领先")
            prompt = "当前局面：%s，共%d手。你（Azure，执白）算出的判断：白方胜率约%s（%s），目差%s（正数是你领先）。你在考虑的几个点：%s。请用你自己的视角（我/你，别说「黑方/白棋」）分析当前局势3-4句：局面走向、关键处或薄弱处，可以点醒Estarth；语气慵懒但专业，先结合【当前棋面】核对再说，别提到盘上没有的子。" % (
                prompts.board_summary(self.st.move_seq), n, self._pct(azure_wr), lead, "%+.1f" % (-score_black), self._py_list(tops))
            msg = await self._reply([
                self._sys_msg(),
                self._user_msg(prompts.with_context(self.st.moves, self.st.memory_log, self.st.chat_history, prompt, "analyze", False, self.st.last_katago, self.st.board)),
            ], 350)
            if self._llm_failed(msg):               # 云端失败：分析退化为兜底台词（形势数据照常下发）
                msg = self._fallback_line("game")
            self._emit(events.message(events.AI, "📊 " + msg))
            self._emit(events.info(black_wr, score_black))
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 分析异常：%s" % e)
            self._emit(events.message(events.AI, "📊 " + self._fallback_line("game")))
        finally:
            self._set_busy(False)

    async def chat(self, text):
        """自由聊天。"""
        if self.busy:
            return
        if text is None or text.strip() == "":
            return
        self.st.chat_history.append({"role": "user", "content": text})
        # 按聊天内容即时结算情绪（本地关键词分类）
        self._mood_event(self._chat_event(self.chat_emotion(text)), "，他说：「%s」" % text[:40].strip())
        self._set_busy(True)
        reply = ""
        try:
            msgs = [
                self._sys_msg(),
                self._user_msg(prompts.context_block(self.st.moves, self.st.memory_log, self.st.chat_history, "chat", True, self.st.last_katago, self.st.board)),
            ]
            hist = self.st.chat_history
            upto = len(hist) - 1            # 最后一条是刚追加的这句，下面单独发，避免重复
            for sp in prompts.chat_windows(hist[:upto]):
                for i in range(int(sp[0]), int(sp[1]) + 1):
                    msgs.append(hist[i])
            msgs.append({"role": "user", "content": text})
            reply = await self._reply(msgs, 400)
        except Exception as e:                      # 意外抛出：按失败处理，别让棋盘锁死
            print("[Agent] 聊天异常：%s" % e)
        finally:
            self._set_busy(False)
        if self._llm_failed(reply):                 # 云端失败：聊天退化为兜底台词
            reply = self._fallback_line("chat")
        self.st.chat_history.append({"role": "assistant", "content": reply})
        self._emit(events.message(events.AI, reply))
        await self._mood_llm_refine()

    async def idle_remark(self):
        """闲置搭话：Estarth 一段时间没动静时，Azure 主动说一句短话。"""
        if self.busy:
            return
        self._mood_event("idle_bored")
        prompt = "Estarth 已经有一段时间没有动静了，你有点好奇他在做什么。主动对他说一句很短的话（20字以内）：问问他还在不在、在忙什么，或要不要继续下棋、聊聊天。慵懒、亲切，1句。"
        reply = await self._reply([
            self._sys_msg(),
            self._user_msg(prompts.context_block(self.st.moves, self.st.memory_log, self.st.chat_history, "chat", True, self.st.last_katago, self.st.board)),
            {"role": "user", "content": prompt},
        ], 90)
        if self._llm_failed(reply):
            reply = str(IDLE_LINES[random.randrange(len(IDLE_LINES))])
        self.st.chat_history.append({"role": "assistant", "content": reply})
        self._emit(events.message(events.AI_IDLE, reply))

    async def summarize(self, note=""):
        """对局总结。"""
        if self.busy:
            return
        if not self.st.moves:
            self._emit(events.message(events.AI, "还没下过棋呢~"))
            return
        self._set_busy(True)
        try:
            n = len(self.st.moves)
            final_wr = 0.5
            final_score = 0.0
            analysis = await self.katago.analyze(self.st.moves)
            if "error" in analysis:
                print("[KataGo] " + str(analysis["error"]))
            else:
                conv = prompts.to_black_perspective(analysis, n)
                final_wr = float(conv[0])
                final_score = float(conv[1])
            self._update_katago_brief(analysis, n)
            # 终局情绪：她赢下（黑方胜率 < 0.5）平和地开心，输了则有点低落
            self._mood_event("summary_win" if final_wr < 0.5 else "summary_lose")
            seq = " → ".join(str(s) for s in self.st.move_seq)
            prompt = "对局结束，共%d手。\n序列：%s\n最终黑胜率：%s，目差：%s\nEstarth备注：%s\n\n写总结：1.对局回顾 2.给Estarth的改进建议 2-3条 3.你的心情\n用Azure口吻，150-250字，慵懒真诚。" % (
                n, seq, self._pct(final_wr), "%+.1f" % final_score, note)
            text = await self._reply([
                self._sys_msg(),
                self._user_msg(prompts.with_context(self.st.moves, self.st.memory_log, self.st.chat_history, prompt, "summary", False, self.st.last_katago, self.st.board)),
            ], 600)
            if self._llm_failed(text):              # 云端失败：总结退化为兜底台词
                text = self._fallback_line("chat")
            self.st.finished = True
            self._emit(events.message(events.AI, "📋 对局总结\n" + text))
        except Exception as e:                      # 意外抛出：给一句兜底台词，别让棋盘锁死
            print("[Agent] 总结异常：%s" % e)
            self._emit(events.message(events.AI, "📋 对局总结\n" + self._fallback_line("chat")))
        finally:
            self._set_busy(False)

    def undo(self, turn_index):
        """俗手回档：撤销该手及其后的 Azure 应手，回到该手之前。"""
        if self.busy:
            return
        if turn_index < 0 or turn_index >= len(self.st.snapshots):
            self._emit(events.message(events.AI, "这个回档点已经不存在了哦"))
            return
        self._mood_event("undo")
        s = self.st.snapshots[turn_index]
        self.st.board = s["board"]
        self.st.moves = s["moves"]
        self.st.move_seq = s["move_seq"]
        self.st.memory_log = s["memory_log"]
        self.st.joseki_log = s["joseki_log"]      # 定式去重记录随快照回滚：悔棋重走那手可以重新讲解
        self.st.snapshots = self.st.snapshots[:turn_index]
        self.st.bad_moves = [b for b in self.st.bad_moves if int(b["turn_index"]) < turn_index]
        self.st.pre_analysis = None
        self._emit(events.state(self.st.payload()))
        self._emit(events.message(events.AI, "嗯~ 那我们退回第%d手，重新想想…" % len(self.st.moves)))

    def reset_game(self):
        keep_pass = self.st.allow_pass
        self.st.reset()
        self.st.allow_pass = keep_pass
        self.mood.reset()                        # 新一局：情绪回到基线（平静）
        self.sync_mood(True)
        self._emit(events.state(self.st.payload()))
        self._emit(events.message(events.AI, "嗯~ 重新来吧..."))

    def set_allow_pass(self, v):
        self.st.allow_pass = bool(v)
        self._emit(events.message(events.AI, "好~ 那这局我允许自己虚着。" if v else "嗯，这局我不会虚着，会认真陪你下。"))
        self._emit(events.state(self.st.payload()))

    def reveal_move(self):
        """由客户端在她「落子宣告」的语音开始播放时调用：把这一手真正显示到棋盘上。"""
        if not self._reveal_pending:
            return
        self._reveal_pending = False
        self._emit(events.state(self.st.payload()))

    def mood_payload(self) -> dict:
        """当前情绪快照（WS 连接建立时随 hello 一起下发）。"""
        info = self.mood.info(self.mood.region())
        return {
            "region": self.mood.region(),
            "v": self.mood.v, "c": self.mood.c, "a": self.mood.a,
            "name": str(info.get("name", "")),
            "morphs": info.get("morphs", {}),
            "filler": str(info.get("filler", "")),
            "burst": str(info.get("burst", "")),
            "pitch": float(info.get("pitch", 0.0)),
        }
