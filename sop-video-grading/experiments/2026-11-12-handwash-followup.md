# 洗手與讀單調整組補評

issue #163，補充 #160 與 PR #161。採用原調整組 25 支與使用者追加的 5 支。
沿用 main `2f2b4dd` 的 0.44／rubric 1.28 紀錄。其餘使用 #141 `3860e32` 的 0.44-disposal.1／rubric 1.28-disposal.1 一次評分步驟 1–4、29、30。兩版的步驟 1–4、29 rubric 條目相同；整份提示版本依原紀錄保留。模型為 Gemini 4 Argon Medium。

比對使用既有標記表的判定與時間，依 `eval_grading.py` 計算。
有標記時間時，模型時間需重疊或起點相差不超過 3 秒。標記表與下列差異仍待人工核對。

共有 30 支依切分 1.5.9-disposal.1（#141）重切步驟 1–4、30，步驟 5–29 逐筆沿用 GitHub 切分。各支來源列於下方。

- 判定一致 78/150（52.0%）。
- 納入時間證據後一致 75/150（50.0%）。有標記時間可比 64 項。
- 共有 150 項評分，標記表可比 150 項。追加影片為 VID_0010、VID_0011、VID_0018、VID_0020、VID_0030。
- 全批切分 input tokens 8,083,856，全批評分 17,522,515（含步驟 30）。

| 影片 | 判定一致 | 納入時間 | 紀錄版本 |
| --- | --- | --- | --- |
| VID_0001 | 3/5 | 3/5 | `rubric1.28_skill0.44_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0008 | 3/5 | 3/5 | `rubric1.28_skill0.44_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0014 | 4/5 | 3/5 | `rubric1.28_skill0.44_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0072 | 2/5 | 2/5 | `rubric1.28_skill0.44_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0073 | 2/5 | 2/5 | `rubric1.28_skill0.44_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0074 | 1/5 | 1/5 | `rubric1.28_skill0.44_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0076 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0081 | 1/5 | 1/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0083 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0087 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0089 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0091 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0095 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0096 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0097 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0098 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0099 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0100 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0103 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0106 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0111 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0118 | 1/5 | 1/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0119 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0123 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0134 | 2/5 | 2/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0010 | 4/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0011 | 4/5 | 4/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0018 | 5/5 | 5/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0020 | 2/5 | 1/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |
| VID_0030 | 3/5 | 3/5 | `rubric1.28-disposal.1_skill0.44-disposal.1_gemini-4-argon_seg-skill1.5.9-disposal.1_gemini-4-argon_partial-1-4-30` |

## 沿用切分的來源

- VID_0001：`rubric1.25-needle13b.3_skill0.41-needle13b.3_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0008：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0014：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0072：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0073：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0074：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0076：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0081：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0083：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0087：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0089：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0091：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0095：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0096：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0097：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0098：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0099：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0100：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0103：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0106：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0111：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0118：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0119：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0123：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0134：`rubric1.25-flick.1_skill0.41-flick.1_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0010：`rubric1.25_skill0.41_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0011：`rubric1.23_skill0.39_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0018：`rubric1.23_skill0.39_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0020：`rubric1.23_skill0.39_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。
- VID_0030：`rubric1.23_skill0.39_gemini-4-argon_seg-skill1.5.6_gemini-4-argon` 的步驟 5–29。

## 待人工核對

