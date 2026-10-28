# 專案工作規則

## 溝通

- 使用繁體中文回覆。
- 完成修改時，說明修改內容、驗證結果，以及仍存在的限制。

## Git 與本機檔案

- 模型權重（例如 `*.pt`）只保留在本機，不得提交到 Git。
- 特別保留 `models/gazelle.pt` 與 `backend/app/models/gazelle_dinov2_vitb14_inout.pt` 的忽略設定，不得將它們加入版本控制。
- 不得使用 `git add -f` 強制加入被忽略的模型檔。
- 提交前檢查暫存區，避免包含模型權重、憑證、秘密資訊或產生的影片。

## Code review

- 每次修改完成後，開 PR 前必須先做 code review（例如 `/code-review`），對象是這個分支相對 `main` 的完整變更；沒有做過 review 不得開 PR。
- review 找到的問題要修正；決定不修的，寫明理由。修正後重新執行受影響的測試；修正幅度大時再 review 一次。
- PR 描述要寫 review 的結果：找到哪些問題、哪些已修正、哪些不修以及理由。

## 多人協作

這個專案由多人同時修改 SOP 規則。每個人（以及每個 AI agent）開始工作前都要讀完這一節；CI（`check_versions.py`）與 PR 範本會檢查其中幾項，沒通過不得合併。

### 分工

- **依步驟分工，不依檔案**：每人認領一組步驟（例如 10b、16a／17b、20a），用 GitHub issue 登記（標題如 `[SOP] 步驟 10b`），開始前先看有沒有人認領。同一組步驟同時只能有一個進行中的分支。
- 只改自己步驟的 `rubric.json` 條目與 `SKILL.md` 小節。**共用段落**——評分 `SKILL.md` 第 1–3、5 節、手部姿勢的通用規則、`sop-video-skill/SKILL.md` 的共通流程——要改時先在 issue 說明，PR 標出並通知其他步驟的負責人。
- 一個 PR 只處理一組步驟，一輪疊代就開 PR，不要累積多輪。
- 每人用自己的 clone 或 `git worktree`，不要共用工作目錄；分支從最新的 `main` 開。

### 版本號：疊代時用分支版本，合併前才改正式版本

- 分支上改了規則，版本寫成「`main` 的版本 + `-<主題>.<輪次>`」，例如 SKILL.md `0.29-10b.1`、rubric `1.16-10b.1`；每輪遞增輪次（`.2`、`.3`…）。主題只用小寫英文、數字，和分支主題相同，不同人不得重複。
- 分支版本的評分紀錄照常 commit 到 `sop-video-grading/runs/`，它們的資料夾和正式版本分開，不會和別人撞號。
- **合併前**（一次完成）：
  1. `git rebase origin/main`，解完衝突。
  2. 把版本改成下一個正式版本（`main` 的版本 +1，例如 `0.30`）。期間有別人先合併，就再 rebase 並往上加。
  3. 把 `changelog.d/` 裡的所有片段（包括別人不改版本的修改留下的）移到 `CHANGELOG.md` 的新版本標題下，刪除片段檔。
  4. 用正式版本對這個 PR 負責的步驟重跑最後一輪的影片並 commit 紀錄。rebase 沒帶進別人對同一段或共用段落的修改時，可以只跑 1 支確認，在 PR 寫明理由。
- 評分端的 `CHANGELOG.md` 以評分 SKILL.md 版本為標題（例如 `## 0.30（rubric 1.17）`），所以改 rubric 時評分 SKILL.md 也要遞增；只改 SKILL.md 時 rubric 不動。sop-video-skill 的版本獨立。

### 不會衝突的紀錄方式

- **CHANGELOG**：疊代期間不改 `CHANGELOG.md`，改在 `sop-video-grading/changelog.d/` 或 `sop-video-skill/changelog.d/` 新增自己分支的檔案（見其中的 `README.md`）。不改版本的修改也寫片段，下一個正式版本合併時一併移入。
- **實驗紀錄**：每輪寫成 `sop-video-grading/experiments/YYYY-MM-DD-<主題>.md`，不再往 `EXPERIMENTS.md` 後面加。需要老師決定的事項仍寫在 `EXPERIMENTS.md` 的「待老師確認」（唯一的清單）。
- **評分紀錄**：資料夾依影片與版本分開，不會衝突。commit 時只加 `sop-video-grading/runs/<影片ID>/`。

### 合併之後

- Git 沒有衝突不代表規則沒有衝突：別人的 PR 合併後，rebase 並抽查自己步驟在之前已一致的影片上是否仍一致（疊代流程第 6 步的回歸）。
- 依合併順序，後合併的人負責解衝突、改版本與重跑。
- 要跑付費評分前，先用 `run_record.py` 查紀錄，並在步驟的 issue 留言登記要跑的影片，避免兩個人重複付費。
- 標準答案資料集維持由一位維護者寫入；人工核對 CSV 由負責該步驟的人處理。

