# VLM+LLM Nursing Practical Exam Scoring System

這是一個基於 VLM (視覺語言模型) 與 LLM 架構的護理術科評分系統。系統包含 JavaScript 前端與 FastAPI (Python) 後端。影片先上傳到 Google Cloud Storage (GCS)，後端透過 Vertex AI 呼叫 Gemini 模型對 `gs://` 路徑的影片進行解析與術科自動評分；另有「只跑 SOP 步驟切分」模式，同樣透過 Vertex AI 由 Gemini（預設 `gemini-4-argon`）執行 `sop-video-skill/`。

> 目前僅供組內測試使用，前端是設計給組員測試 prompt/影片評分效果的工具，尚未考慮一般使用者的多人權限管理。

## 系統特色

- **事件驅動架構 (Event-Driven)**：採用高擴充性的 Worker 排程概念，完全解耦 API 請求與耗時推論任務。
- **即時進度監控**：前端透過 WebSocket 即時取得任務執行進度 (Uploading -> Processing -> Scoring)，並在頁面上呈現終端機風格的進度條與日誌。
- **資料庫狀態持久化**：所有的任務執行狀態與最終判定結果會被記錄至 PostgreSQL 資料庫中。
- **GCS 影片上傳與內容去重**：前端可直接選取本機影片；後端依原始內容的 SHA-256 將轉檔存為 `videos/{sha256}_src.mp4`，相同內容直接沿用，不同內容即使同名也不會互相覆蓋。手動貼入時只接受本系統先前產生、位於設定 bucket 的 normalized URI。
- **單一原檔影片來源**：後端用 ffmpeg 轉成 H.264（CRF 18、保留原始解析度與幀率、固定幀率、每 1 秒一個關鍵影格、保留音訊）；Gemini、Gazelle 與 sop-video-skill 共用同一個 GCS 物件。Gazelle 自行從片段抽 5 FPS，skill 自行產生 proxy，zoom 直接讀原檔。舊的 `videos/{sha256}_5fps.mp4` 已停止產生，新工作也不再接受，需用原始影片重新上傳。
- **Vertex AI 認證**：後端統一使用 GCP service account 認證 Vertex AI／GCS，組員不需要各自準備或輸入 Gemini API Key。
- **彈性結果格式**：多個 Agent 產出的評分 JSON 欄位尚未統一，前端以通用卡片＋原始 JSON 檢視的方式呈現，方便邊測 prompt 邊看結果。
- **時間分段＋可選 Agent 評分**：`Time_cuting.txt` 先對完整影片找出四個重疊時間區段，再執行所選的 Agent_A ~ Agent_D；各 Agent 的 prompt 分別存放於 `backend/app/prompts/Agent_A.txt` ~ `Agent_D.txt`。評分表單可複選 Agent，預設全選；選到 Agent_A 或 Agent_D 時會先執行 Gazelle，兩者皆未選時才跳過 Gazelle。
- **Token 用量與成本**：每一次模型回應（Time_cuting、Agent A–D、SOP skill 每回合，包含 JSON 驗證失敗後的重送）的 token 數都會存進 PostgreSQL 的 `model_usage` 表，可從前端下載「token 用量 CSV」；設定 `MODEL_PRICES_JSON` 後會一併算出估計成本，詳見下方〈Token 用量與成本〉。
- **評分證據時間點**：Agent_A、Agent_B、Agent_D 的每個評分步驟都會輸出 `證據時間點`（`MM:SS-MM:SS`），指出最直接支持判定的影片區間；未觀察到的步驟則填該動作應出現的檢查區間。前端下載的 CSV 也會帶出此欄位。
- **PostgreSQL 持久佇列**：PgQueuer 管理派送、去重與中斷恢復；每個評分工作最多 10 支活動影片，所有 worker 合計最多 5 個 Gemini 呼叫。
- **SOP 影片步驟切分 skill（開發中）**：`sop-video-skill/` 是新的前處理方案，把影片切成 30 個 SOP 步驟各自的時間區段（只切分、不評分）。評分表單的「執行模式」選「只跑 SOP 步驟切分」即可執行，詳見下方「SOP 影片步驟切分 skill」。
- **SOP 評分（開發中）**：`sop-video-grading/` 讀取切分結果，對 30 步逐條判定「正確／錯誤／部分正確／無法判定」並附說明。看畫面的標準由評分 agent（Gemini，可聽影片聲音）判定。評分表單的「執行模式」選「SOP 步驟切分＋評分」即可從前端執行（先切分、再評分），也可以用命令列在本機逐支測試，詳見下方「SOP 評分」。

## 系統運作流程與架構

API 與 PgQueuer worker 分開執行，共用 PostgreSQL，流程如下：

0. **[影片上傳]**：前端選取本機影片，轉成內容雜湊命名的 `_src.mp4` 影片並上傳到 GCS，取得唯一的 `gs://` 路徑；也可以貼上本系統先前產生的 normalized URI。
1. **[呼叫 API]**：前端帶著 `gs://` 路徑發起評分請求說：「我要上傳評分任務喔！」
2. **[建立工作]**：API 在同一交易寫入評分工作、全部影片、四個 Agent 狀態與前 10 支影片的切段任務，然後回覆 HTTP 200。超過 10 支的影片保存在資料庫等候。
3. **[喚醒 worker]**：PgQueuer 使用 `LISTEN/NOTIFY` 與 polling fallback 派送 PostgreSQL 中的任務。
4. **[確認影片]**：worker 確認 GCS 物件存在，開始時間切段。GCS 確認會隨影片排入窗口進行，與其他影片的分析重疊。
5. **[時間分段]**：`Time_cuting` 先讀取完整影片，產生 Agent_A ~ Agent_D 的重疊時間範圍。
6. **[平行評分]**：切段結果與所選評分任務在同一交易提交；所選 Agent 分析自己的原影片時間區段。所有工作共用 PostgreSQL 強制執行的 5 路 Gemini 上限。一支影片的所選 Agent 完成後，才補入下一支影片。
7. **[過程回報]**：每個 Agent 完成時更新資料庫，再由 PostgreSQL 廣播給 WebSocket Manager 即時推送前端。

Gemini 推論在雲端執行；worker 不需要 GPU。增加 worker 數量不會增加全域 Gemini 上限。

### 併發與重試設定

在根目錄 `.env` 可設定以下值，套用時需停止並重啟所有 worker，確保整個 fleet 的限制一致：

| 設定 | 預設 | 意義 |
| --- | --- | --- |
| `MAX_ACTIVE_VIDEOS_PER_EVALUATION` | 10 | 每個評分工作的活動影片窗口 |
| `GEMINI_GLOBAL_CONCURRENCY` | 5 | 所有 worker 共用的 Gemini 任務上限，包含 SDK 退避期間 |
| `GEMINI_CALL_STAGGER_MS` | 250 | 同一批切段／四個 Agent 任務的最早執行時間間距 |
| `GEMINI_RETRY_ATTEMPTS` | 5 | 每次邏輯模型呼叫的 HTTP attempts，包含第一次 |
| `GEMINI_CALL_TIMEOUT_SECONDS` | 900 | 單次邏輯模型呼叫的最長等待秒數 |
| `GEMINI_QUEUE_RETRY_BASE_SECONDS` | 60 | 暫時性模型錯誤後，PgQueuer 第一次重新派送前的等待秒數 |
| `GEMINI_QUEUE_RETRY_MAX_SECONDS` | 900 | queue 層指數退避的最長等待秒數 |
| `GEMINI_QUEUE_MAX_ATTEMPTS` | 10 | 同一個 queue 任務最多執行幾次（含第一次）；暫時性模型錯誤到這個次數仍未成功，工作改為失敗，不再重新派送（預設約 1.8 小時後放棄） |

`execute_after` 是最早執行時間，不是保證的全域 RPM 限流；積壓任務或多個評分工作仍可能同時就緒。5 路是本專案的控制值，不是 Google 配額保證。

Vertex AI 回傳 429／`RESOURCE_EXHAUSTED`、499／`CANCELLED`，或呼叫超過上述 timeout 時，該模型任務會保留在 PgQueuer 並按 queue 層設定延遲重派，不會將整個評分工作標記為失敗。其他模型錯誤仍維持終止工作的行為，且可在前端保留既有進度後手動續跑。

SDK 對 408、429、500、502、503、504 及其支援的暫時性網路錯誤執行指數退避：1 秒起跳、倍率 2、最高 60 秒、jitter 1。400、401、403、404 不重試；應用層另以 `GEMINI_CALL_TIMEOUT_SECONDS` 限制整次呼叫。JSON／切段驗證最多重試一次；10 支影片正常為 50 次邏輯呼叫，只有切段重試時最多 60 次，若切段及評分都各重試一次則最多 100 次，HTTP attempts 另計。

