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
- [ ] SOP 規則（`SKILL.md`、`criteria/`、`rubric.json`、`sop-video-skill/references/`）沒有改，或：
  - [ ] 已 rebase 到最新的 `main`，分支版本（`main` 的版本加 `-<主題>.<輪次>`）已改成 `main` 的下一個正式版本
  - [ ] `changelog.d/` 裡的片段（包括別人留下的）已移進 `CHANGELOG.md` 的新版本標題下，片段檔已刪除
  - [ ] 已用正式版本重跑這個 PR 負責的步驟，紀錄在 `sop-video-grading/runs/`
  - [ ] 這輪的實驗紀錄寫在 `sop-video-grading/experiments/`
  - [ ] 只改了自己負責的 `criteria/` 檔；動到共用部分（評分 `SKILL.md`、`sop-video-skill/`）時，已在 PR 標出並通知其他標準組的負責人
