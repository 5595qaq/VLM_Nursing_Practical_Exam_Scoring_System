const assert = require('node:assert/strict');
const SopResult = require('./sop-result.js');

assert.equal(SopResult.formatSeconds(0), '00:00.0');
assert.equal(SopResult.formatSeconds(65.25), '01:05.3');
assert.equal(SopResult.formatSeconds(null), '');
assert.equal(SopResult.formatSeconds(59.96), '01:00.0');
assert.equal(SopResult.formatSeconds(119.97), '02:00.0');
assert.equal(SopResult.formatSpan(6.5, 41), '00:06.5–00:41.0');
assert.equal(SopResult.formatSpan(null, null), '');
assert.equal(Object.keys(SopResult.STEP_NAMES).length, 30);

const result = {
    steps: [
        { step_id: 22, start: 300, end: 305, confidence: 0.7, status: 'detected', evidence: 'repeat', merged_with: [] },
        { step_id: 2, start: null, end: null, confidence: 0, status: 'not_visually_determinable',
          evidence: '未見注視紀錄單', logical_window: [41, 70] },
        { step_id: 22, start: 200, end: 204, confidence: 0.8, status: 'detected', evidence: '=zoom@201' },
        { step_id: 17, start: 150, end: 170, confidence: 0.5, status: 'detected', evidence: '彈針',
          merged_with: [15, 16], handoff: 'flick_detector' },
        { step_id: 11, start: null, end: null, confidence: 1, status: 'not_performed', evidence: '已移除' }
    ],
    observations: [
        { type: 'repeated', start: 300, end: 305, steps: [22], description: '重抽', evidence: 'ov', confidence: 0.6 }
    ],
    unassigned_spans: []
};

const rows = SopResult.buildStepRows(result);
assert.deepEqual(rows.map(row => [row.stepId, row.start]), [[2, ''], [11, ''], [17, '02:30.0'], [22, '03:20.0'], [22, '05:00.0']]);
assert.equal(rows[0].statusLabel, '無法由畫面判定');
assert.equal(rows[0].notes, '邏輯區間 00:41.0–01:10.0');
assert.equal(rows[2].notes, '與步驟 15、16 合併；交給 flick_detector');
assert.equal(rows[1].stepName, '（已自評分表移除）將瓶口上酒精棉片移除');

assert.deepEqual(SopResult.statusCounts(result), { detected: 2, not_visually_determinable: 1, not_performed: 1 });

const observations = SopResult.buildObservationRows(result, [
    { type: 'out_of_order', start: 10, end: 20, steps: [5, 6], description: 'auto', evidence: 'seq', confidence: 0.5 }
]);
assert.deepEqual(observations.map(row => [row.type, row.steps, row.source]),
    [['重複', '22', '模型'], ['順序偏離', '5、6', 'sequence_check.py']]);

const csv = SopResult.buildStepsCsv({
    id: 'job-1',
    result: { pipeline: 'sop_skill', sop_skill: [{ video_uri: 'gs://b/videos/abc_src.mp4', result }] }
}, { 'gs://b/videos/abc_src.mp4': '考生一.mp4' });
const lines = csv.replace(/^﻿/, '').split('\r\n');
assert.equal(lines.length, 1 + 5);
assert.match(lines[0], /^"工作編號","影片名稱","影片路徑","步驟編號"/);
assert.match(lines[1], /^"job-1","考生一.mp4","gs:\/\/b\/videos\/abc_src.mp4","2",/);
assert.ok(lines.some(line => line.includes(`"'=zoom@201"`)), 'formula-like evidence must be neutralized');

assert.equal(SopResult.fileUrl('http://localhost:8000', 'a/b', 0, 'artifacts'),
    'http://localhost:8000/api/v1/evaluations/a%2Fb/sop-skill/0/artifacts');

console.log('sop-result tests passed');