任一任務用盡重試後整個 evaluation 失敗，後續排隊任務跳過，已在執行的結果不再寫入。結果順序固定為輸入影片順序，再依所選 Agent A–D。每支影片的進度步驟為切段、所選 Agent，以及選到 Agent_A 或 Agent_D 時的 Gazelle。重試沿用建立工作時的 Agent 選擇。API 建立 evaluation 時可傳入 `selected_agents`，例如 `["Agent_B"]`；省略時執行全部，空清單、重複或未知 Agent 會回傳 422。

### Gazelle gaze preprocessing

上傳 API 只產生一支 `videos/{sha256}_src.mp4`。Gemini 時間切段及 Agent B、C 直接使用這支影片；獨立 GPU worker 會分別對 Agent A 與 Agent D 的時段執行 Gazelle，先將該時段抽成 5 FPS，產生各自的紫色注視點影片與逐幀 JSON，再用對應的 overlay 啟動 Agent A／D。

1. 下載 `gazelle_dinov2_vitb14_inout.pt` checkpoint，存成 `./models/gazelle.pt`（或設定 `GAZELLE_CHECKPOINT_PATH` 指向原檔名）。
2. 將 `GAZELLE_REF` 設為部署驗證過的 Gazelle commit SHA；未設定時 Docker build 使用 `main`，僅適合開發。
3. 安裝 NVIDIA Container Toolkit；Gazelle 是 Agent A 與 Agent D 的必要前置，標準的 `docker compose up -d --build` 會自動啟動 GPU worker。

可用 `GAZELLE_MODEL_NAME`、`GAZELLE_MODEL_VERSION`、`GAZELLE_INOUT_THRESHOLD` 與 `GAZELLE_DOT_RADIUS` 調整模型與疊點行為。`GAZELLE_MODEL_NAME` 必須選擇帶有 in/out head 的 `_inout` 模型，並使用對應 checkpoint；Gazelle worker 會在啟動時拒絕不支援的名稱。正式環境應固定 Git commit、checkpoint 檔及 DINOv2 快取版本。

