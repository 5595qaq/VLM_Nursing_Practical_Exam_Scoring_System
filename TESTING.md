# 測試

測試使用 Python 3.11+、真實 PostgreSQL 15、PgQueuer 1.4.0。Gemini HTTP 與 GCS 都以 mock 取代，不需要 Google 憑證或付費呼叫。

## 執行

建立隔離資料庫（不要使用正式或開發資料庫）：

```bash
docker run --name vlm_test_db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=vlm_eval_test -p 55432:5432 -d postgres:15
```

在 Linux、macOS 或 WSL：

```bash
pip install -r backend/requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:55432/vlm_eval_test
pytest backend/tests -q
```

Windows 建議使用 Docker／WSL，因為 PgQueuer 使用 uvloop。亦可在安裝好依賴的容器內執行上述 pytest，將 DATABASE_URL 指向測試容器。

只有需要 DB 的 fixtures 才會初始化 schema；資料庫名稱必須以 `_test` 結尾，否則拒絕執行。每項整合測試會清除這個測試資料庫的評分工作與 queue rows；不會建立、連線或清除正式資料庫。測試不支援多個 pytest runner 同時共用一個測試 DB。

純 SDK／in-memory 測試可不啟動 PostgreSQL：

```bash
pytest backend/tests/test_agents.py backend/tests/test_model_retries.py -q
pytest backend/tests/test_worker_reliability.py -k in_memory -q
```

SOP skill（`sop-video-skill/`）的結果驗證（`schema.json`、`sop-video-skill/scripts/`）不需要資料庫，測試在 `tests/sop_skill/`：

```bash
pip install -r tests/sop_skill/requirements.txt
pytest tests/sop_skill -q
python sop-video-skill/scripts/validate_result.py result.json   # 驗證一份 final 結果
python sop-video-skill/scripts/schema_tools.py sop-video-skill/schema.json > out.json    # 產生 Structured Outputs 用的 schema
python sop-video-skill/scripts/schema_tools.py sop-video-skill/actions.schema.json > actions.json  # 動作 schema（自動嵌入 schema.json）
```

SOP 評分（`sop-video-grading/`）的評分標準、程式判定與結果驗證也不需要資料庫，測試在 `tests/sop_grading/`。測試會先用 sop-video-skill 的 `validate_result.py` 確認輸入的切分結果合法，再檢查 `rules.py` 的判定與 `validate_grading.py` 的每條規則：

```bash
pip install -r tests/sop_grading/requirements.txt
pytest tests/sop_grading -q
python sop-video-grading/scripts/rules.py segmentation.json -o grading.json   # 由切分結果產生評分結果（visual 標準為待判定）
python sop-video-grading/scripts/validate_grading.py grading.json --final     # 驗證最終評分結果
python sop-video-grading/scripts/eval_grading.py --labels 標註表.csv grading.json  # 與人工標註比對
python sop-video-grading/scripts/labels_to_segmentation.py --labels 標註表.csv --video-id VID_0083 --duration 424 -o seg.json  # 用標註時間組出切分結果
```

`tests/sop_grading/test_clip.py`、`test_motion_burst.py` 與 `test_audio_peaks.py` 需要 ffmpeg／ffprobe（沒有時自動略過；burst 的圖片測試另外需要 OpenCV）。評分 agent 迴圈（`backend/tests/test_sop_grading_service.py`）用 ffmpeg 產生 60 秒帶聲音的合成影片，以真實腳本執行 overview／zoom／burst／clip／final（含「需人工複核」標記帶到結果、彈針任務列出聲音峰值、沒有聲音的影片不能判彈針錯誤、洗手計時沒填 span 會被退回、腳本拒絕的動作退回給模型而不讓任務失敗），模型以假回覆取代，不需資料庫與 Google 憑證：

```bash
pytest backend/tests/test_sop_grading_service.py -q
```

