# criteria

評分 `SKILL.md` 第 4 節「各類標準怎麼判」的內容，一組標準一個檔，讓負責不同步驟的人改不同的檔案（分工見 `AGENTS.md`「多人協作」）。

- 評分 agent 看到的 system prompt 是 `SKILL.md` 把 `<!-- criteria/*.md ... -->` 那一行換成這裡的 `*.md`（依檔名順序，`README.md` 除外）；由 `scripts/skill_doc.py` 組裝，`python sop-video-grading/scripts/skill_doc.py` 可以印出完整內容。
- 每個檔以 `### 標題（標準編號）` 開頭，結尾留一個空行，接起來才和原本的單一檔案一樣。
- 新增一組標準就新增一個檔，用兩位數前綴決定在 prompt 中的位置；改檔名會改變順序，也就是改了 prompt。
- 版本仍是 `SKILL.md` 的 `version`：改了這裡任何一個檔（或新增、刪除檔案），都要遞增它，CI（`check_versions.py`）以組裝後的全文判斷有沒有改。

| 檔案 | 標準 |
| --- | --- |
| `01-hand-washing.md` | 洗手計時 1a、七個動作 1b |
| `02-drug-sheet.md` | 注視藥單、手指指出 2a/2b、4a/4b、29a/29b |
| `03-drug-check.md` | 有看藥物 3a |
| `04-needle-down-13b.md` | 針頭朝下插入稀釋液瓶 13b |
| `05-flick.md` | 彈針 17a、27a，先墊棉片 15b、25b |
| `06-plunger.md` | 針心往後抽 16a、26a，排氣 17b、27b |
| `07-rolling-20a.md` | 雙手旋轉 vial 20a |
| `08-hand-posture.md` | 手部姿勢 10b、12a、13a、14a、18b、18c、19b、22b、22c、24a、26b、28b |
| `09-disposal.md` | 丟棄空瓶 29c、空針放入包裝 30a、30b |
| `10-presence.md` | 開瓶、消毒、墊棉片 5a、6a、7a、8a、15a、25a |