使用 `pgqueuer==1.4.0`：原方案的 1.0.2 經雙 worker 測試曾超出 5 路；1.4.0 包含官方 [capacity slots 修正](https://github.com/janbjorge/pgqueuer/pull/777)。SDK 固定為已驗證的 `google-genai==2.23.0`。

PgQueuer 提供 at-least-once delivery。已提交結果會跳過重跑，重複完成不會重複累計；但若 Gemini 成功後、資料庫提交前 worker 中斷，恢復時可能再次呼叫並計費。也不能以本地 worker 的名額保證被中斷的遠端 Google 請求已立即停止。

`docker compose logs -f worker` 可查看 `gemini_http_attempt` JSON 日誌，包含 queue/evaluation/video/agent ID、queue attempt、HTTP attempt、狀態碼、重試間隔及耗時；不記錄影片內容或認證標頭。

### SOP 影片步驟切分 skill

`sop-video-skill/` 以「無菌抽藥技術－vial 粉劑」的 30 個步驟為準，輸出每個步驟發生的時間區段 JSON。模型不直接看影片，而是由外部程式抽幀、裁切、拼圖後提供影格圖片；模型每回合輸出一個 JSON 動作請求（`crop_preview`、`set_crop`、`overview`、`zoom`、`final`），由 `scripts/` 執行後回傳下一批圖片。

| 檔案 | 用途 |
| --- | --- |
| `SKILL.md` | 部署時當作 system prompt 的規則 |
| `references/sop_steps.md` | 30 個步驟的定義、視覺線索與易混淆配對 |
| `actions.schema.json` | 動作請求格式，由 `scripts/validate_action.py` 檢查 |
| `schema.json` | `final` 結果格式，由 `scripts/validate_result.py` 驗證 |
| `scripts/` | 抽幀／裁切（`prepare.py`、`crop.py`、`sheets.py`、`grid_overlay.py`）、動作能量（`motion.py`）、順序檢查（`sequence_check.py`）、與標準答案比對（`eval.py`）、Structured Outputs schema 轉換（`schema_tools.py`） |
| `CHANGELOG.md` | 各版本的修改紀錄 |

抽幀、裁切與動作能量腳本需要 ffmpeg；結果驗證與 schema 轉換只需要 `jsonschema`，不需要資料庫或 ffmpeg：

```bash
python sop-video-skill/scripts/validate_result.py result.json
python sop-video-skill/scripts/schema_tools.py sop-video-skill/schema.json > out.json
```

測試位於 `tests/sop_skill/`，執行方式見 [TESTING.md](TESTING.md)。打包好的 `sop-video-skill*.zip` 已被 `.gitignore` 排除，請直接使用 `sop-video-skill/` 目錄。

#### 執行模式：只跑 SOP 步驟切分

評分表單的「執行模式」可選 **只跑 SOP 步驟切分**。此模式不執行 Time_cuting、Gazelle 與 Agent A–D，而是由 worker 依 `sop-video-skill/SKILL.md` 執行外部程式迴圈，透過 Vertex AI 呼叫 Gemini（預設 `gemini-4-argon`；設定 `SOP_SKILL_PROVIDER=claude` 可改用 Vertex AI 上的 Claude），輸出 30 個步驟各自的時間區段（`sop-video-skill/schema.json`，不評分）。

1. worker 從 GCS 下載上傳時產生的 `_src.mp4` 原檔，執行 `prepare.py` 產生 proxy 與格線圖。
2. 每回合模型回覆一個動作 JSON（`crop_preview`／`set_crop`／`overview`／`zoom`／`final`）；worker 先用 `validate_action.py` 驗證，不合格就把錯誤和被退回的動作原文回傳（要求模型只修正錯誤的地方）、不執行也不扣預算，合格才執行對應腳本並把新圖片送回。
3. 每次請求只附「全片總覽圖＋動作能量圖＋最新一批圖片＋最新 draft＋動作紀錄＋剩餘預算」（SKILL.md 第 0 節），不重送舊的對話回合。每回合不變的 system prompt 與固定圖片（`set_crop` 前是格線圖，之後是總覽圖與動作能量圖）一律放在最前面、逐位元組相同，會變的部分放在最後。送 Gemini 時，固定內容自成一個 user 回合，後面接一句固定的 model 回覆（「已收到固定圖片…」），再接本回合的 user 內容：實測 `gemini-4-argon` 的隱式快取只在前綴剛好結束一個回合時命中，固定內容與本回合內容放在同一個（或兩個連續的）user 回合時完全不命中。`gemini-4-argon` 不支援明確快取；隱式快取是否命中仍由 Google 決定，不保證。
4. `final` 通過 `schema.json` 與 `validate_result.py` 規則後，`video_id`、`meta.duration`（實際影片長度）、`meta.crop` 的 `box`／`hands_box`（`set_crop` 確定的框）與 `meta.budget` 由 worker 依實際執行紀錄填入；`sequence_check.py` 的自動比對另存於 `sequence_check`，不寫進 result。
5. 結果 JSON、模型對話紀錄（`transcript.json`）與全部中間圖片（`artifacts.zip`，另含這次讀到的 SKILL.md 副本 `rules_SKILL.md`）上傳到 `gs://<bucket>/sop_runs/<video_id>/g<generation>/`。失敗的執行也會上傳對話紀錄，方便除錯。工作結果另記 `versions`（`{"skill": SKILL.md 版本}`）、`rules_digest`（skill 目錄全部檔案的 SHA-256）、`rules_changed_during_run`、`commit`（`GIT_COMMIT`）與 `finished_at`，供實驗紀錄 ZIP 使用。

前端完成後顯示每支影片的 30 步表格與流程觀察，可下載「步驟切分 CSV」，以及每支影片的結果 JSON、圖片 ZIP、模型對話紀錄（後端 `GET /api/v1/evaluations/{job_id}/sop-skill/{影片序號}/{result|artifacts|transcript}` 代為從 GCS 讀取）。API 建立工作時傳 `"pipeline": "sop_skill"` 即為此模式（不可同時傳 `selected_agents`）；省略時為原本的 `"agents"` 模式。

前置需求：預設的 Gemini 使用與 Agent A–D 相同的 Vertex AI 設定與 service account，不需額外啟用；只有改用 Claude 時，才需在 Vertex AI Model Garden 啟用對應的 Claude 模型。skill 目錄以唯讀方式掛載進 worker（`docker-compose.yml`）；修改 skill 後重啟 worker 即生效，不必重建映像。

| 設定 | 預設 | 意義 |
| --- | --- | --- |
| `SOP_SKILL_PROVIDER` | `gemini` | 執行 skill 的模型：`gemini`，或 `claude`（Vertex AI 上的 Claude） |
| `SOP_SKILL_GEMINI_MODEL` | `gemini-4-argon` | Gemini 模型 ID（region 沿用 `GCP_LOCATION`） |
| `SOP_SKILL_GEMINI_THINKING_LEVEL` | （空） | 選填：Gemini 的 `thinking_level`（例如 `low`、`high`），空白表示使用模型預設 |
| `SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS` | 0 | 選填：單回合輸出上限，0 表示使用模型預設 |
| `CLAUDE_MODEL_NAME` | `claude-opus-5-5` | `SOP_SKILL_PROVIDER=claude` 時的 Claude 模型 ID |
| `CLAUDE_VERTEX_REGION` | `global` | `SOP_SKILL_PROVIDER=claude` 時的 Vertex AI region |
| `CLAUDE_EFFORT` | `high` | `output_config.effort`（low／medium／high／xhigh／max） |
| `CLAUDE_MAX_TOKENS` | 64000 | 單回合輸出上限（final 含 30 步 evidence，需較大） |
| `CLAUDE_CALL_TIMEOUT_SECONDS` | 900 | 單回合呼叫的最長等待秒數 |
| `SOP_SKILL_CONCURRENCY` | 2 | 所有 worker 同時執行的 skill 數。skill 一次只送一個請求，但不計入 Agent A–D 的 5 路 Gemini 上限，兩者同時跑時 Gemini 最多會有 5＋2 個請求 |
| `SOP_SKILL_MAX_TURNS` | 60 | 每支影片的最多回合數（預算用滿約 42 回合） |
| `SOP_GRADING_DIR` | repo 內的 `sop-video-grading/`（容器內 `/sop-video-grading`） | 評分 agent 的 skill 目錄（SKILL.md、rubric、腳本） |
| `GIT_COMMIT` | （空） | 記進切分／評分結果與實驗紀錄 ZIP 的程式版本；啟動 compose 時帶入（見「實驗紀錄 ZIP」），空白時紀錄寫 `unknown` |
| `SOP_GRADING_CONCURRENCY` | 2 | 「切分＋評分」模式下，所有 worker 同時評分的影片數。每支影片的評分最多同時送 3 個 Gemini 請求，所以評分最多 2×3 個請求，同樣不計入 Agent A–D 的 5 路上限 |

Gemini 回傳 429／`RESOURCE_EXHAUSTED`、499，或 Claude 回傳 429／529（overloaded），且 SDK 重試用盡時，該影片的 skill 任務會依 queue 層退避重新派送，並從頭重跑該影片（最多 `GEMINI_QUEUE_MAX_ATTEMPTS` 次，之後工作失敗）；其他錯誤（模型拒答、連續 6 次不合格動作、超過回合上限）會讓工作失敗，可在前端「從失敗處繼續」只重跑未完成的影片。

### SOP 評分（開發中）

`sop-video-grading/` 以 sop-video-skill 的切分結果為輸入，依 `rubric.json` 對每一步輸出判定（正確／錯誤／部分正確／無法判定；已移除的步驟 11 為不適用）與說明。說明是評分的重點：每條標準都要寫看到什麼、哪條標準沒做到，並引用時間點與影格。

每條評分標準分成三類：

| 類別 | 判定方式 | 例子 |
| --- | --- | --- |
| `program` | 由切分結果以程式判定 | 步驟有沒有做 |
| `visual` | 評分 agent 看畫面（彈針連聲音一起聽） | 洗手是否 ≥ 15 秒（評分 agent 量時間、程式檢查秒數）、洗手七個動作（每 0.5 秒一格）、注視藥單 ≥ 10 秒、持毛筆姿勢、單手回套、彈針、雙手旋轉 vial（要看過片段才能判定） |
| `not_visible` | 影片看不到，固定輸出「無法判定」，不影響步驟判定 | 藥物是否正確、刻度（針頭斜面改看針筒方向，見 13b） |

步驟判定由程式從各條標準彙整：`key` 標準錯誤 → 錯誤；有標準判部分正確，或有錯誤也有正確 → 部分正確；有無法判定 → 無法判定；其餘 → 正確。

學生重複做同一個動作、有對有錯時（例如插針 3 次），評分 agent 判部分正確，並把該標準標記 `needs_review` 與 `review_reason`；說明依時間逐次寫出每一次的動作。標記會帶到整步（`needs_review: true`，說明末尾加「需人工複核：…」），`eval_grading.py` 會另外列出這些步驟，交由人工複核。

| 檔案 | 用途 |
| --- | --- |
| `rubric.json` | 30 步的評分標準、類別與 `key` |
| `schema.json` | 評分結果格式 |
| `scripts/rubric.py` | 讀取並檢查 rubric、彙整步驟判定 |
| `scripts/rules.py` | 由切分結果判定 `program` 標準，並為 `visual` 標準算出要看的影片區間（先標為待判定） |
| `scripts/validate_grading.py` | 驗證評分結果；`--final` 另外拒絕還在待判定的標準 |
| `SKILL.md` | 評分 agent 的 system prompt（看畫面的標準怎麼判、說明怎麼寫） |
| `actions.schema.json` | 評分 agent 的動作格式（`overview`、`zoom`、`burst`、`clip`、`final`），由 `scripts/validate_grading_action.py` 檢查 |
| `scripts/clip.py` | 從原檔剪出帶聲音的片段（裁到 context box），給彈針與洗手七個動作用 |
| `scripts/motion_peaks.py` | 以每秒 15 格分析任務區間內 hands_box 的畫面變化，找出手有明顯動作的時間點（不呼叫模型） |
| `scripts/audio_peaks.py` | 找出任務區間內短促的響聲與連續響聲（彈針聲），並判斷影片有沒有可用的聲音（不呼叫模型） |
| `scripts/burst.py` | 在 0.3–3 秒內平均取 3–12 張原始解析度的局部畫面，看不到 1 秒的短動作 |
| `scripts/eval_grading.py` | 與人工標註表比對：每步的人工／模型判定與說明、一致率、錯誤召回、誤報（另外列出有看畫面標準的步驟） |
| `scripts/compare_arms.py` | 對照實驗：在同一批影片上比較多組評分紀錄（例如評分 agent 與 Gemini 代理式影片解讀）的一致率、成對差異（McNemar）、重跑翻轉率與成本，列出各組判定不同的步驟（不呼叫模型） |
| `agentic/PROMPT.md` | 對照實驗用的提示詞：Gemini 直接看影片（`media_processing=agentic`）評分，後面接 `SKILL.md` 第 3–5 節與 `sop_steps.md`，判定規則和評分 agent 相同（見下方「對照實驗」） |
| `scripts/labels_to_segmentation.py` | 用人工標註表的開始／結束秒數組出切分結果，調整評分 agent 時可跳過切分 skill |
| `CHANGELOG.md` | 各版本的修改紀錄 |
| `EXPERIMENTS.md` | 調整評分 agent 的對照實驗、結論、已確認的標準定義與待辦 |

步驟定義沿用 `sop-video-skill/references/sop_steps.md`。

評分 agent（`backend/app/services/sop_grading_service.py`）把待判定的標準依 `window_steps` 分組（例如 15b 與 17a 共用步驟 15–17 的區間），每組跑一段短對話：外部程式先附上區間的 3×3 影格，需要聲音、每 0.5 秒一格或看連續動作（`rubric.json` 標 `clip: true`，例如 20a 雙手旋轉 vial）時改附 2 fps 的帶聲音片段；任務說明會列出程式算出的動作峰值，告訴模型短動作可能在哪裡（區間超過 180 秒時分段分析再合併，不會只看前 180 秒）；模型可再要求 `overview`／`zoom`／`burst`／`clip`（每組各 3／3／3／2 次），最後以 `final` 判定該組的每一條標準。不合格的動作（`validate_grading_action.py` 檢查不過，或腳本以 `REJECTED` 拒絕，例如要求的時間超出原檔長度）會連同錯誤訊息退回給模型，不執行也不扣預算；連續 4 次不合格，該組才算失敗。開場影格每 2–4 秒才一張，不到 1 秒的短動作（彈針、墊棉片、碰觸的瞬間）可能落在兩格之間，所以 SKILL.md 要求在峰值附近用 `burst` 確認；持續的持握姿勢（持毛筆、夾瓶、持針心尾端、單手回套）則在手穩定持握的時間點用 overview 或 zoom 看，判錯誤前要在兩個以上時間點都看到同樣的問題（依據見 `sop-video-grading/EXPERIMENTS.md`）。有幾條標準由 `validate_grading_action.py` 檢查判錯誤前用過對應的素材，否則退回：彈針（17a／27a）與針心往後抽（16a／26a）要先用過 `burst`，固定針頭（10b，旋緊是很短的來回動作）要先看過 `fps` ≥ 5 的 `clip`。片段帶聲音只有 Gemini 支援，所以評分一律使用 Gemini（`SOP_SKILL_GEMINI_MODEL`）。

#### 執行模式：SOP 步驟切分＋評分

評分表單的「執行模式」選 **SOP 步驟切分＋評分**（API 傳 `"pipeline": "sop_grading"`，不可同時傳 `selected_agents`）。每支影片在佇列裡依序跑兩個任務：

1. `sop_skill`：與「只跑 SOP 步驟切分」相同，切分結果存進該影片的 `sop_result`。
2. `sop_grading`：切分完成時在同一個交易排入。worker 從 GCS 下載原檔，以 `sop_result` 的切分結果執行 `sop_grading_service.run_grading`，把 `grading.json` 與各組對話紀錄、影格（`artifacts.zip`，不含片段影片；另含這次讀到的 `rules_rubric.json`、`rules_SKILL.md` 副本）上傳到 `gs://<bucket>/sop_runs/<video_id>/g<generation>/grading/`，結果存進 `grading_result`。每完成一組評分任務回報一次進度。

工作的總步數是「影片數 × 2」。有任務失敗或最終結果驗證不過時，會先上傳對話紀錄，並把已評好的標準存進該影片的 `grading_partial`：

- 失敗的任務全是 429／`RESOURCE_EXHAUSTED`、499 或逾時（配額暫時用完）時，與切分一樣由 queue 退避後自動重新派送（最多 `GEMINI_QUEUE_MAX_ATTEMPTS` 次），不必手動續跑。
- 其他失敗（例如連續 4 次不合格動作）會讓工作失敗，按「從失敗處繼續」。

不論哪一種，下一次嘗試都只把還沒評好的那幾組送給模型，已評好的標準沿用，也不會重新切分；新的 `artifacts.zip` 會併入前一次的對話紀錄與影格，下載時看得到每一組的依據。已評好的標準本身驗證不過時不保留，下一次改為整支重評。

另外幾種情況也不會丟掉已完成的評分：

- 進度回報（heartbeat）遇到資料庫短暫斷線時只記 log，評分照常進行。
- 評分完成但上傳 `artifacts.zip`／`grading.json` 到 GCS 失敗時，仍保存評分結果（`files` 為空並附 `upload_error`），只是沒有下載連結。
- 同一工作的另一支影片失敗後，這支影片正在進行的評分：已評好的標準存進 `grading_partial`；若剛好評完，結果直接保存為已完成，續跑時不再重評。

完成後的 `result` 為 `{"pipeline": "sop_grading", "items": [], "sop_skill": [...], "sop_grading": [...]}`，兩個陣列都依影片順序排列。`sop_grading` 的每一項含 `video_uri`、`result`（`sop-video-grading/schema.json`）、`tasks`、`failed_tasks`、`validation_errors`、`usage`、`files`、`model`、`criterion_texts`（評分標準 id → `rubric.json` 的文字，供前端顯示），以及 `versions`（`rubric`、評分 `skill` 版本）、`rules_digest`、`attempts`（每次嘗試的版本、`rules_digest`、commit 與開始時間；續跑會累加）、`mixed_rules`（續跑沿用的標準是在不同規則下評的；前一次沒有規則紀錄時為 null，表示不知道）、`rules_changed_during_run`、`commit`、`finished_at`（在這些欄位加入前跑完的工作沒有它們）。

前端每支影片先顯示評分摘要與 30 步評分，再顯示原本的步驟切分表：

- 得分 = 正確步數 ÷ 可判定步數（不適用、無法判定不計入分母），並列出各判定的步數與需人工複核的步數。
- 每一步可展開，查看各評分標準的判定、來源（程式規則／模型／固定）、說明、證據、時間窗與無法判定的原因。
- 可下載「SOP 評分 CSV」（每步一列），以及每支影片的評分 JSON 與對話紀錄 ZIP（`GET /api/v1/evaluations/{job_id}/sop-grading/{影片序號}/{result|artifacts}`，與切分的下載端點一樣只讀取本部署 bucket 的 `sop_runs/`）。
- 可下載「實驗紀錄 ZIP（含圖片）」：一支影片的切分＋評分整理成 `sop-video-grading/runs/` 的格式，見下方「實驗紀錄 ZIP」。

評分的 token 用量以 `SOP_grading` 階段記錄。

#### 實驗紀錄 ZIP

前端每支影片的「下載實驗紀錄 ZIP（含圖片）」（`GET /api/v1/evaluations/{job_id}/sop-grading/{影片序號}/record?video_id=VID_0083`）把疊代比對需要的東西放在一起，目錄和命令列的評分紀錄相同，只是版本資料夾後面多了工作編號：

```
VID_0083/rubric<版本>_skill<版本>_<模型>_seg-skill<切分版本>_<切分模型>_job<工作編號>-<序號>/
  record.json          版本、影片 SHA-256、commit、日期、評過的步驟、failed_tasks、token 用量，
                       以及 job_id、validation_errors、切分回合數、每次 queue 重試的用量（usage_by_attempt）、
                       規則紀錄（attempts、mixed_rules、rules_digest、rules_changed_during_run）、
                       缺少的檔案與原因（missing_files）
  segmentation.json    評分用的切分（video_id 改成 VID_xxxx）
  grading.json         評分結果（video_id 改成 VID_xxxx）
  run1/criterion_texts.json   當時的評分標準文字
  run1/seg/            切分的對話紀錄、motion.json、影格、rules_SKILL.md、sequence_check.json
  run1/grading/        各組評分對話紀錄、probe.json、影格、rules_rubric.json、rules_SKILL.md
```

- `video_id` 由前端從原始檔名取出 `VID_xxxx`（只送這個 ID，不送檔名），並顯示在下載按鈕上，下載前請確認。前後接著英數字（`VID_00831`、`XVID_0083`）或重拍編號（`VID_0083-2`、`VID_0083_2`）時不採用；這時資料夾改用 `job<工作編號>_<序號>`，`record.json` 的 `video_id` 為空。
- 影片 SHA-256 取自 `_src.mp4` 的檔名，就是上傳原檔的 SHA-256，與命令列紀錄可以直接比對。
- `commit` 來自 worker 的 `GIT_COMMIT`，啟動時帶入（PowerShell 寫法同上方命令列）：
  `GIT_COMMIT=$(git rev-parse HEAD)$(git diff --quiet HEAD -- || echo -dirty) docker compose up -d --build`。skill 與規則目錄是即時掛載的，啟動後才改或 `git pull` 的規則與腳本不會反映在 commit 上，所以每次執行另存 `rules_*` 副本，並記下兩個目錄全部檔案的 `rules_digest`（`runs/`、`review/`、隱藏檔與 `~` 備份檔除外，換行一律當 LF，Windows 與 Linux 的 checkout 結果相同）：digest 不同就表示規則或腳本不同，即使 commit 一樣；讀檔時檔案剛好被換掉則記為空字串（不知道），不會讓評分失敗。執行中規則被改，`rules_changed_during_run` 為 true；續跑時前後兩次的規則不同，`mixed_rules` 為 true，前一次的規則副本保留為 `rules_rubric.attempt<N>.json`、`rules_SKILL.attempt<N>.md`。這兩種結果都不該拿來比較版本。在這些欄位加入前跑完的工作，版本與 commit 寫 `unknown`。
- 解壓到 `sop-video-grading/runs/` 後，`run_record.py`、`eval_grading.py`、`review_page.py` 都能直接使用。資料夾名稱帶工作編號，所以不會覆蓋同版本的命令列紀錄或其他工作的紀錄；`sop_grading_cli` 的重複檢查會一併看同版本的 `<版本資料夾>_job*`，前端評過的步驟一樣要加 `--force` 才會重跑。圖片（`*.jpg`、`*.png`）已被 `.gitignore` 忽略：畫面有學生，只留在本機，commit 時只會帶到 JSON 與 Markdown。

#### 命令列

在本機逐支測試（轉檔 → 切分 → 評分；需要 Vertex AI 憑證，會計費）：

```bash
docker compose run --rm --build -e SOP_RUN_BY=你的名字 \
  -e GIT_COMMIT=$(git rev-parse HEAD)$(git diff --quiet HEAD -- || echo -dirty) \
  -v /path/to/videos:/data:ro -v /path/to/work:/work worker \
  python -m app.sop_grading_cli /data/影片.mp4 --video-id VID_0083 --out /work/VID_0083
# 加 --labels 時改用標註表的時間當切分結果，不跑 sop-video-skill（較快、較省）
docker compose run --rm ... worker python -m app.sop_grading_cli ... --labels /labels.csv
# 只重評某幾步（其餘沿用上次的 grading.json；重評失敗的步驟也保留上次的判定）
# 新的評分版本預設沿用同一切分版本的舊切分；加 --new-segmentation 才重新切分
docker compose run --rm ... worker python -m app.sop_grading_cli ... --new-segmentation
docker compose run --rm ... worker python -m app.sop_grading_cli ... --steps 15,17
python sop-video-grading/scripts/eval_grading.py --labels 標註表.csv sop-video-grading/runs/VID_0083/<版本>/grading.json
python sop-video-grading/scripts/run_record.py   # 列出所有評分紀錄（影片、版本、評過的步驟、誰、哪天）
```

PowerShell 的 commit 寫法：`-e GIT_COMMIT=$(git rev-parse HEAD)$(if (git status --porcelain --untracked-files=no) { '-dirty' })`；也可以改用 `--by`、`--commit` 參數。`--build` 讓容器跑的是目前的後端程式（映像檔不會自動更新，舊映像檔會和紀錄的 commit 不符）。

`--out` 目錄會留下轉檔後的 `source.mp4`、`segmentation.json`、`grading.json` 與每組的對話紀錄；已有 `segmentation.json` 時不會重新切分（寫評分紀錄時例外，見下方）。

**評分紀錄（`sop-video-grading/runs/`，要 commit）**：影片ID 是標記表的 `VID_xxxx` 時，每次執行都會記錄到 `runs/<影片ID>/rubric<版本>_skill<SKILL.md 版本>_<模型>_seg-<切分來源>/`，讓多人共用、避免重複付費。切分來源是 `skill<sop-video-skill 版本>_<切分模型>`（例如 `skill1.4.5_gemini-4-argon`），或用 `--labels` 時的 `labels-<標記表時間的雜湊>`（標記表時間一改就是新的紀錄）：

- `record.json`：版本、原始影片的 SHA-256，以及每次執行的執行者、日期、commit、該次影片的 SHA-256、評過的步驟（失敗任務的步驟不算）、`failed_tasks`、切分來源與 token 用量。切分是從其他紀錄沿用的，另有 `segmentation_from`（見下方「沿用切分」）。
- `segmentation.json`：這份紀錄固定使用的切分（第一次執行決定）；`grading.json`：最新的評分結果，之後的 `--steps` 都以它為基底。
- `run1/`、`run2/`…：該次寫出的評分對話（`grading/transcript_task_*.json`、`probe.json`，只收這次執行寫出的檔案）；該次有跑切分時另含 `seg/transcript.json`、`motion.json`、`probe.json`。
- 不存圖片與影片片段：對話紀錄列出每張 overview／zoom／burst／clip 的時間、張數與區域，`segmentation.json` 有裁切框，用原始影片（以 SHA-256 確認）在紀錄的 commit 上可以重新產生。
- 在 Linux 上，容器以 root 寫入的紀錄會改成 `runs/` 資料夾擁有者的檔案，不需要 sudo 就能 commit。

**人工核對頁面**：把需要人工確認的步驟做成本機網頁，邊看影片邊點同意／不同意（只需要 Python 標準函式庫，不用進容器）：

```bash
python sop-video-grading/scripts/review_page.py sop-video-grading/runs/VID_0083/<版本> sop-video-grading/runs/VID_0074/<版本> \
  --labels 標註表.csv --video-root /path/to/原始影片資料夾 [--items claude_items.json] [--all]
```

- 列入的項目：模型判定和標記表不同、或模型標了需人工複核的步驟（`--all` 改為所有有標記的步驟）；`--items` 加入 Claude 提出的建議判定或標準修改（格式見 `review_page.py` 開頭）。
- 頁面播放每個人自己電腦上的原始影片（`--video-root` 依標記表的相對路徑找檔，也可用 `--video VID_0083=路徑`；找不到時在頁面裡選檔），說明裡的時間點一下就跳過去，可慢放、逐 0.2 秒前後、放大手部區域；不複製任何影格。
- 每項可選「同意建議／同意標記表／改判（選正確判定）／待老師確認」並寫備註，進度存在瀏覽器；完成後「匯出 CSV」交回。決定綁定當時看到的建議、模型與標記表判定：重新產生頁面後只要其中一個變了，那一項就回到待核對。
- 同一支影片可以放兩個版本的紀錄一起比較，每個版本各自一項（卡片與 CSV 標出版本）；同一個版本給兩次會報錯。
- 預設輸出到 `sop-video-grading/review/`（已被 `.gitignore` 忽略：頁面含影片路徑，可能有學生姓名），不要 commit 頁面與匯出的 CSV。

呼叫模型前會先檢查（任何一項不過就停止，不花錢）：

- 要跑的步驟已經用相同版本（含切分來源）評過 → 拒絕，確定要重跑加 `--force`。
- 影片檔和紀錄的 SHA-256 不同 → 拒絕，`--force` 才跑（該次會記下自己的 SHA-256）。
- commit 結尾是 `-dirty`（有未 commit 的修改，紀錄無法重現）→ 拒絕，`--allow-dirty` 才跑。
- 沒有填執行者或 commit、紀錄資料夾無法寫入。執行者、commit 這類檢查在計算影片 SHA-256 之前。

一份紀錄只評一份切分：紀錄已存在時，`--out` 的 `segmentation.json` 換成紀錄的，`--steps` 以紀錄的 `grading.json` 為基底（不用本機舊檔）。紀錄還不存在時，`--labels` 會重建 `segmentation.json`（舊檔改名為 `segmentation.prev.json`）；不是這個 CLI 以同一個切分來源產生的 `segmentation.json`（例如舊的工作資料夾）會讓執行停止，請換一個 `--out`。其他影片ID（可能含學生姓名）不寫紀錄。worker 容器只有 `sop-video-grading/runs` 可寫，其餘 skill 目錄維持唯讀。

**沿用切分（避免重複付費切分）**：只改了評分版本（rubric 或評分 SKILL.md），切分版本（sop-video-skill 版本＋切分模型）沒變時，新版本第一次執行不會重新切分，而是複製同一支影片（SHA-256 相同）、同一切分版本的舊紀錄的 `segmentation.json`，前端實驗紀錄 ZIP 解壓後的紀錄（`_job*`）也算。有多份時優先選自己切分的（不是沿用來的）、最早的、命令列的。用未 commit 的程式（commit 結尾 `-dirty`）、commit 不明，或執行中規則被改過的紀錄不會被沿用。

- 評分模型和 Gemini 切分模型目前都讀 `SOP_SKILL_GEMINI_MODEL`，換評分模型就等於換切分模型，不會沿用。
- 沿用與否只看 sop-video-skill 的 `SKILL.md` 版本和切分模型。改了切分的程式或腳本（例如 `sop_skill_service.py`、`sop-video-skill/scripts/`）而影響切分結果時，要遞增 `SKILL.md` 版本，否則新的評分版本會沿用舊切分。

- 執行開始與結束時都會印出「注意：沿用既有的切分結果，這次沒有重新切分」以及來源紀錄、原本的切分者、日期與 commit。
- 新紀錄的 `record.json` 有 `segmentation_from`（來源紀錄資料夾、run、日期、執行者、commit、原本的切分來源；來源本身也是沿用的時，`copied_from` 記它的來源），第一個 run 的 `segmentation` 寫 `reused segmentation.json of <來源紀錄>`；`run_record.py` 的列表也會標「沿用 … 的切分，未重新切分」。新紀錄的 `run1/` 沒有 `seg/`，切分的對話紀錄在來源紀錄裡。
- 比較不同評分版本時，兩份紀錄用的是同一份切分，差異只來自評分；要看切分本身的變異，加 `--new-segmentation` 重新切分（只能用在還沒有紀錄的版本，紀錄已存在時這份紀錄的切分已固定）。
- `--out` 裡原有的 `segmentation.json` 會改名為 `segmentation.prev.json`（已存在時依序用 `prev2`、`prev3`…，不覆蓋），改用沿用的切分（紀錄裡的來源可追溯）。例外：這個 CLI 在同一個 `--out` 用同一切分版本切出、還沒寫進任何紀錄的切分（例如 `--new-segmentation` 跑到一半中斷）會保留使用，連同它的 `seg/` 對話紀錄記進新紀錄。寫進紀錄後 `segmentation.source` 第二行會標 `recorded`，之後的新版本就當成沿用那份紀錄。標了 `recorded` 卻找不到可沿用的來源紀錄（紀錄被刪、影片檔不同，或來源是 `-dirty`）時會停止，避免記成自己切分的；加 `--new-segmentation` 或換 `--out`。
- 切分標記加上切分模型之前的工作資料夾，`segmentation.source` 是舊格式（例如 `skill1.4.5`），沒有紀錄可沿用時會停止並提示：確認是用同一個模型切分的，可以把它改成新格式（例如 `skill1.4.5_gemini-4-argon`）。在此之前解壓的前端紀錄 ZIP 也是舊名稱，重複檢查與沿用切分都看不到，要用的話重新下載。
- 前端的「切分＋評分」流程不沿用，每個工作仍會重新切分。

用 `--labels` 時只取標註表的時間，不複製判定與說明（評分 agent 會看到切分依據，不能讓它看到答案）；沒有時間的步驟給前後步驟之間的區間，由評分 agent 自己判斷有沒有做。裁切框不在標註表裡，預設為整個畫面。此時「有沒有做」這類程式判定的標準等於照抄標註時間，所以評估評分 agent 時要看 `eval_grading.py` 的「看畫面的步驟（V）」那一行。

程式判定與驗證也可以單獨執行：

```bash
python sop-video-grading/scripts/rules.py segmentation.json -o grading.json
python sop-video-grading/scripts/validate_grading.py grading.json          # 允許待判定
python sop-video-grading/scripts/validate_grading.py grading.json --final  # 最終結果
```

人工標註表（`*影片標記表*.csv`／`.xlsx`）含學生姓名，已被 `.gitignore` 排除，只放在本機。

#### 對照實驗：評分看影片 vs 看圖片

`app.sop_agentic_cli` 讓 gemini-4-argon 直接看影片評分（不用 overview／zoom／burst／clip 的圖片與工具），和上面的評分 agent 比較（實驗設計與結果見 `sop-video-grading/EXPERIMENTS.md`）。看影片的方式有兩種：固定取樣（`Part.media_processing=STATIC`，預設）或代理式影片解讀（`AGENTIC`，模型自己跳轉時間軸、放大影格；task 模式會撞到 `TOO_MANY_TOOL_CALLS`，實驗已放棄）。

- `--mode e2e`：一次請求送整支影片，模型同時切分 30 步並判定所有看畫面的標準；程式判定的標準由 `rules.py` 依模型的切分判定，結果格式與 `validate_grading.py --final` 相同。
- `--mode task`：沿用同一版 sop-video-skill 的切分紀錄（和評分 agent 評的是同一份），每個任務一次請求、只送該任務區間的影片，不用 overview／zoom／burst／clip。
- `--media-processing static`：同樣的請求改用固定取樣，每秒 `--fps` 格（預設 2，和評分 agent 的 clip 相同；fps 會寫進版本的模型名稱，例如 `gemini-4-argon-static-fps2`）。`--mode task --media-processing static` 就是「評分 agent 的任務改看影片片段、不用圖片與工具」，是目前實驗採用的組別。

影片會上傳到 `GCS_BUCKET_NAME` 的 `sop_runs/agentic/`（Gemini 只能用 URI 讀長影片）。紀錄寫在 `sop-video-grading/runs/`，版本資料夾是 `rubric<R>_skillagentic<提示詞版本>-<mode>-s<SKILL.md 版本>_<模型>-<agentic|static>_seg-<agentic-e2e｜切分版本>`，不會和評分 agent 的紀錄撞名。兩種模式都評全部步驟，同版本重跑要加 `--force`（刻意重測穩定性）；每個 `runN/` 另存那次的 `grading.json`（e2e 另存 `segmentation.json`），`record.json` 的 `usage` 多記 `wall_seconds`（模型呼叫的時間，不含轉檔與上傳），對話紀錄有 `tool_use_tokens`（模型自己取的影格，算在 input）、`tool_calls` 與 `diag`（finish reason、HTTP 嘗試次數等）。空回覆會以原請求重送，最多連續 2 次。

```bash
docker compose run --rm --build -e SOP_RUN_BY=你的名字 -e GIT_COMMIT=... \
  -v /path/to/videos:/data:ro -v /path/to/work:/work worker \
  python -m app.sop_agentic_cli /data/影片.mp4 --video-id VID_0072 --out /work/VID_0072_video \
  --mode task --media-processing static          # 目前實驗的組別（--fps 預設 2）
docker compose run --rm ... worker python -m app.sop_agentic_cli ... --mode e2e   # 整支影片，代理式
python sop-video-grading/scripts/compare_arms.py --labels 標註表.csv \
  --arm 評分agent=rubric1.12_skill0.22_gemini-4-argon_seg-skill1.4.7_gemini-4-argon \
  --arm 影片=rubric1.12_skillagentic0.2-task-s0.22_gemini-4-argon-static-fps2_seg-skill1.4.7_gemini-4-argon \
  --group 驗證=VID_0091,VID_0095,VID_0096,VID_0097,VID_0100 [--corrections sop-video-grading/review/fix.json]
```

`compare_arms.py` 的評分 agent 秒數在沒有 `wall_seconds` 時是對話紀錄每回合模型秒數的加總（評分任務 3 組並行，比實際經過的時間長）；它的 token 含切分（沿用的切分算來源紀錄的）。

### Token 用量與成本

每一次成功的模型回應寫一列到 `model_usage`，記錄 evaluation、影片、階段（`Time_cuting`、`Agent_A`～`Agent_D`、`SOP_skill`、`SOP_grading`）、generation、queue attempt、provider 與實際使用的模型。JSON 驗證失敗後的重送與 SOP skill 的每一回合也都會計費，所以各自算一列；HTTP 失敗、沒有回應的嘗試沒有 token，不會記錄。工作被刪除時，對應的用量列也會一起刪除。

| 欄位 | 意義 |
| --- | --- |
| `input_tokens` | 以一般輸入單價計費的輸入 token（不含快取命中） |
| `cached_input_tokens` | 從快取讀取的輸入 token（Gemini 隱式快取、Claude cache read） |
| `cache_write_tokens` | 寫入快取的輸入 token（只有 Claude 有） |
| `output_tokens` | 以輸出單價計費的 token，包含思考 token |
| `thinking_tokens` | `output_tokens` 中屬於思考的部分（只有 Gemini 會回報） |

查詢方式：

- 前端：評分完成後按「下載 token 用量 CSV」。內容是每次回應一列，最後附上各模型合計與總計。檔案帶 UTF-8 BOM，Excel 可以直接開啟。
- API：`GET /api/v1/evaluations/{job_id}/usage`（JSON，含各模型合計），或 `GET /api/v1/evaluations/{job_id}/usage.csv`。

成本只在設定單價後才會計算。在 `.env` 設定 `MODEL_PRICES_JSON`，單位是每 1M token 的美元價格；JSON 要用單引號包起來：

```env
MODEL_PRICES_JSON='{"gemini-3.1-pro-preview": {"input": 2.0, "cached_input": 0.2, "output": 12.0}, "gemini-4-argon": {"input": 1.0, "output": 5.0}}'
```

上面的數字只是格式示範，請依 [Vertex AI 定價頁](https://cloud.google.com/vertex-ai/generative-ai/pricing) 填入實際單價。`cached_input`、`cache_write` 沒有填時，會沿用 `input` 的單價。沒有設定單價的模型，估計成本欄位留空，API 會列在 `unpriced_models`。單價在查詢時才套用，修改後重啟 backend，舊工作也會用新單價重新計算。Gemini 部分模型的輸入超過一定長度時會改用較高的單價，這裡沒有區分，需要精算時請以 GCP 帳單為準。

### 核心模組架構

1. **Frontend**: 純 HTML, CSS, 與 vanilla JavaScript。透過 Fetch API 下達評估任務，並透過 WebSocket 監聽伺服器事件更新 DOM。
2. **Backend**: Python FastAPI，提供 REST API 創建任務，以及管理 WebSocket 節點。
3. **Database (Message Broker)**: PostgreSQL，除了使用 SQLAlchemy ORM 保存狀態外，更兼任 Pub/Sub 訊息佇列，掌控並觸發全域系統事件。
4. **VLM/LLM Engine**: Vertex AI 上的 Gemini 模型，讀取 GCS 影片提供視覺解析與最終護理步驟檢核表的邏輯統整。

## GCP / gcloud 首次設定（首次使用必看）

本專案使用 Vertex AI（不是個人 Gemini API Key）。`gcloud` 是用來設定 Google Cloud 的命令列工具，不需要像伺服器一樣持續「啟動」；首次完成下列設定後，平常只要啟動本專案即可。若組內已提供 GCP 專案、bucket 和可用憑證，可直接跳到「建立 `.env`」。

### 1. 安裝並初始化 gcloud CLI

先安裝 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)，重新開啟終端機後確認指令可用：

```bash
gcloud --version
gcloud init
```

`gcloud init` 會引導登入並選擇預設專案；若瀏覽器沒有自動開啟，依終端機顯示的網址與驗證碼完成登入即可。日後若需要更換或重新登入 Google 帳號，再執行 `gcloud auth login`。

### 2. 選擇專案並啟用 API

請將下方的 `YOUR_PROJECT_ID` 換成實際的 GCP project ID（不是專案顯示名稱），且該專案必須已開通 Billing：

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com storage.googleapis.com
gcloud config get-value project
```

「只跑 SOP 步驟切分」模式預設使用 Gemini，不需額外設定；只有改用 Claude（`SOP_SKILL_PROVIDER=claude`）時，才需在 Vertex AI Model Garden 啟用 Claude 模型（見下方〈SOP 步驟切分〉）。

### 3. 建立 GCS bucket

Bucket 名稱在全球必須唯一；`YOUR_BUCKET_NAME` 請自行替換。Bucket 的區域不需要與下方的 `GCP_LOCATION=global` 相同：

```bash
gcloud storage buckets create gs://YOUR_BUCKET_NAME --location=asia-east1 --uniform-bucket-level-access
```

若組內已有 bucket，略過此步並直接使用既有名稱。

### 4. 建立 service account 並授權

以下權限讓後端能呼叫 Vertex AI，並只對指定 bucket 讀寫影片：

```bash
gcloud iam service-accounts create vlm-evaluator --display-name="VLM Evaluator"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:vlm-evaluator@YOUR_PROJECT_ID.iam.gserviceaccount.com" --role="roles/aiplatform.user"
gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET_NAME --member="serviceAccount:vlm-evaluator@YOUR_PROJECT_ID.iam.gserviceaccount.com" --role="roles/storage.objectAdmin"
```

`Storage Object Admin` 只允許管理 bucket 裡的影片物件，不包含修改 bucket 本身的設定。

### 5. 建立應用程式憑證

以下兩種方式擇一。建議優先使用「方式 A」的無私鑰認證；若組織政策禁止建立 service account 金鑰（錯誤訊息包含 `iam.disableServiceAccountKeyCreation`），也必須使用方式 A。

#### 方式 A：ADC + service account impersonation（推薦）

先查出目前登入帳號，將 `YOUR_GOOGLE_ACCOUNT` 替換成該 Email，再允許此使用者代用 service account：

```bash
gcloud auth list
gcloud iam service-accounts add-iam-policy-binding vlm-evaluator@YOUR_PROJECT_ID.iam.gserviceaccount.com --member="user:YOUR_GOOGLE_ACCOUNT" --role="roles/iam.serviceAccountTokenCreator" --condition=None
gcloud auth application-default login --impersonate-service-account="vlm-evaluator@YOUR_PROJECT_ID.iam.gserviceaccount.com"
```

最後一行會開啟瀏覽器，請使用同一個 Google 帳號完成授權。接著把產生的 ADC 檔複製到本專案的 `secrets/gcp-key.json`，讓 Docker Compose 可以掛載：

**Windows PowerShell：**

```powershell
New-Item -ItemType Directory -Force secrets
Copy-Item "$env:APPDATA\gcloud\application_default_credentials.json" ".\secrets\gcp-key.json"
```

**macOS / Linux：**

```bash
mkdir -p secrets
cp ~/.config/gcloud/application_default_credentials.json ./secrets/gcp-key.json
```

雖然此方式沒有 service account 私鑰，ADC 檔仍含有使用者授權資訊，必須視為敏感檔案保管。

#### 方式 B：service account JSON 金鑰

只有在組織允許建立金鑰時才使用此方式：

**Windows PowerShell：**

```powershell
New-Item -ItemType Directory -Force secrets
gcloud iam service-accounts keys create .\secrets\gcp-key.json --iam-account="vlm-evaluator@YOUR_PROJECT_ID.iam.gserviceaccount.com"
```

**macOS / Linux：**

```bash
mkdir -p secrets
gcloud iam service-accounts keys create ./secrets/gcp-key.json --iam-account="vlm-evaluator@YOUR_PROJECT_ID.iam.gserviceaccount.com"
```

> `secrets/` 已被 `.gitignore` 排除。無論使用 ADC 或 service account 金鑰，都請勿提交到 Git、貼到聊天室或公開分享；不再使用的 JSON 金鑰應至 IAM 撤銷。

### 6. 建立 `.env`

在專案根目錄建立 `.env`（同樣不會被 Git 追蹤）：

```env
GCP_PROJECT_ID=YOUR_PROJECT_ID
GCP_LOCATION=global
GCS_BUCKET_NAME=YOUR_BUCKET_NAME
GEMINI_MODEL_NAME=gemini-3.1-pro-preview
GCP_SA_KEY_PATH=./secrets/gcp-key.json
# 選填：SOP 步驟切分模式使用的 Gemini 模型（預設即為 gemini-4-argon）
SOP_SKILL_GEMINI_MODEL=gemini-4-argon
```

可用以下指令確認目前 gcloud 登入帳號、專案與 bucket：

```bash
gcloud auth list
gcloud config get-value project
gcloud storage ls gs://YOUR_BUCKET_NAME
```

## 如何啟動執行

### 方式一：使用 Docker 快速啟動（推薦 ✨）
為解決環境相依性與資料庫建構繁瑣的問題，本專案已支援 Docker 微服務容器化部署。
請確保系統已安裝 [Docker Desktop](https://www.docker.com/products/docker-desktop/) 與 NVIDIA Container Toolkit，已完成上方「GCP 設定」及 Gazelle checkpoint 設定：
1. 進入專案根目錄 (`my-awesome-project`) 開啟終端機。
2. 執行以下指令，建立 PostgreSQL、schema 初始化、FastAPI、一般 worker 與必要的 Gazelle GPU worker（Docker Compose 會自動讀取根目錄的 `.env`）：
   ```bash
   docker compose up -d --build
   ```
3. 查看後端啟動狀態與日誌：
   ```bash
   docker compose ps
   docker compose logs -f backend worker gazelle-worker
   ```
4. 看到 Uvicorn 啟動完成後，開啟 `http://localhost:8000/docs`；能看到 FastAPI API 文件即表示後端已成功啟動。按 `Ctrl+C` 只會停止追蹤日誌，不會關閉容器。

停止服務：

```bash
docker compose down
```

若出現認證或 bucket 權限錯誤，先確認 `.env` 內的 project/bucket 是否正確，以及 `GCP_SA_KEY_PATH` 指向的 ADC 或金鑰 JSON 檔確實存在；修改 `.env` 後請重新執行 `docker compose up -d --build`。`docker-compose.yml` 會把 `GCP_PROJECT_ID` 同時設成 `GOOGLE_CLOUD_PROJECT`，所以使用不含 project 的 ADC 檔時，不會再出現 `No project ID could be determined` 警告；手動本機執行時若看到這個警告，可以忽略，或自行設定 `GOOGLE_CLOUD_PROJECT`。

從舊版升級時先 `docker compose stop backend worker gazelle-worker`（舊版沒有對應 worker service 時可忽略），再 `docker compose up -d --build`。`init` 使用 PgQueuer 官方 install/upgrade 介面與 durable 預設建表；API、一般 worker 與 Gazelle worker 在初始化完成後才啟動。統一 5 FPS 來源的首次升級會把所有未完成或已失敗的舊格式工作標記為不可重試的 `retired`，並清除舊 queue 任務；需用原始影片重新上傳後再提交。已完成工作的結果，以及已失敗工作的原始錯誤結果會保留。升級是向前遷移，不要同時執行新舊 worker，也不要刪除 PostgreSQL volume。

從只有 Agent A overlay 的版本升級到 Agent A／D 各自產生 overlay 的版本時，`init` 會一次性檢查未完成或可重試工作。若尚未完成的 Agent 缺少可用的對應 overlay，工作會標記為可重試的 `failed`，並顯示需要重新產生 gaze 來源的訊息；點選「重試」即可重新執行受影響影片的 Gazelle 階段，再完成剩餘評分。已完成的切段與 Agent 評分會保留，進度會扣除失效的 gaze 階段；舊佇列或執行中的結果會被 generation 檢查忽略。已完成的工作、具有完整來源的工作，以及仍可使用舊 overlay 的 Agent A 工作不受此遷移影響。

舊版選到 Agent D、未選 Agent A 且 gaze 狀態為 `skipped` 的工作，會繼續使用原影片與 Agent D 時段評分，重試也保留此方式與原本的進度總數。新建立的 Agent D 工作仍必須完成 Gazelle 前處理後才評分。

`db` 服務的 5432 port 只綁定在本機（`127.0.0.1`），區網其他電腦連不到預設帳密 `postgres/postgres` 的資料庫；本機的 psql 或資料庫工具照常用 `localhost:5432` 連線。

若要增加 worker：`docker compose up -d --scale worker=2`，所有 worker 使用同一份環境設定。PgQueuer 預設 heartbeat timeout 為 30 秒，中斷任務會在 heartbeat 過期後重新派發。`pgq` 管理指令使用 PostgreSQL 的 `PGHOST/PGUSER/PGPASSWORD/PGDATABASE` 環境變數；本專案的 `app.bootstrap`／`app.worker` 則使用 `DATABASE_URL`。

### 方式二：手動本機環境設定
1. 使用 Python 3.11+、PostgreSQL 15，以及 **ffmpeg**。Gazelle worker 還需要 NVIDIA CUDA GPU、相容的驅動程式與 Git。PgQueuer 依賴 uvloop，Windows 請使用 Docker 或 WSL 執行後端／worker。
2. 設定資料庫連線變數 (或直接使用預設 `postgresql://postgres:postgres@localhost:5432/vlm_eval`)。
3. 設定「GCP 設定」小節列出的環境變數，並將 `GOOGLE_APPLICATION_CREDENTIALS` 指向前面建立的 `secrets/gcp-key.json`（可以是 ADC 或 service account 金鑰）。注意：本機啟動時 Python 不會自動載入根目錄的 `.env`，必須先把變數載入目前的終端機工作階段。

   **Windows PowerShell：**
   ```powershell
   $env:GCP_PROJECT_ID="YOUR_PROJECT_ID"
   $env:GCP_LOCATION="global"
   $env:GCS_BUCKET_NAME="YOUR_BUCKET_NAME"
   $env:GEMINI_MODEL_NAME="gemini-3.1-pro-preview"
   $env:GOOGLE_APPLICATION_CREDENTIALS=(Resolve-Path ".\secrets\gcp-key.json").Path
   ```

   **macOS / Linux：**
   ```bash
   export GCP_PROJECT_ID="YOUR_PROJECT_ID"
   export GCP_LOCATION="global"
   export GCS_BUCKET_NAME="YOUR_BUCKET_NAME"
   export GEMINI_MODEL_NAME="gemini-3.1-pro-preview"
   export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/secrets/gcp-key.json"
   ```
4. 安裝相依套件：
   ```bash
   pip install -r backend/requirements.txt
   ```
5. 進入 `backend` 資料夾，初始化 schema 並啟動 Server：
   ```bash
   cd backend
   python -m app.bootstrap
   uvicorn app.main:app --reload
   ```
6. 在另一個使用相同環境變數的終端機，進入 `backend` 並執行 `pgq run app.worker:main`。
7. 在有 CUDA GPU 的環境另開終端機，使用相同的 `DATABASE_URL`、GCP 設定與憑證，安裝 Gazelle worker 相依套件（以下指令從專案根目錄執行）：
   ```bash
   export GAZELLE_REF="YOUR_VERIFIED_GAZELLE_COMMIT_SHA"
   pip install torch==2.3.1 torchvision==0.18.1 --index-url https://download.pytorch.org/whl/cu121
   pip install -r backend/requirements.gazelle.txt
   git clone https://github.com/fkryan/gazelle.git gazelle
   git -C gazelle checkout "$GAZELLE_REF"
   pip install -e ./gazelle
   export GAZELLE_CHECKPOINT_PATH="$(pwd)/models/gazelle.pt"
   cd backend
   pgq run app.gazelle_worker:main
   ```
   將 `GAZELLE_REF` 的範例值換成已驗證的 commit SHA，並把 checkpoint 放在指定路徑；`GAZELLE_CHECKPOINT_PATH` 必須是 worker 可讀的實際路徑。使用 WSL 時，在 WSL 終端機執行上述指令。一般 worker 與 Gazelle worker 必須同時運行，否則 `gazelle_inference` 任務會留在佇列中。
8. 開啟 `http://localhost:8000/docs` 確認後端成功啟動。

### 前端執行方式
1. 無需特別的伺服器。請使用檔案總管進入 `frontend` 資料夾，直接**對著 `index.html` 點擊兩下**開啟，或是將 `index.html` 檔案**直接拖曳到您的瀏覽器視窗**中。
   *(注意：請不要直接在網址列手動輸入 `frontend/index.html`，否則瀏覽器會當成網址搜尋而報錯)*
2. 選擇評估項目後，在「Upload Videos to GCS」選取本機影片檔案並按下「Upload to GCS」——內容相同的影片（依 SHA-256 判斷，與檔名無關）若已存在於 bucket 中會直接沿用，不會重複上傳。也可以在下方文字框貼上本系統先前產生的 `gs://` 路徑。
3. 點擊「Start Evaluation」進行即時評分！結果會依 Agent 分組顯示，並提供「View Raw JSON Response」查看模型原始輸出，方便測試/調整 prompt。完成後可用「下載評分總表 CSV」與「下載完整分析資料 CSV」匯出結果，用「下載 token 用量 CSV」查看每次模型呼叫的 token 數與估計成本。執行模式選「SOP 步驟切分＋評分」時，改為顯示 SOP 評分與步驟切分表，並可下載「SOP 評分 CSV」與「步驟切分 CSV」。

## 測試與 CI

測試方式與驗證範圍見 [TESTING.md](TESTING.md)。GitHub Actions 設定在 `.github/workflows/`：

| Workflow | 觸發 | 內容 |
| --- | --- | --- |
| `python-ci.yml` | push 或 PR 到 `main`、`release/**` | 先跑 `tests/sop_skill/`、`tests/sop_grading/` 與前端 Node 測試，再以 PostgreSQL 跑後端 pytest |
| `claude-code-review.yml` | PR 開啟、推送新提交、重新開啟或轉為可審查 | Claude 以 code-review 外掛審查 PR，結果以行內留言貼回（草稿與 fork 的 PR 不審查） |
| `claude.yml` | issue／PR 留言、review 或新 issue 中提到 `@claude` | Claude 回應並可推送提交；只有具寫入權限的人能觸發 |

兩個 Claude workflow 需要在 repo secrets 設定 `CLAUDE_CODE_OAUTH_TOKEN`。注意 `python-ci.yml` 只對目標為 `main`／`release/**` 的 PR 執行，疊在其他功能分支上的 PR 需要在本機自行跑測試。

協作規則（溝通語言、模型權重不得提交、測試範圍）寫在 [AGENTS.md](AGENTS.md)，`CLAUDE.md` 會引用它。

## 目錄結構
```text
my-awesome-project/
├── .github/workflows/
│   ├── python-ci.yml
│   ├── claude-code-review.yml
│   └── claude.yml
├── docker-compose.yml
├── AGENTS.md
├── CLAUDE.md
├── TESTING.md
├── locustfile.py
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   ├── csv-export.js
│   ├── recovery.js
│   ├── sop-result.js
│   └── sop-grading.js
├── backend/
│   ├── Dockerfile
│   ├── Dockerfile.gazelle
│   ├── .dockerignore
│   ├── requirements.txt
│   ├── requirements.gazelle.txt
│   ├── app/
│   │   ├── api/
│   │   │   └── endpoints/
│   │   │       ├── evaluations.py
│   │   │       └── uploads.py
│   │   ├── models/
│   │   │   └── evaluation.py
│   │   ├── prompts/
│   │   │   ├── Time_cuting.txt
│   │   │   ├── Agent_A.txt
│   │   │   ├── Agent_B.txt
│   │   │   ├── Agent_C.txt
│   │   │   └── Agent_D.txt
│   │   ├── schemas/
│   │   │   └── evaluation.py
│   │   ├── services/
│   │   │   ├── agents.py
│   │   │   ├── evaluation_queue.py
│   │   │   ├── gazelle_service.py
│   │   │   ├── gcs_service.py
│   │   │   ├── gemini_service.py
│   │   │   ├── model_logging.py
│   │   │   ├── sop_agentic_service.py
│   │   │   ├── sop_grading_service.py
│   │   │   ├── sop_run_bundle.py
│   │   │   ├── sop_skill_service.py
│   │   │   ├── usage.py
│   │   │   └── video_service.py
│   │   ├── bootstrap.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── gazelle_worker.py
│   │   ├── main.py
│   │   ├── sop_agentic_cli.py
│   │   ├── sop_grading_cli.py
│   │   ├── worker.py
│   │   └── ws_manager.py
│   └── tests/
├── sop-video-skill/
│   ├── SKILL.md
│   ├── CHANGELOG.md
│   ├── schema.json
│   ├── actions.schema.json
│   ├── references/
│   │   └── sop_steps.md
│   └── scripts/
├── sop-video-grading/
│   ├── SKILL.md
│   ├── CHANGELOG.md
│   ├── EXPERIMENTS.md
│   ├── rubric.json
│   ├── schema.json
│   ├── actions.schema.json
│   ├── agentic/         # 對照實驗：Gemini 代理式影片解讀的提示詞（sop_agentic_cli）
│   ├── runs/            # sop_grading_cli／sop_agentic_cli 的評分紀錄（commit 共用）
│   ├── review/          # review_page.py 產生的本機人工核對頁面（gitignored）
│   └── scripts/
├── tests/
│   ├── sop_skill/
│   └── sop_grading/
└── README.md
```