`sop_grading_cli` 的評分紀錄（`backend/tests/test_sop_grading_cli.py`）以假的轉檔與評分取代，不需 ffmpeg、資料庫與 Google 憑證：檢查同版本重複執行、影片 SHA-256 不符、未 commit 的修改、沒填執行者或 commit（在計算 SHA-256 之前）、紀錄無法寫入、來源不明的本機切分時都在呼叫模型前停止；紀錄只存這次執行寫出的對話；`--steps` 以紀錄的切分與評分為基底而不是本機舊檔；`--labels` 是另一份紀錄且會重建切分；`--force` 換影片時該次記下自己的 SHA-256。新的評分版本沿用同一切分版本（含切分模型）、同一影片檔的舊切分而不重新切分，印出沿用提示並在 `record.json` 記下 `segmentation_from`；換影片檔或加 `--new-segmentation` 時重新切分，紀錄已存在時 `--new-segmentation` 在計算 SHA-256 前被拒絕；`-dirty` 紀錄的切分不沿用；還沒寫進紀錄的本機切分（含 `seg/` 對話紀錄）會保留；換下的本機切分不覆蓋舊的 `segmentation.prev.json`。`run_record.reproducible` 也在 `test_run_record.py`。`run_record.segmentation_source` 的挑選順序（自己切分的優先、最早、命令列優先）與 `skill_tag` 在 `tests/sop_grading/test_run_record.py`；前端紀錄 ZIP 的切分標記與 `run_record.skill_tag` 一致在 `test_sop_run_bundle.py`。`run_record.py` 的純函式測試在 `tests/sop_grading/test_run_record.py`（含 Linux 上 root 寫出的檔案改回 `runs/` 擁有者）。

人工核對頁面（`review_page.py`）的 Python 部分在 `tests/sop_grading/test_review_page.py`：列入哪些步驟、重複步驟的時間、同一支影片兩個版本各自一項、同一版本重複時報錯、Claude 建議的合併、依標記表相對路徑找影片、說明文字不能提早結束頁面的 script。頁面裡的互動（點時間跳轉、手部放大、決定按鈕、重新整理後保留進度、建議改變後回到待核對、匯出 CSV、找不到影片時選檔）沒有自動測試，修改頁面腳本後要在瀏覽器裡實際操作一遍。

```bash
pytest backend/tests/test_sop_grading_cli.py -q
```

前端的實驗紀錄 ZIP（`backend/app/services/sop_run_bundle.py`）的組裝在 `backend/tests/test_sop_run_bundle.py`，不需資料庫：版本資料夾名稱與 `run_record.record_dir` 一致（另加工作編號）、步驟與失敗任務的算法與 `run_record.py` 一致、解壓後 `run_record.py` 讀得到、`grading.json`／`segmentation.json` 的 `video_id` 改成 `VID_xxxx`、圖片不重新壓縮、上傳 zip 裡巢狀或 `../` 的檔名不會被複製、讀不到的 zip 列出原因、舊工作沒有版本與 commit 時寫 `unknown`、token 用量依 queue 重試分組。下載端點（`test_api.py` 的 `test_download_sop_grading_record*`，需要 PostgreSQL）檢查 `video_id` 只接受 `VID_xxxx`、不讀取本部署 `sop_runs/` 以外的檔案、GCS 讀不到圖片時仍回傳 JSON 紀錄。規則快照、`rules_digest`（不受 `runs/` 新增紀錄、隱藏檔、備份檔與 CRLF 影響；檔案讀到一半消失時為空字串）與續跑的 `attempts`（規則改變時 `mixed_rules` 為 true 並保留前一次的副本；沒有前一次紀錄時為 null）在 `test_sop_skill_service.py`、`test_sop_grading_service.py`。`sop_grading_cli` 把同版本的前端紀錄（`<版本資料夾>_job*`）算成已評過，在 `test_sop_grading_cli.py`；`run_record.job_records` 在 `tests/sop_grading/test_run_record.py`。前端取出檔名中 `VID_xxxx`（前後不能接英數字或重拍編號）的邏輯在 `frontend/sop-grading.test.js`。

