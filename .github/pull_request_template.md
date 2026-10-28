## 修改內容

<!-- 改了什麼、為什麼。SOP 規則的 PR 寫出負責的步驟（例如 10b）。 -->

## Code review 結果

<!-- AGENTS.md「Code review」：找到哪些問題、哪些已修正、哪些不修以及理由。 -->

## 測試

<!-- 實際執行的測試與結果，以及沒有驗證的部分。 -->

## 檢查清單（規則見 AGENTS.md）

- [ ] 已對這個分支相對 `main` 的完整變更做 code review，結果寫在上面
- [ ] 文件已同步（README、TESTING、SKILL.md、CHANGELOG 等），或已寫明不需要更新的原因
- [ ] 沒有模型權重、憑證、影片、影格圖片、標記表或學生姓名（只用影片 ID）
- [ ] SOP 規則（`SKILL.md`、`rubric.json`、`sop-video-skill/references/`）沒有改，或：
  - [ ] 已 rebase 到最新的 `main`，分支版本（`0.29-10b.1`）已改成下一個正式版本
  - [ ] `changelog.d/` 裡這個分支的片段已移進 `CHANGELOG.md` 的新版本標題下
  - [ ] 已用正式版本重跑這個 PR 負責的步驟，紀錄在 `sop-video-grading/runs/`
  - [ ] 這輪的實驗紀錄寫在 `sop-video-grading/experiments/`
  - [ ] 動到共用段落（評分 SKILL.md 第 1–3、5 節、手部姿勢的通用規則）時，已在 PR 標出並通知其他步驟的負責人