## 文件同步

- 修改程式、API、資料格式、設定、環境變數、依賴、目錄結構、CI workflow 或操作流程時，必須在同一個分支同步更新受影響的文件，包括 `README.md`、`TESTING.md`、`sop-video-skill/SKILL.md`、`sop-video-skill/CHANGELOG.md` 等。
- 提交或開 PR 前，檢查 `README.md` 的功能說明、指令、設定表與目錄結構是否仍與程式一致；不一致就一併修正。
- 確認不需要更新文件時，在完成說明或 PR 描述中寫明原因。

## SOP 評分與切分的疊代流程

適用於修改 `sop-video-grading/`、`sop-video-skill/` 的規則、prompt、rubric，或使用者要求評估切分／評分結果時。使用者只要求「跑」某支影片時，照下方「只執行評分」做，不進入疊代流程。

### 只執行評分

使用者只要求跑某支影片（例如「幫我跑 VID_0081」）時：

- 用標記表找出影片ID，再用 `python sop-video-grading/scripts/run_record.py` 確認同一版本還沒評過。
- 執行 `sop_grading_cli`。使用者指定的影片就是同意執行的範圍；工作目錄有別人未 commit 的修改時，照下方「執行評分」的規則先問使用者。
- 跑完確認紀錄正常寫進 `sop-video-grading/runs/`：檔案齊全（`record.json`、`segmentation.json`、`grading.json`、每個任務的對話紀錄）、`validate_grading.py --final` 通過、沒有學生姓名或原始影片路徑、commit 沒有 `-dirty`。
- 回報紀錄位置、token 用量與執行時間，並問使用者要不要 commit 紀錄。
- 不抽影格、不判斷結果對錯、不比對標記表，除非使用者另外要求；要進入疊代流程前先問使用者。

### 資料來源

- 人工標記表：根目錄的 `無菌抽藥技術_vial粉劑_影片標記表_已填.xlsx - SOP標記明細 (1).csv`。每次疊代先讀它，取得目標影片與步驟的判定、時間與說明。
- 標記表含學生姓名，已被 `.gitignore` 忽略，不得提交。commit 訊息、PR、文件與 artifact 一律用影片 ID（例如 `VID_0083`），不寫姓名。
- 標記表不一定正確（時間錯、漏標、判定與標準不符都出現過），只能當參考，不能直接當標準答案。
- 標準答案資料集由人工另外維護（Google 試算表，連結不公開，不得寫進 repo、commit、PR 或 artifact；需要時向維護者索取）。Claude 不直接修改它，也不修改標記表；發現的更正列入人工核對清單，由人寫進資料集。
- 評分紀錄：`sop-video-grading/runs/`（見 README「評分紀錄」）。多位同事共用，跑之前先用 `python sop-video-grading/scripts/run_record.py` 查同一影片、同一版本是否已經評過；評過的直接用紀錄裡的 `grading.json` 比對，不要重跑。

### 每輪疊代

1. **建立參考答案**：先讀標記表，再自己抽影格（`burst.py`、`clip.py`，或 `sop-video-skill/scripts/sheets.py`、`crop.py`）判斷每個目標步驟。參考答案要寫出判定、依據的 mm:ss 與影格，以及引用的 rubric 條目。
   - 影格會拍到學生的臉：抽出的圖片只能放在 repo 之外的工作資料夾（例如 `vlm_data/`）或被忽略的 `sop-video-grading/review/`，不得 commit，也不得放進 PR 或 artifact。
   - 在看評分 agent 結果之前完成參考答案，避免被模型答案影響。
   - 參考答案與標記表不一致時，記下兩者與理由，標成「待人工核對」，不要自行認定標記表錯。
