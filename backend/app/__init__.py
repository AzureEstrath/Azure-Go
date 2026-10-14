"""Azure 后端（前后端分离后的服务端）。

目录约定（与 Godot 客户端 scripts/ 一一对应，便于对齐与回归）：

    coords.py   ↔  game/scripts/core/Coords.gd
    board.py    ↔  game/scripts/core/GoBoard.gd
    state.py    ↔  game/scripts/core/GameState.gd
    joseki.py   ↔  game/scripts/core/JosekiData.gd + JosekiRecognizer.gd
    emotion.py  ↔  game/scripts/core/EmotionModel.gd
    prompts.py  ↔  game/scripts/ai/AzurePrompts.gd
    config.py   ↔  game/scripts/core/AzureConfig.gd（仅后端所需字段）
    engine.py   ↔  game/scripts/services/KataGoEngine.gd
    llm.py      ↔  game/scripts/services/LLMClient.gd
    agent.py    ↔  game/scripts/ai/AzureAgent.gd
    events.py   ↔  WS 事件契约（对应 AzureAgent 的 signal）
    main.py     ↔  FastAPI 入口（REST + WS 路由装配）

行为对齐：tests/golden/ 存放由 Godot 端导出的期望输出（golden），
tests/ 下的用例逐模块比对，保证「现有效果不变」。
"""
