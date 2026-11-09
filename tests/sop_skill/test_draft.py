"""Tests for draft.py (SKILL.md 2.3: each action sends only the changed rows)."""

import draft

FULL = "\n".join([
    "step_id | 估計區間 | 依據 | 確定度 | 下一步",
    "1 | 0.0-17.0 | global #0-#1 | 高 | 已確認",
    "5 | null | 待找 | 低 | 要 overview",
    "15-17 | 120.2-152.0 | global #15 | 中 | 要 zoom",
])


def test_changed_row_replaces_the_row_with_the_same_step():
    merged = draft.merge(FULL, "5 | 96.5-100.5 | ov@84-106 #5 | 高 | 已確認")
    assert "5 | 96.5-100.5 | ov@84-106 #5 | 高 | 已確認" in merged
    assert "5 | null" not in merged
    assert "1 | 0.0-17.0" in merged and "15-17 | 120.2-152.0" in merged


def test_new_rows_go_in_step_order_after_the_header():
    merged = draft.merge(FULL, "3 | 18.0-41.4 | global #2 | 中 | 無\n30 | 300-310 | global #37 | 中 | 無")
    keys = [line.split("|")[0].strip() for line in merged.splitlines()]
    assert keys == ["step_id", "1", "3", "5", "15-17", "30"]


def test_no_change_keeps_the_draft():
    for update in ("無變動", "（無變動）", "無變動。", "無變動（只看了 overview）", "No change.", "  ", ""):
        assert draft.merge(FULL, update) == FULL


def test_first_draft_is_taken_as_is():
    assert draft.merge("", FULL) == FULL


def test_note_lines_are_added_after_the_earlier_notes():
    first = draft.merge(FULL, "藥單在桌面右下角")
    assert first.endswith("藥單在桌面右下角")
    second = draft.merge(first, "5 | 96.5-100.5 | ov #5 | 高 | 無")
    assert second.endswith("藥單在桌面右下角")  # an update without notes keeps them
    third = draft.merge(second, "左手拿 vial\n藥單在桌面右下角")
    assert third.endswith("藥單在桌面右下角\n左手拿 vial")  # one new note does not erase the others


def test_markdown_table_rows_and_separators():
    merged = draft.merge(FULL, "| 5 | 96 | ov #5 | 高 | 無 |\n|---|---|---|---|---|\n| 7 | 12 | zoom@12 | 中 | 無 |")
    assert "5 | 96 | ov #5 | 高 | 無" in merged and "5 | null" not in merged
    assert "7 | 12 | zoom@12 | 中 | 無" in merged and "---" not in merged


def test_same_step_written_differently_is_one_row():
    merged = draft.merge(FULL, "05 | 96 | ov #5 | 高 | 無\n5. | 97 | ov #6 | 高 | 無")
    assert [line for line in merged.splitlines() if line.startswith("5 ")] == ["5 | 97 | ov #6 | 高 | 無"]


def test_a_row_can_be_deleted_when_a_merged_row_is_split():
    merged = draft.merge(FULL, "15-17 | 刪除\n15 | 120-130 | zoom@125 | 高 | 無\n16 | 130-140 | zoom@135 | 中 | 無")
    keys = [line.split("|")[0].strip() for line in merged.splitlines()]
    assert keys == ["step_id", "1", "5", "15", "16"]


def test_merging_the_full_draft_again_changes_nothing():
    # A model that still sends the whole table gets the same draft back.
    assert draft.merge(FULL, FULL) == FULL


def test_missing_rows_lists_what_a_full_rewrite_left_out():
    assert draft.missing_rows(FULL, FULL) == []
    assert draft.missing_rows(FULL, "5 | 96 | ov #5 | 高 | 無") == ["1", "15-17"]
    assert draft.missing_rows(FULL, "無變動") == ["1", "5", "15-17"]
    rewrite = "1 | 0-17 | g | 高 | 無\n5 | 96 | ov | 高 | 無\n15-17 | 刪除\n15 | 120 | z | 中 | 無"
    assert draft.missing_rows(FULL, rewrite) == []  # no header needed; deleting a row counts
