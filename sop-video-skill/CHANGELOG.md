# CHANGELOG

## 1.4.6

- `references/sop_steps.md` 步驟 10：開始改為「空針離開包裝或開始固定針頭（取較早者）」，結束改為「固定完成、空針拿在手上或放到車上」，不在空針離開包裝時就結束。刪掉「緊接在 9 之後」；固定針頭和取出中間隔了其他步驟時輸出兩筆步驟 10，並寫 `interrupted`。配合評分 rubric 1.9 的 10b（固定針頭不限取出那一刻）。動作格式與 schema 不變。

## 1.4.5

規則、動作格式與 schema 都不變。

- `motion.py`：明確指定縮圖高度給 ffmpeg（`scale=w:h`）。原本用 `scale=w:-2`，ffmpeg 遇到 .5 會進位，Python 的 `round()` 卻取偶數，兩邊可能差 2 px，之後每一格都切錯位置，能量曲線與 candidates 會變成雜訊而且不會報錯。ffmpeg 失敗或讀不到影格時改為直接報錯。
- `validate_action.py`：有傳入影片長度時，`final` 的 `meta.duration` 必須和影片相差 0.5 秒以內，避免模型寫大的長度讓超出影片的區間通過驗證。
- 後端在驗證 `final` 前，以實際影片長度與 `set_crop` 確定的框覆寫 `meta.duration` 與 `meta.crop.box`／`hands_box`。

## 1.4.4

- `sequence_check.py`：允許步驟 15 與 17 重疊（17 的區間是 15/16 外框整段，見 SKILL.md 4.7，通常比 15 早開始）。原本幾乎每支影片都會多一筆假的 `out_of_order [15,17]`；25/27 早已允許。
- `SKILL.md` frontmatter 的 version 由 1.3 更新為 1.4.4，與本檔一致（1.4～1.4.3 漏改）。規則、動作格式與 schema 都不變。

## 1.4.3

動作被 `validate_action.py` 退回時，外部程式除了 `errors`，也會附上被退回的動作原文；SKILL.md 第 0 節要求模型只修改錯誤指出的地方再重送。原本模型只拿到錯誤訊息和舊的 draft，重送的 `final` 等於重新產生一份，連沒被指出的步驟與 observations 也會改變。動作格式與 schema 不變。

## 1.4.2

後端預設改由 Vertex AI 上的 Gemini（`gemini-4-argon`）執行本 skill；設定 `SOP_SKILL_PROVIDER=claude` 可改回 Claude。skill 的規則、動作格式與 schema 都不變。

- `SKILL.md` 的 description 由「Claude 不吃影片」改為「模型不直接讀影片」，不再綁定特定模型。

## 1.4.1

修正 #51 審查找到的問題（腳本行為修正，schema 與 SKILL.md 規則不變）。

- **影片結尾抓不到畫面（`_video.grab`）：** 容器長度常比最後一格畫面長（音軌、每格的時長），zoom／sheets 的時間點落在最後一格之後時，ffmpeg 沒有輸出畫面，原本會直接當掉。現在會往前退（最多 2 秒）改抓最接近的前一格；完全抓不到才丟 `RuntimeError`。
- **`sequence_check.py --write`：** 寫回結果時不再帶 `"auto": true`（schema.json 沒有這個欄位，寫回後驗證不過）。來源仍可從 `evidence` 看出；`check()` 的回傳值保留 `auto`。
- **`eval.py`：** 標準答案只有開始或只有結束時間的列，視為「無法判定」並印出警告，不再當掉。
- **長度為 0 的時段（`validate_result.py`）：** `unassigned_spans` 與 observation 若有一方長度為 0，碰到邊界也算重疊；兩段都有長度時，只碰到邊界仍不算。
- **檔案編碼：** `probe.json`、`state.json` 與 proxy 的 sidecar JSON 一律以 UTF-8 讀寫，Windows 上路徑含中文時不會失敗。

## 1.4

`schema.json` 補齊限制，讓 jsonschema 能完整驗證最終結果（`scripts/validate_result.py`）。

### 結果 schema（schema.json）
- **所有物件加上 `additionalProperties: false`：** 多出或拼錯的欄位會被退回，不再默默通過。
- **補上缺的 `required`：**
  - 最外層加入 `unassigned_spans`、`zoom_log`（沒有就輸出空陣列）；
  - `meta.crop` 加入 `hands_box`；
  - `unassigned_spans[]` 要求 `start`、`end`、`note`；
  - `zoom_log[]` 要求 `times`、`region`、`for_steps`、`finding`。
- **數值範圍：**
  - `duration`、`rounds`、`budget.*` ≥ 0；
  - `merged_with`、`for_steps` 的步驟編號限 1–30；
  - `zoom_log.times` 1–4 個時間點；
  - `zoom_log.region` 固定 4 個 0–1 的數。
- **`logical_window`：** 由 `type: [array, null]` 改成 `anyOf`，null 時不再套用陣列的長度限制。

### 動作 schema（actions.schema.json）
- **`final.result` 改成 `{"$ref": "schema.json"}`：** 原本只寫 `{"type": "object"}`，任何物件都能通過，壞掉的最終結果要到動作驗證之後才會被發現；也因為不是封閉物件，無法用於 Structured Outputs。現在 `validate_action.py` 收到 `final` 時就用 schema.json 驗證 `result`，不合格會退回（不扣預算）。

