const assert = require('node:assert/strict');
const SopGrading = require('./sop-grading.js');

const criterion = (id, verdict, extra = {}) => ({
    criterion_id: id, verdict, source: 'vlm', explanation: `${id} 說明`, evidence: [], confidence: 0.8,
    reason: null, window: null, ...extra
});

const result = {
    video_id: 'VID_0001',
    rubric_version: '1.5',
    steps: [
        { step_id: 17, verdict: '錯誤', explanation: '未聽到彈針聲', confidence: 0.7,
          criteria: [criterion('17a', '錯誤', { evidence: ['clip@150-170', 'burst@160-162 #3'], window: [148, 172] })] },
        { step_id: 1, verdict: '正確', explanation: '洗手 18 秒', confidence: 0.9,
          criteria: [criterion('1a', '正確', { span: [3, 21] })] },
        { step_id: 11, verdict: '不適用', explanation: '已自評分表移除', confidence: 1,
          criteria: [criterion('11a', '不適用', { source: 'fixed' })] },
        { step_id: 2, verdict: '無法判定', explanation: '藥單看不見', confidence: 0,
          criteria: [criterion('2a', '無法判定', { source: 'fixed', reason: '藥單看不見' })] },
        { step_id: 15, verdict: '部分正確', explanation: '一次正確一次錯誤', confidence: 0.6,
          criteria: [criterion('15a', '部分正確', { needs_review: true, review_reason: '兩次嘗試結果不同 01:10、01:30' })] },
        { step_id: 3, verdict: '正確', explanation: '有檢查藥瓶', confidence: 0.8, needs_review: true,
          criteria: [criterion('3a', '正確', { source: 'rule' })] }
    ]
};

const rows = SopGrading.buildStepRows(result);
assert.deepEqual(rows.map(row => row.stepId), [1, 2, 3, 11, 15, 17]);
assert.equal(rows[0].stepName, '洗手');
assert.equal(rows[0].verdictClass, 'correct');
assert.equal(rows.find(row => row.stepId === 17).verdictClass, 'wrong');
// needs_review on the step, or on any of its criteria
assert.deepEqual(rows.filter(row => row.needsReview).map(row => row.stepId), [3, 15]);
assert.deepEqual(SopGrading.buildStepRows(null), []);

const criteria = SopGrading.buildCriterionRows(result.steps[0], { '17a': '有彈針排氣（聽到聲音）' });
assert.equal(criteria.length, 1);
assert.equal(criteria[0].text, '有彈針排氣（聽到聲音）');
assert.equal(criteria[0].source, '模型');
assert.equal(criteria[0].evidence, 'clip@150-170、burst@160-162 #3');
assert.equal(criteria[0].window, '02:28.0–02:52.0');
assert.equal(SopGrading.buildCriterionRows(result.steps[3])[0].reason, '藥單看不見');
assert.equal(SopGrading.buildCriterionRows(result.steps[4])[0].review, '兩次嘗試結果不同 01:10、01:30');

// 不適用 and 無法判定 are left out of the denominator.
const score = SopGrading.scoreSummary(result);
assert.equal(score.total, 6);
assert.equal(score.gradable, 4);
assert.equal(score.correct, 2);
assert.equal(score.percent, 50);
assert.equal(score.needsReview, 2);
assert.deepEqual(score.counts, { '正確': 2, '部分正確': 1, '錯誤': 1, '無法判定': 1, '不適用': 1, '待判定': 0 });
assert.equal(SopGrading.scoreSummary({ steps: [result.steps[2]] }).percent, null);

const job = {
    id: 'job-1',
    result: {
        pipeline: 'sop_grading',
        sop_grading: [{
            video_uri: 'gs://bucket/videos/abc_src.mp4',
            criterion_texts: { '1a': '搓洗 15 秒' },
            result: { steps: [{ ...result.steps[1], explanation: '=HYPERLINK("x")' }, result.steps[0]] }
        }]
    }
};
const csv = SopGrading.buildGradingCsv(job, { 'gs://bucket/videos/abc_src.mp4': '考生1.mp4' });
assert.ok(csv.startsWith('﻿'));
const lines = csv.slice(1).split('\r\n').map(line => line.replace(/"/g, ''));
assert.equal(lines.length, 3);
assert.equal(lines[0], '工作編號,影片名稱,影片路徑,步驟編號,步驟,判定,正確,信心,需人工複核,說明,評分項目');
assert.ok(lines[1].startsWith('job-1,考生1.mp4,gs://bucket/videos/abc_src.mp4,1,洗手,正確,1,0.9,否,'));
assert.ok(lines[1].endsWith(',1a 正確：1a 說明'));
assert.ok(lines[2].includes(',17,手指輕彈空針，排出空氣,錯誤,0,'));
// a formula in a model explanation must not run in a spreadsheet
assert.ok(csv.includes(`"'=HYPERLINK(""x"")"`));

assert.equal(SopGrading.fileUrl('http://api', 'a/b', 2, 'result'),
    'http://api/api/v1/evaluations/a%2Fb/sop-grading/2/result');

console.log('sop-grading tests passed');
