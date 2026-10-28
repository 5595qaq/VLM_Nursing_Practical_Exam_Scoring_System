# changelog.d

還沒進正式版本的修改紀錄，一個分支一個檔案，避免多人同時改 `CHANGELOG.md` 而衝突（規則見 `AGENTS.md`「多人協作」）。

- 檔名用分支的主題，例如 `package-spans.md`；內容是要放進 `CHANGELOG.md` 的條列，寫法和 `CHANGELOG.md` 相同。
- 疊代期間只改自己的檔案，不改 `CHANGELOG.md`。
- 合併前把版本改成正式版本時，把自己的片段移到 `CHANGELOG.md` 的新版本標題下，並刪除片段檔。
- 不影響切分結果、不改版本的修改也寫片段；下一個正式版本合併時，一併移進 `CHANGELOG.md`。
