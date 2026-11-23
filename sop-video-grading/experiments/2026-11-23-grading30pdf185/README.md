# 歷史資料的離線驗證

在 repository 根目錄執行（需要 `jsonschema`，不需要影片、帳戶憑證或模型服務）：

```bash
python sop-video-grading/experiments/2026-11-23-grading30pdf185/verify_bundle.py
python -m pytest tests/sop_grading/test_verify_archived_bundle.py -q
```

驗證器依 `index.json` 的每一步來源讀取歷史 rubric 與彙總程式：1、2、29 為
`readsheet`；4、10 為 `separate174`；30 為 `placement`。步驟 3 是原紀錄沿用，
檢查版本、來源雜湊及輸出相同，不宣稱已驗證未保存的原版彙總程式。

檢查保留的 1,094 個原始檔、243 個程式快照、210 個逐步輸出、180 個歷史規則
彙總判定，以及 30 支 CLI 紀錄的歷史 final 格式。只略過未選步驟的待判定錯誤。
不讀目前 main 的 rubric／criteria，也不檢查 main 與過去版本等價。

根目錄 `.gitattributes` 把這個資料夾設成 `-text`，checkout 時不轉換換行；否則 Windows 的
`core.autocrlf=true` 會把 LF 換成 CRLF，雜湊對不上。在設定加入前就 checkout 的工作目錄，
要重新 checkout 這個資料夾（刪掉後 `git checkout HEAD -- <資料夾>`）。

`verification.json` 是 #186 當時執行的紀錄；裡面的 16,920 組 main 相容性比較是
當時整合分支的歷史結果，不能套用在只匯入資料的 #192 或目前 main。修正後不再
執行這項相容性比較，輸出改為 `historical_step_aggregations`，不覆寫歷史驗證紀錄。

來源補公開見 [#193](https://github.com/5595qaq/VLM_Nursing_Practical_Exam_Scoring_System/issues/193)
與 `source-publication.json`。保存 commit 不等於當時執行 commit；原 dirty、非 CLI、
混合歷史版本及舊答案重新彙總的限制全部保留。readsheet／placement 快照保留原始
CRLF；檢查 exact Git commit 與快照時，須區分換行差異與規則內容差異。

本次因預算不足，沒有新增 Gemini 影片評分、切分、獨立參考答案或正式版本影片回歸。
這些離線檢查不取代上述影片驗證，也不改變模型輸出、判定標準或既有一致率。