對照實驗（`sop_agentic_service.py`、`sop_agentic_cli.py`、`compare_arms.py`）在 `backend/tests/test_sop_agentic.py`，模型以假回覆取代，不需資料庫與 Google 憑證：e2e 模式由模型的切分經 `rules.py` 產生 30 步並通過 `validate_grading --final`、模型判沒做的步驟維持程式判定、缺標準時把錯誤送回模型重送（user／model／user）、重送用完時失敗、`media_processing` 與整支影片（沒有 offset）；task 模式（需 ffmpeg）每個任務一次請求、影片帶任務區間的 offset、static 的 fps 與 media_resolution、任務說明沒有工具預算、送 overview 等工具動作會被退回；空回覆以原請求重送（不接空的模型回合）、連續空回覆超過上限時失敗；CLI 的版本資料夾不會和評分 agent 撞名；`compare_arms` 的 McNemar 與重跑翻轉計算。代理式影片解讀本身（真實的跳轉、計費、時間換算）只能用實際呼叫驗證，見 `EXPERIMENTS.md`。

```bash
pytest backend/tests/test_sop_agentic.py -q
```

```bash
pytest backend/tests/test_sop_run_bundle.py -q
```

真實影片的評分要呼叫 Vertex AI（會計費），用 `python -m app.sop_grading_cli` 在 worker 容器內逐支執行，再以 `eval_grading.py` 比對人工標註，見 README「SOP 評分」。

sop-video-skill 執行迴圈（`backend/tests/test_sop_skill_service.py`）用 ffmpeg 產生 60 秒合成影片，以真實的 skill 腳本執行 crop／set_crop／overview／zoom／final，模型以腳本化假回覆取代；需要 ffmpeg 與 `opencv-python-headless`、`matplotlib`、`jsonschema`（已列在 `backend/requirements.txt`），不需資料庫與 Google 憑證：

```bash
pytest backend/tests/test_sop_skill_service.py backend/tests/test_sop_skill_gemini.py backend/tests/test_sop_skill_claude.py -q
```

Token 用量記錄（`backend/tests/test_usage.py`）包含不需資料庫的單元測試（token 欄位換算、成本估計、單價設定檢查），以及需要 PostgreSQL 的整合測試（pipeline 每次回應寫一列、API 與 CSV）：

```bash
pytest backend/tests/test_usage.py -q
```

前端純函式測試使用 Node.js（CI 也會執行；CI 另外安裝 ffmpeg，所以需要 ffmpeg 的影片腳本測試在 CI 上不會被略過）：

```bash
node frontend/csv-export.test.js
node frontend/recovery.test.js
node frontend/sop-result.test.js
node frontend/sop-grading.test.js
```

## 驗證範圍

- 23 支影片只啟動 10 支；四個 Agent 都完成才補入下一支。
- 兩個 queue manager，以及兩個獨立 Python worker 程序共用 PostgreSQL，合计 20 支影片正常為 100 次邏輯呼叫，實測峰值 5。
- 原子建立工作／入列；切段結果與四個後續任務同時提交，途中例外則全部 rollback。
- 重複 completion 不重複加進度；結果依影片順序與 Agent A–D 排列，branch 通知進度從 0 到 N×6 單調增加。
- 408、429、500、502、503、504 使用真實 SDK retry 搭配 mock HTTP transport，驗證等待指數、最多 5 attempts 及每次 HTTP 日誌；400、401、403、404 不重試。
- SDK 重試用盡後的暫時性錯誤（429／499／逾時）由 queue 以指數退避重新派送；同一任務執行到 `GEMINI_QUEUE_MAX_ATTEMPTS` 次仍失敗時，工作改為失敗並記錄重試次數。
- 無效 JSON 只額外嘗試一次，GCS 缺檔／模型最終失敗使父工作失敗，已失敗父工作不再送模型请求。
- 將測試 worker 在切段／評分途中 SIGKILL，再啟動新程序；驗證 heartbeat 到期後恢復且結果／進度不重複。為縮短測試，probe worker 的 heartbeat timeout 是 1 秒，正式 worker 預設 30 秒。
- 舊版未完成工作一次性 backfill、空影片 422、23 支影片的 REST API 相容性與上傳回歸。

