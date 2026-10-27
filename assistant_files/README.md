# Azure 资料箱

把要交给 Azure 整理的文件放进 **`inbox/`**：

- 图片：`png / jpg / jpeg / webp / bmp`
- 文档：`pdf / docx / txt / md / csv / json / html / xlsx / pptx`（取决于本机 Python 依赖）
- 扫描会优先用本机 **OCR + VLM** 读取图片与扫描件，再交给 LLM 整理成日程。

操作：

1. 启动桌宠与后端；
2. 右键桌宠 → **资料与日程**；
3. 点 **开始处理**；
4. 处理结果会写入共享日程（`backend/data/life.db`），并作为可召回记忆与后续提示词上下文。

文件会保留在 `inbox/`，方便复查或再次处理；同一条日程会按“来源 + 时间 + 标题”去重。