2. **執行評分**：用 `backend/app/sop_grading_cli.py` 評分，只重評受影響的步驟時用 `--steps`。評分會呼叫付費模型：使用者沒有明確指定影片、要跑的影片比使用者指定的多，或單支影片預估超過 200 萬 input tokens 時，先問使用者（參考：VID_0081 是 11 分鐘的影片，切分約 52 萬、評分約 79 萬，合計約 131 萬）。
   - 不得把標記表或參考答案的判定、說明放進評分 agent 的輸入；`labels_to_segmentation.py` 只能帶時間。
   - 執行時帶 `SOP_RUN_BY` 與 `GIT_COMMIT`（用 README 的寫法，有未 commit 的修改會帶 `-dirty`），跑完把 `sop-video-grading/runs/` 的新紀錄一起 commit。規則有改就先遞增 rubric 或 SKILL.md 版本並 commit 再跑（疊代中用分支版本，見「多人協作」），否則會被當成重複或 dirty 而拒絕；不要用 `--force`、`--allow-dirty` 繞過，除非是刻意重測穩定性並在紀錄中說明。
   - 共用的工作目錄常有別人未 commit 的修改，`GIT_COMMIT` 會帶 `-dirty` 而被拒絕。不要為了跑評分而 commit、stash 或還原別人的修改，也不要用 `--allow-dirty` 繞過；先告訴使用者，由使用者決定等對方 commit，或改在乾淨的 `git worktree` 跑（worktree 裡沒有被忽略的 `.env` 與 `secrets/`，要另外指定 `--env-file` 與 `GCP_SA_KEY_PATH`；紀錄會寫在 worktree 的 `sop-video-grading/runs/`，跑完複製回原工作目錄再 commit）。
   - commit 紀錄時只加 `sop-video-grading/runs/<影片ID>/`，不要連同工作目錄裡其他人的修改一起提交。
3. **比對**：用 `sop-video-grading/scripts/eval_grading.py` 比對評分結果與標記表，再逐一比對與參考答案不一致的步驟。
4. **歸因**：每個不一致先判斷原因，再決定改什麼：
   - 評分 agent 看錯、漏看 → 改 SKILL.md 的作法（取樣、放大、聲音等）。
   - 標準本身不清楚或沒寫 → 改 `rubric.json` 的標準文字，並列入人工核對。
   - 切分時間錯 → 改 `sop-video-skill`，不要在評分端補救。
   - 參考答案或標記表錯 → 不改規則，列入人工核對。
   - 畫面看不到 → 應判「無法判定」，不要為了對上答案而放寬規則。
5. **修改規則**：只為可以說清楚、能套用到其他影片的原因修改，不為單一影片加特例。改 rubric 或 SKILL 時遞增分支版本，並寫進自己分支的 `changelog.d/` 片段（合併前才移進 `CHANGELOG.md`，見「多人協作」）。
6. **重跑與回歸**：重跑不一致的步驟，並抽查同一步驟之前已經一致的影片，確認沒有改壞。
7. **記錄**：每輪把做法、影片、一致率、不一致案例與歸因寫成 `sop-video-grading/experiments/YYYY-MM-DD-<主題>.md`（2026-10-29 之前的紀錄在 `EXPERIMENTS.md`）。

### 何時算穩定

- 連續兩輪沒有修改規則，且這兩輪結果一致。
- 同一支影片同一步驟重跑，判定不會在正確與錯誤之間跳動；會跳動的步驟標成需人工複查，不算穩定。
- 規則是在一組影片上調出來的，最後要在沒參與調整的影片上驗證一次，並分開報告兩組的一致率。

### 人工核對

規則穩定後，用 `sop-video-grading/scripts/review_page.py` 產生人工核對頁面交給使用者（用法見 README「人工核對頁面」），至少包含：

- 參考答案與標記表或標準答案資料集不一致的步驟：寫成 `--items` 的 step 建議，`why` 要有判定理由與 mm:ss 時間點（頁面會變成可點的跳轉）。
- 這次新增或修改的標準，以及修改前後的文字：寫成 `--items` 的 standard 項目。

`--items` 的 JSON 放在被忽略的 `sop-video-grading/review/`，不寫學生姓名。使用者匯出的結果 CSV（決定為同意建議／同意標記表／改判／待老師確認）是下一輪的依據：讀完後依決定修改規則或列入待老師確認，不要自己推翻使用者的決定。

使用者確認的標準寫進 `rubric.json` 與 `changelog.d/` 片段；需要老師決定的寫進 `EXPERIMENTS.md` 的「待老師確認」，並在下次 SOP 評分工作開始時提醒使用者。未經人工核對的參考答案與標準，在文件中都要標明是暫定。

## 測試與驗證

- 修改後依影響範圍執行測試，涵蓋修改功能及可能受影響的功能；不得只因修改功能的測試通過，就認定其他功能不受影響。
- 修改共用程式、API、資料格式、設定或依賴時，檢查使用它們的模組並擴大測試範圍；影響範圍不明時，執行完整測試。
- 跨模組流程變更時，執行相關整合測試，必要時驗證從影片上傳、處理到評分結果的完整流程。
- 僅修改文件或註解且不影響程式行為時，可只驗證內容與格式；若文件包含操作指令或設定範例，仍應確認其正確性。
- 完成時說明實際執行的測試及結果，並列出未驗證的部分；若因環境、依賴或外部服務無法執行測試，明確說明原因，不得宣稱測試通過。
