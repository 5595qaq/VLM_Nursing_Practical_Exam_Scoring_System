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

SOP skill（`sop-video-skill/`）的結果驗證（`schema.json`、`scripts/`）不需要資料庫：

```bash
pip install -r scripts/requirements.txt
pytest scripts/tests -q
python scripts/validate_result.py result.json           # 驗證一份 final 結果
python scripts/schema_tools.py sop-video-skill/schema.json > out.json    # 產生 Structured Outputs 用的 schema
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

`tests/queue_probe.py` 只供測試子程序使用，啟動前檢查資料庫名稱，不會呼叫 Google。

## 首次真實模型驗收

自動測試通過後，可由使用者提交 10 支代表性影片，查看 `docker compose logs -f worker` 的 429 比率與耗時，再決定是否調整 5 路設定。真實模型容量、延遲及計費不由 mock 測試保證。增加 worker 不會增加全域上限，改動設定需同時重啟所有 worker。
