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

sop-video-skill 執行迴圈（`backend/tests/test_sop_skill_service.py`）用 ffmpeg 產生 60 秒合成影片，以真實的 skill 腳本執行 crop／set_crop／overview／zoom／final，模型以腳本化假回覆取代；需要 ffmpeg 與 `opencv-python-headless`、`matplotlib`、`jsonschema`（已列在 `backend/requirements.txt`），不需資料庫與 Google 憑證：

```bash
pytest backend/tests/test_sop_skill_service.py backend/tests/test_sop_skill_gemini.py backend/tests/test_sop_skill_claude.py -q
```

前端純函式測試使用 Node.js：

```bash
node frontend/csv-export.test.js
node frontend/recovery.test.js
node frontend/sop-result.test.js
```

## 驗證範圍

- 23 支影片只啟動 10 支；四個 Agent 都完成才補入下一支。
- 兩個 queue manager，以及兩個獨立 Python worker 程序共用 PostgreSQL，合计 20 支影片正常為 100 次邏輯呼叫，實測峰值 5。
- 原子建立工作／入列；切段結果與四個後續任務同時提交，途中例外則全部 rollback。
- 重複 completion 不重複加進度；結果依影片順序與 Agent A–D 排列，branch 通知進度從 0 到 N×6 單調增加。
- 408、429、500、502、503、504 使用真實 SDK retry 搭配 mock HTTP transport，驗證等待指數、最多 5 attempts 及每次 HTTP 日誌；400、401、403、404 不重試。
- 無效 JSON 只額外嘗試一次，GCS 缺檔／模型最終失敗使父工作失敗，已失敗父工作不再送模型请求。
- 將測試 worker 在切段／評分途中 SIGKILL，再啟動新程序；驗證 heartbeat 到期後恢復且結果／進度不重複。為縮短測試，probe worker 的 heartbeat timeout 是 1 秒，正式 worker 預設 30 秒。
- 舊版未完成工作一次性 backfill、空影片 422、23 支影片的 REST API 相容性與上傳回歸。

- SOP skill 模式：只派送 skill 任務（不呼叫 Time_cuting、Agent、Gazelle），活動窗口補位、結果依影片順序彙整；失敗後續跑只重跑未完成影片；Gemini 429／499 與 Claude 429／529 重新派送；Gemini 呼叫的請求格式（system instruction、圖片、JSON 回覆）與 finish reason 對應（MAX_TOKENS → 截斷重送、SAFETY 等 → 拒答）；被退回的動作不扣預算、舊圖片不重送、`final` 不合格會回傳模型修正；結果檔下載端點只讀取本部署 bucket 的 `sop_runs/`。

`tests/queue_probe.py` 只供測試子程序使用，啟動前檢查資料庫名稱，不會呼叫 Google。

## 首次真實模型驗收

自動測試通過後，可由使用者提交 10 支代表性影片，查看 `docker compose logs -f worker` 的 429 比率與耗時，再決定是否調整 5 路設定。真實模型容量、延遲及計費不由 mock 測試保證。增加 worker 不會增加全域上限，改動設定需同時重啟所有 worker。