### 外部程式（scripts/）
- `validate_result.py`、`schema_tools.py` 從 repo 根目錄移入 skill，打包後不再缺檔。
- `validate_action.py` 收到 `final` 時，schema 通過後接著跑 `validate_result.py` 的規則檢查（30 步齊全、步驟 11 等），一次驗完。
- `schema_tools.py`：轉換前把引用的檔案（`schema.json`）嵌入 `$defs`，供 Structured Outputs 使用。

### 流程與文件（SKILL.md）
- 第 7 節輸出規定同步：`unassigned_spans`、`zoom_log` 必填；只能使用 `schema.json` 定義的欄位。

## 1.3

依 v1.2 測試（20250522 影片）回顧修改。

### 流程與文件（SKILL.md / references/sop_steps.md）
- **motion 改在裁切確定後計算。** v1.2 表格把 motion 放在開場，但能量要在裁切框內算。開場只產生 proxy＋格線圖；新增 `set_crop` 動作，確定 context box / hands_box 後才產生全片總覽圖與動作能量。v1.2 本來就沒有定義「確定裁切」的動作。
- **zoom region 有數字上限：** 面積 ≤ context box 的 50%，寬、高各 ≤ 0.75。並規定 region 要涵蓋所有時間點的手部位置聯集，附常用高度表；手的高度不同時拆成兩次 zoom。
- **重抽規則（第 6 節）：** 發現重複區塊後，必須對前一區塊結束到重複開始的空檔做密集 overview，確認是從 21 還是 22 開始；看不到的步驟只寫進 `repeated` 的 description，不補 `detected`。sop_steps.md 的重抽說明與步驟 21 同步修改，短步驟清單加入 21。
- **left_spans：** 2.2 節加入 `left_spans` 的用法，並說明它只是提示，要用 context overview 確認。
- **draft 規定不一致：** 2.3 節原本寫「草稿在推理中、不輸出」，與第 0 節「每個動作要附 draft」矛盾，改成 draft 欄位，並要求依據寫成影格引用。
- **proxy 規格：** 第 0 節寫明 proxy 規格與檔案分工（proxy：grid／preview／sheets／motion；原檔：zoom）。

### 外部程式（scripts/）
- `prepare.py`（新增）：開場一次產生 `proxy.mp4`、`probe.json`、`grid.jpg`。
- `_video.make_proxy`：
  - 依 CPU 核心數切段平行轉檔後 concat；
  - ultrafast、fast_bilinear；
  - 可選 `--hwaccel`；
  - 用 sidecar `proxy.mp4.json` 記錄來源大小／mtime／參數，來源沒變就沿用（v1.2 只看檔案存不存在）。
  - 輸出規格不變：960 寬、10 fps、每 1 s 一個 keyframe。
  - 實測：90 s HEVC 1080p 片段在 2 核機器上 23 s → 20 s；瓶頸是 HEVC 解碼，核心數越多加速越明顯。
- `motion.py`：
  - 新增 `--box2 CONTEXT_BOX`，計算 context box 中 hands_box 以外的能量（`energy2`）。
  - 外／內能量比持續偏高 → `left_spans`，與它重疊的 `still_spans` 會移除。
  - 不直接用 context box 整體能量：身體一直在 hands_box 裡，整體能量只會跟著 hands_box 走，測試時 `left_spans` 是空的。
  - 在測試影片上，`left_spans` 涵蓋 05:50–06:48 的點滴架操作；04:56 彎腰重插針時有一次誤報。
- `crop.py zoom`：
  - 搭配 `--rel-to-crop` 時檢查 region 上限，超過就 exit 2、不產生圖片；
  - 每格右下角附定位縮圖（整個 context box＋紅框 region）。
- `validate_action.py`、`actions.schema.json`（新增）：在執行前驗證動作 JSON，涵蓋 schema、框的順序、hands_box 是否在 context box 內、zoom 上限、overview 區間長度、影片長度、是否已裁切、`set_crop` 只能一次、剩餘預算。退回的動作不執行、不扣預算。

### 外部程式迴圈（部署參考）
```
prepare.py VIDEO --out run/                 # 第 1 回合：送 grid.jpg
loop:
  action = model()
  r = validate_action.py action --state run/state.json --probe run/probe.json
  if not r.ok: 把 r.errors 回傳給模型，continue（不扣預算）
  crop_preview -> crop.py preview run/proxy.mp4 --box BOX --n 9
  set_crop     -> sheets.py run/proxy.mp4 --box HANDS --start 0 --end DUR --step 8
                  motion.py run/proxy.mp4 --box HANDS --box2 CONTEXT --plot ...
  overview     -> sheets.py run/proxy.mp4 --box (HANDS|CONTEXT) --start S --end E --n N
  zoom         -> crop.py zoom ORIGINAL --box REGION --rel-to-crop CONTEXT --times ...
  final        -> 依 schema.json 驗證，sequence_check.py result.json --write
  更新 state.json 的 used / box / hands_box；每回合附上剩餘預算
```
