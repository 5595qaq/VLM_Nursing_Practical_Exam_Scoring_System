# VLM+LLM Nursing Practical Exam Scoring System

這是一個基於 VLM (視覺語言模型) 與 LLM 架構的護理術科評分系統。系統包含 JavaScript 前端與 FastAPI (Python) 後端。影片先上傳到 Google Cloud Storage (GCS)，後端透過 Vertex AI 呼叫 Gemini 模型對 `gs://` 路徑的影片進行解析與術科自動評分。

> 目前僅供組內測試使用，前端是設計給組員測試 prompt/影片評分效果的工具，尚未考慮一般使用者的多人權限管理。

## 系統特色

- **事件驅動架構 (Event-Driven)**：採用高擴充性的 Worker 排程概念，完全解耦 API 請求與耗時推論任務。
- **即時進度監控**：前端透過 WebSocket 即時取得任務執行進度 (Uploading -> Processing -> Scoring)，並在頁面上呈現終端機風格的進度條與日誌。
- **資料庫狀態持久化**：所有的任務執行狀態與最終判定結果會被記錄至 PostgreSQL 資料庫中。
- **GCS 影片上傳與內容去重**：前端可直接選取本機影片；後端依原始內容的 SHA-256 將轉檔存為 `videos/{sha256}_5fps.mp4`，相同內容直接沿用，不同內容即使同名也不會互相覆蓋。手動貼入時只接受本系統先前產生、位於設定 bucket 的 normalized URI。
- **單一 5 FPS 影片來源**：後端用 ffmpeg 將影片統一轉成 5 FPS（H.265）；Gemini 與 Gazelle 共用同一個 GCS 物件。
- **Vertex AI 認證**：後端統一使用 GCP service account 認證 Vertex AI／GCS，組員不需要各自準備或輸入 Gemini API Key。
- **彈性結果格式**：多個 Agent 產出的評分 JSON 欄位尚未統一，前端以通用卡片＋原始 JSON 檢視的方式呈現，方便邊測 prompt 邊看結果。
- **時間分段＋可選 Agent 評分**：`Time_cuting.txt` 先對完整影片找出四個重疊時間區段，再執行所選的 Agent_A ~ Agent_D；各 Agent 的 prompt 分別存放於 `backend/app/prompts/Agent_A.txt` ~ `Agent_D.txt`。評分表單可複選 Agent，預設全選；選到 Agent_A 或 Agent_D 時會先執行 Gazelle，兩者皆未選時才跳過 Gazelle。
- **PostgreSQL 持久佇列**：PgQueuer 管理派送、去重與中斷恢復；每個評分工作最多 10 支活動影片，所有 worker 合計最多 5 個 Gemini 呼叫。

## 系統運作流程與架構

API 與 PgQueuer worker 分開執行，共用 PostgreSQL，流程如下：

0. **[影片上傳]**：前端選取本機影片，轉成內容雜湊命名的 5 FPS 影片並上傳到 GCS，取得唯一的 `gs://` 路徑；也可以貼上本系統先前產生的 normalized URI。
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

`execute_after` 是最早執行時間，不是保證的全域 RPM 限流；積壓任務或多個評分工作仍可能同時就緒。5 路是本專案的控制值，不是 Google 配額保證。

Vertex AI 回傳 429／`RESOURCE_EXHAUSTED`、499／`CANCELLED`，或呼叫超過上述 timeout 時，該模型任務會保留在 PgQueuer 並按 queue 層設定延遲重派，不會將整個評分工作標記為失敗。其他模型錯誤仍維持終止工作的行為，且可在前端保留既有進度後手動續跑。

SDK 對 408、429、500、502、503、504 及其支援的暫時性網路錯誤執行指數退避：1 秒起跳、倍率 2、最高 60 秒、jitter 1。400、401、403、404 不重試；應用層另以 `GEMINI_CALL_TIMEOUT_SECONDS` 限制整次呼叫。JSON／切段驗證最多重試一次；10 支影片正常為 50 次邏輯呼叫，只有切段重試時最多 60 次，若切段及評分都各重試一次則最多 100 次，HTTP attempts 另計。

任一任務用盡重試後整個 evaluation 失敗，後續排隊任務跳過，已在執行的結果不再寫入。結果順序固定為輸入影片順序，再依所選 Agent A–D。每支影片的進度步驟為切段、所選 Agent，以及選到 Agent_A 或 Agent_D 時的 Gazelle。重試沿用建立工作時的 Agent 選擇。API 建立 evaluation 時可傳入 `selected_agents`，例如 `["Agent_B"]`；省略時執行全部，空清單、重複或未知 Agent 會回傳 422。

### Gazelle gaze preprocessing

上傳 API 只產生一支 `videos/{sha256}_5fps.mp4`。Gemini 時間切段及 Agent B、C 直接使用這支影片；獨立 GPU worker 會分別對 Agent A 與 Agent D 的時段執行 Gazelle，產生各自的紫色注視點影片與逐幀 JSON，再用對應的 overlay 啟動 Agent A／D。