| 影片 | 步驟 | 標記表 | 模型 | 標記表時間 | 模型時間 | 差異 |
| --- | --- | --- | --- | --- | --- | --- |
| VID_0001 | 4 | 正確 | 部分正確 | 02:25–03:01 | 02:23–03:48／02:26.5–02:41／02:45.5–02:58.5／03:25.5–03:26 | 判定 |
| VID_0001 | 29 | 無法判定 | 錯誤 | — | — | 判定 |
| VID_0008 | 1 | 正確 | 錯誤 | 01:10.8–01:26 | 01:09.5–01:27.5／01:10.5–01:27 | 判定 |
| VID_0008 | 4 | 正確 | 部分正確 | 01:44–02:04 | 00:50–00:51／01:01.5–01:06.5／01:42.5–02:04／02:31.5–02:42.5／02:34–02:41／02:44.5–02:47.5／03:42–03:51.5 | 判定 |
| VID_0014 | 3 | 正確 | 正確 | 01:30–01:37／03:28–03:30 | 01:44.5–02:04 | 時間 |
| VID_0014 | 4 | 部分正確 | 正確 | — | 01:36.5–01:37.5／03:29.5–03:39／03:30–03:39／03:41.5–03:56.5／03:46.5–03:55.5／03:46.5–03:56／04:36.5–04:40 | 判定 |
| VID_0072 | 2 | 無法判定 | 錯誤 | — | 00:06–00:17.5／00:18–00:19／00:18.5 | 判定 |
| VID_0072 | 3 | 無法判定 | 正確 | — | 00:22.3–00:26.6／00:39.4–00:47.9 | 判定 |
| VID_0072 | 29 | 無法判定 | 錯誤 | — | 04:44–04:50 | 判定 |
| VID_0073 | 2 | 無法判定 | 錯誤 | — | 00:00–00:22／00:00–00:22.5／00:22.5／00:23.5 | 判定 |
| VID_0073 | 3 | 無法判定 | 正確 | — | 00:23.6–00:25 | 判定 |
| VID_0073 | 29 | 無法判定 | 錯誤 | — | — | 判定 |
| VID_0074 | 1 | 正確 | 錯誤 | 00:00–00:14 | 00:01–00:15 | 判定 |
| VID_0074 | 2 | 無法判定 | 錯誤 | — | 00:02.9–00:14.8／00:12.8–00:15.8／00:14.8／00:15.8 | 判定 |
| VID_0074 | 3 | 無法判定 | 正確 | — | 00:17.5–00:25 | 判定 |
| VID_0074 | 29 | 無法判定 | 錯誤 | 06:00–06:05 | 05:59–06:04.5 | 判定 |
| VID_0076 | 2 | 無法判定 | 錯誤 | — | 00:12–00:17.5／00:12–00:24／00:18–00:23.5／00:24 | 判定 |
| VID_0076 | 3 | 無法判定 | 正確 | — | 00:25–00:28 | 判定 |
| VID_0081 | 1 | 正確 | 錯誤 | 00:04–00:30 | 00:05.5–00:32.5／00:06.5–00:32 | 判定 |
| VID_0081 | 2 | 無法判定 | 錯誤 | — | 00:00／00:00–00:03.5／00:03／00:03–00:03.5 | 判定 |
| VID_0081 | 3 | 無法判定 | 正確 | — | 00:33.9–00:38.9 | 判定 |
| VID_0081 | 29 | 無法判定 | 錯誤 | 10:41–10:46 | 08:42–10:48／08:45–09:06／10:41.2–10:46.7 | 判定 |
| VID_0083 | 2 | 無法判定 | 錯誤 | — | 00:00–00:20.5／00:00–00:22／00:01.5–00:20.5／00:22 | 判定 |
| VID_0083 | 3 | 無法判定 | 正確 | — | 00:22–00:30 | 判定 |
| VID_0083 | 29 | 無法判定 | 錯誤 | — | 05:05–05:11／05:06–05:11／05:08.5–05:11 | 判定 |
| VID_0087 | 2 | 無法判定 | 錯誤 | — | 00:06.9–00:14.4／00:06.9–00:19.3／00:14.9–00:18.8／00:18.8–00:19.3／00:19.3 | 判定 |
| VID_0087 | 3 | 無法判定 | 正確 | — | 00:19.7–00:23.1 | 判定 |
| VID_0089 | 2 | 無法判定 | 錯誤 | — | 00:12.4–00:22.4／00:12.4–00:25.4／00:17.9–00:19.4／00:22.4–00:25.4／00:24.9–00:25.4／00:25.4 | 判定 |
| VID_0089 | 3 | 無法判定 | 正確 | — | 00:26.1–00:36.4 | 判定 |
| VID_0089 | 29 | 無法判定 | 錯誤 | — | — | 判定 |
| VID_0091 | 2 | 無法判定 | 錯誤 | — | 00:08–00:21.5／00:13.5–00:20／00:24.5 | 判定 |
| VID_0091 | 3 | 無法判定 | 正確 | — | 00:24.9／00:32.2–00:32.5 | 判定 |
| VID_0095 | 2 | 無法判定 | 錯誤 | — | 00:04–00:15.5／00:14–00:17／00:16–00:17／00:16.5 | 判定 |
| VID_0095 | 3 | 無法判定 | 正確 | — | 00:17.3–00:19.6／00:28.9 | 判定 |
| VID_0096 | 1 | 正確 | 錯誤 | 00:00–00:26 | 00:01.5–00:26／00:03–00:25.5 | 判定 |
| VID_0096 | 2 | 無法判定 | 錯誤 | — | 00:13.5–00:26／00:25.5／00:26 | 判定 |
| VID_0096 | 3 | 無法判定 | 正確 | — | 00:27.9–00:32.2 | 判定 |
| VID_0097 | 2 | 無法判定 | 錯誤 | — | 00:44–00:44.5／00:44–00:45／00:45.5 | 判定 |
| VID_0097 | 3 | 無法判定 | 錯誤 | — | 00:45–00:49 | 判定 |
| VID_0097 | 29 | 無法判定 | 錯誤 | — | 04:49–04:55／04:49.5–06:25.9／04:57–05:34／04:57–06:25／05:34–05:40／05:45–06:18 | 判定 |
| VID_0098 | 1 | 正確 | 錯誤 | 00:01–00:18 | 00:01.5–00:19／00:02.5–00:18.5 | 判定 |
| VID_0098 | 2 | 無法判定 | 錯誤 | — | 00:00–00:02.5／00:00–00:10／00:06–00:18.5／00:06–00:19／00:19／00:20 | 判定 |
| VID_0098 | 3 | 無法判定 | 正確 | — | 00:20.5–00:32.6 | 判定 |
| VID_0099 | 2 | 無法判定 | 錯誤 | — | 00:00／00:59.5 | 判定 |
| VID_0099 | 3 | 無法判定 | 錯誤 | — | 00:22.5–01:07 | 判定 |
| VID_0100 | 2 | 無法判定 | 錯誤 | — | 00:02.5–00:13／00:02.5–00:20／00:13.5–00:17.5／00:18 | 判定 |
| VID_0100 | 3 | 無法判定 | 正確 | — | 00:18–00:21 | 判定 |
| VID_0103 | 2 | 無法判定 | 錯誤 | — | 00:03–00:14.3／00:14.3–00:15.3／00:14.8／00:15.3 | 判定 |
| VID_0103 | 3 | 無法判定 | 正確 | — | 00:15.8–00:19.6 | 判定 |
| VID_0103 | 29 | 無法判定 | 錯誤 | — | 05:57–05:59／05:57–07:45.4／06:01–06:28／06:28.5–07:05.5／07:05.5–07:07／07:45.4 | 判定 |
| VID_0106 | 1 | 正確 | 錯誤 | 00:03–00:17 | 00:02–00:17.5／00:03–00:15.5 | 判定 |
| VID_0106 | 2 | 無法判定 | 錯誤 | — | 00:05.4–00:17.4／00:15.4–00:17.9／00:17.9 | 判定 |
| VID_0111 | 1 | 正確 | 錯誤 | 00:01–00:12 | 00:00.5–00:12.5／00:01.5–00:12 | 判定 |
| VID_0111 | 2 | 無法判定 | 錯誤 | — | 00:00.5–00:12／00:00.5–00:13／00:12.5–00:13／00:13 | 判定 |
| VID_0111 | 3 | 無法判定 | 正確 | — | 00:16.2／00:44.3 | 判定 |
| VID_0118 | 1 | 正確 | 錯誤 | 00:02–00:18 | 00:02–00:18.5／00:03–00:18 | 判定 |
| VID_0118 | 2 | 無法判定 | 錯誤 | — | 00:18.4–00:25.4／00:25.4 | 判定 |
| VID_0118 | 3 | 無法判定 | 正確 | — | 00:51.3–00:54.8 | 判定 |
| VID_0118 | 29 | 無法判定 | 錯誤 | 05:57–06:02 | 00:03／00:15–00:19／05:42–07:23／05:45／05:45–06:01.5／05:45–07:23.5／05:57–06:01／06:09.5–06:46.5／06:51／06:51–07:23.5／06:52–07:20／07:23 | 判定 |
| VID_0119 | 2 | 無法判定 | 錯誤 | — | 00:07.5–00:13／00:13.5–00:21／00:21 | 判定 |
| VID_0119 | 29 | 無法判定 | 錯誤 | — | — | 判定 |
| VID_0123 | 1 | 正確 | 錯誤 | 00:09–00:26 | 00:07.5–00:26／00:08.5–00:25.5 | 判定 |
| VID_0123 | 2 | 無法判定 | 錯誤 | — | 00:13.7–00:27.1／00:16.2–00:26.6／00:27.1 | 判定 |
| VID_0134 | 2 | 無法判定 | 錯誤 | — | 00:17.5–00:29／00:17.5–00:32.5／00:29.5 | 判定 |
| VID_0134 | 3 | 無法判定 | 正確 | — | 00:29.5–00:34.5 | 判定 |
| VID_0134 | 29 | 無法判定 | 錯誤 | — | 04:52.5–05:56.5／04:54.5／04:54.5–05:52.5 | 判定 |
| VID_0010 | 3 | 正確 | 正確 | 01:05–01:09 | 01:09.5–02:02 | 時間 |
| VID_0010 | 4 | 正確 | 部分正確 | 01:11–02:06 | 01:11–02:04.5／02:46.9–02:49／02:52–03:11／03:32.5–03:41.5 | 判定 |
| VID_0011 | 29 | 部分正確 | 錯誤 | 09:08–09:15 | 08:54.5–08:56.5／09:07–09:15.5／09:07.5–09:15.5 | 判定 |
| VID_0020 | 1 | 正確 | 錯誤 | 00:29–00:50.8 | 00:26.5–00:49.5／00:28.5–00:49 | 判定 |
| VID_0020 | 2 | 錯誤 | 正確 | — | 01:18.5–01:29／01:21.5–01:28／01:32.5 | 判定 |
| VID_0020 | 3 | 正確 | 正確 | 01:33–01:37／02:11–02:18 | 01:39.8–02:07.8 | 時間 |
| VID_0020 | 4 | 正確 | 無法判定 | 02:24–03:24 | 01:38／01:38–04:18.5／02:36–02:38.5／02:42.5–03:12.5／02:55–03:13.8／05:02–05:10.5 | 判定 |
| VID_0030 | 4 | 正確 | 部分正確 | 00:40–01:00／02:28–02:44 | 00:25–00:33.5／02:20.2–02:37.7／02:44–02:45.5／02:53.7–03:02.2／03:04–03:10.5／03:20.5–03:24 | 判定 |
| VID_0030 | 29 | 正確 | 無法判定 | 08:48–08:58 | 08:44–08:53.5／08:47–08:53.5 | 判定 |

上述表格依差異類型列出待核對項目。具體判定依據可查各影片的評分紀錄。
