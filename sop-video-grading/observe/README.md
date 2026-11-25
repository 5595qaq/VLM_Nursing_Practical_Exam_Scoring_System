# observe

兩段式評分（issue #207，實驗）的描述 agent 看的「怎麼看」，一組標準一個檔，和 `criteria/` 的檔名對應。

- 描述 agent 的 system prompt 是 `DESCRIBE.md` 把 `<!-- criteria/*.md ... -->` 那一行換成這裡的 `*.md`（依檔名順序，`README.md` 除外）；`python sop-video-grading/scripts/skill_doc.py --describe` 可以印出完整內容。
- 這裡只寫**要怎麼看**（zoom 框哪隻手、短動作用 burst、先找出哪個時刻），**不寫怎樣算對**：門檻寫在 `criteria/` 與 `rubric.json`，由判定 agent 讀。只改門檻時，描述可以重用、只重跑判定（`sop_grading_cli --judge-only`）。
- 每條標準要觀察的項目寫在 `rubric.json` 該條的 `observe`；有 `observe` 的標準，`--two-stage` 時才會交給描述 agent。
- 這裡的檔、`DESCRIBE.md`、`JUDGE.md` 都算評分 SKILL.md 的一部分：改了要遞增評分 SKILL.md 的版本（`check_versions.py` 以組裝後的全文判斷）。改了這裡或 `DESCRIBE.md`、`observe` 之後，舊的描述就不能沿用。

| 檔案 | 標準 |
| --- | --- |
| `05-flick.md` | 彈針 17a、27a，先墊棉片 15b、25b |
| `08-hand-posture.md` | 手部姿勢 10b、12a、13a、14a、18b、18c、18d、19b、22b、22c、22d、24a、24c、26b、28b |