1. 下載 `gazelle_dinov2_vitb14_inout.pt` checkpoint，存成 `./models/gazelle.pt`（或設定 `GAZELLE_CHECKPOINT_PATH` 指向原檔名）。
2. 將 `GAZELLE_REF` 設為部署驗證過的 Gazelle commit SHA；未設定時 Docker build 使用 `main`，僅適合開發。
3. 安裝 NVIDIA Container Toolkit；Gazelle 是 Agent A 與 Agent D 的必要前置，標準的 `docker compose up -d --build` 會自動啟動 GPU worker。

可用 `GAZELLE_MODEL_NAME`、`GAZELLE_MODEL_VERSION`、`GAZELLE_INOUT_THRESHOLD` 與 `GAZELLE_DOT_RADIUS` 調整模型與疊點行為。`GAZELLE_MODEL_NAME` 必須選擇帶有 in/out head 的 `_inout` 模型，並使用對應 checkpoint；Gazelle worker 會在啟動時拒絕不支援的名稱。正式環境應固定 Git commit、checkpoint 檔及 DINOv2 快取版本。

使用 `pgqueuer==1.4.0`：原方案的 1.0.2 經雙 worker 測試曾超出 5 路；1.4.0 包含官方 [capacity slots 修正](https://github.com/janbjorge/pgqueuer/pull/777)。SDK 固定為已驗證的 `google-genai==2.23.0`。

PgQueuer 提供 at-least-once delivery。已提交結果會跳過重跑，重複完成不會重複累計；但若 Gemini 成功後、資料庫提交前 worker 中斷，恢復時可能再次呼叫並計費。也不能以本地 worker 的名額保證被中斷的遠端 Google 請求已立即停止。

`docker compose logs -f worker` 可查看 `gemini_http_attempt` JSON 日誌，包含 queue/evaluation/video/agent ID、queue attempt、HTTP attempt、狀態碼、重試間隔及耗時；不記錄影片內容或認證標頭。

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

若出現認證或 bucket 權限錯誤，先確認 `.env` 內的 project/bucket 是否正確，以及 `GCP_SA_KEY_PATH` 指向的 ADC 或金鑰 JSON 檔確實存在；修改 `.env` 後請重新執行 `docker compose up -d --build`。

從舊版升級時先 `docker compose stop backend worker gazelle-worker`（舊版沒有對應 worker service 時可忽略），再 `docker compose up -d --build`。`init` 使用 PgQueuer 官方 install/upgrade 介面與 durable 預設建表；API、一般 worker 與 Gazelle worker 在初始化完成後才啟動。統一 5 FPS 來源的首次升級會把所有未完成或已失敗的舊格式工作標記為不可重試的 `retired`，並清除舊 queue 任務；需使用本系統產生的 5 FPS URI 重新提交。已完成工作的結果，以及已失敗工作的原始錯誤結果會保留。升級是向前遷移，不要同時執行新舊 worker，也不要刪除 PostgreSQL volume。

從只有 Agent A overlay 的版本升級到 Agent A／D 各自產生 overlay 的版本時，`init` 會一次性檢查未完成或可重試工作。若尚未完成的 Agent 缺少可用的對應 overlay，工作會標記為可重試的 `failed`，並顯示需要重新產生 gaze 來源的訊息；點選「重試」即可重新執行受影響影片的 Gazelle 階段，再完成剩餘評分。已完成的切段與 Agent 評分會保留，進度會扣除失效的 gaze 階段；舊佇列或執行中的結果會被 generation 檢查忽略。已完成的工作、具有完整來源的工作，以及仍可使用舊 overlay 的 Agent A 工作不受此遷移影響。

舊版選到 Agent D、未選 Agent A 且 gaze 狀態為 `skipped` 的工作，會繼續使用原影片與 Agent D 時段評分，重試也保留此方式與原本的進度總數。新建立的 Agent D 工作仍必須完成 Gazelle 前處理後才評分。

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
2. 選擇評估項目後，在「Upload Videos to GCS」選取本機影片檔案並按下「Upload to GCS」——同檔名的影片若已存在於 bucket 中會直接沿用，不會重複上傳。也可以在下方文字框直接貼上已存在的 `gs://` 路徑。
3. 點擊「Start Evaluation」進行即時評分！結果會依 Agent 分組顯示，並提供「View Raw JSON Response」查看模型原始輸出，方便測試/調整 prompt。

## 目錄結構
```text
my-awesome-project/
├── docker-compose.yml
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── backend/
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── requirements.txt
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
│   │   │   ├── gcs_service.py
│   │   │   ├── gemini_service.py
│   │   │   └── video_service.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── main.py
│   │   └── ws_manager.py
└── README.md
```