- SOP skill 模式：只派送 skill 任務（不呼叫 Time_cuting、Agent、Gazelle），活動窗口補位、結果依影片順序彙整；失敗後續跑只重跑未完成影片；Gemini 429／499 與 Claude 429／529 重新派送；Gemini 呼叫的請求格式（system instruction、圖片、JSON 回覆）與 finish reason 對應（MAX_TOKENS → 截斷重送、SAFETY 等 → 拒答）；被退回的動作不扣預算、舊圖片不重送、`final` 不合格會連同被退回的原文回傳模型修正；`set_crop` 後每回合的固定前綴逐位元組相同，送 Gemini 時拆成 user(固定內容)＋固定的 model 回覆＋user(本回合)（隱式快取）；結果檔下載端點只讀取本部署 bucket 的 `sop_runs/`。
- SOP 切分＋評分模式（`test_pipeline.py`、`test_api.py`、`test_sop_grading_service.py`）：每支影片先切分再評分，總步數為影片數 × 2，結果依影片順序含 `sop_skill` 與 `sop_grading`；評分失敗後續跑只重跑評分、不重新切分，且只重評失敗的那幾組（`grading_partial`）；失敗任務全是 429 時由 queue 自動重新派送；全部評好但驗證不過時改為整支重評，已評好的標準驗證不過時不保留；續跑時新的 artifacts.zip 併入前一次的檔案（影格編號接續、不覆蓋）；上傳 GCS 失敗時仍回傳評分結果；heartbeat 例外在最後一組之前會停止並保留已評的標準、在最後一組之後忽略；另一支影片讓工作失敗後才評完的影片，續跑時不再重評；區間切段不因浮點誤差超過 180 秒；WebSocket 傳送逾時的連線會被關閉（讓瀏覽器重連），關閉卡住也不會擋住廣播；`run_sop_grading` 下載原檔、上傳 `grading.json` 與不含影片的對話紀錄 ZIP、每完成一組任務回報進度，有失敗任務時先上傳再讓工作失敗；進度回報丟出例外時停止其他任務；評分下載端點只讀取本部署 bucket 的 `sop_runs/`，不提供 transcript。前端 `sop-grading.test.js` 檢查步驟排序、需人工複核、得分分母（不計不適用與無法判定）與 CSV 公式防護。
- Token 用量：每次模型回應（含 JSON 重送）都會寫入 `model_usage`，並帶有正確的階段與影片；工作被刪除時跳過寫入、不影響評分；`/usage` 與 `/usage.csv` 的合計與成本估計。

`tests/queue_probe.py` 只供測試子程序使用，啟動前檢查資料庫名稱，不會呼叫 Google。

## 壓力測試（locust）

`locustfile.py` 對 `POST /api/v1/evaluations/` 送出建立評分工作的請求。每個請求都是真的工作，worker 會呼叫 Gemini（會計費），所以只對測試部署執行。`LOCUST_VIDEO_URI` 必須是該部署上傳 API 產生的 `gs://<GCS_BUCKET_NAME>/videos/<sha256>_src.mp4`：

```bash
pip install locust
LOCUST_VIDEO_URI=gs://bucket/videos/<sha256>_src.mp4 locust -H http://localhost:8000
```

## 首次真實模型驗收

自動測試通過後，可由使用者提交 10 支代表性影片，查看 `docker compose logs -f worker` 的 429 比率與耗時，再決定是否調整 5 路設定。真實模型容量、延遲及計費不由 mock 測試保證。增加 worker 不會增加全域上限，改動設定需同時重啟所有 worker。
