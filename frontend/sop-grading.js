// Pure helpers for sop-video-grading results (result.pipeline === 'sop_grading').
// Each run is {video_uri, result, criterion_texts, files, ...}; result follows
// sop-video-grading/schema.json. Rendering lives in app.js.
(function (root, factory) {
    const isNode = typeof module === 'object' && module.exports;
    const csv = isNode ? require('./csv-export.js') : root.CsvExport;
    const sop = isNode ? require('./sop-result.js') : root.SopResult;
    const api = factory(csv, sop);
    if (isNode) module.exports = api;
    root.SopGrading = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (CsvExport, SopResult) {
    const VERDICTS = ['正確', '部分正確', '錯誤', '無法判定', '不適用', '待判定'];

    // CSS class per verdict (style.css: .grade-*).
    const VERDICT_CLASSES = {
        '正確': 'correct',
        '部分正確': 'partial',
        '錯誤': 'wrong',
        '無法判定': 'undetermined',
        '不適用': 'not-applicable',
        '待判定': 'pending'
    };

    const SOURCE_LABELS = { rule: '程式規則', vlm: '模型', fixed: '固定' };

    function confidence(value) {
        return typeof value === 'number' ? value : '';
    }

    function stepsOf(result) {
        const steps = Array.isArray(result?.steps) ? [...result.steps] : [];
        return steps.sort((a, b) => a.step_id - b.step_id);
    }

    function buildStepRows(result) {
        return stepsOf(result).map(step => ({
            stepId: step.step_id,
            stepName: SopResult.STEP_NAMES[step.step_id] || '',
            verdict: step.verdict || '',
            verdictClass: VERDICT_CLASSES[step.verdict] || '',
            explanation: step.explanation || '',
            confidence: confidence(step.confidence),
            needsReview: Boolean(step.needs_review || (step.criteria || []).some(c => c.needs_review))
        }));
    }

    function buildCriterionRows(step, texts = {}) {
        return (step?.criteria || []).map(criterion => ({
            id: criterion.criterion_id,
            text: texts[criterion.criterion_id] || '',
            verdict: criterion.verdict || '',
            verdictClass: VERDICT_CLASSES[criterion.verdict] || '',
            source: SOURCE_LABELS[criterion.source] || criterion.source || '',
            explanation: criterion.explanation || '',
            evidence: (criterion.evidence || []).join('、'),
            window: Array.isArray(criterion.window) ? SopResult.formatSpan(...criterion.window) : '',
            reason: criterion.reason || '',
            confidence: confidence(criterion.confidence),
            review: criterion.needs_review ? (criterion.review_reason || '需人工複核') : ''
        }));
    }

    // Score = 正確 steps / steps that can be judged (不適用 and 無法判定 are left out).
    function scoreSummary(result) {
        const steps = stepsOf(result);
        const counts = Object.fromEntries(VERDICTS.map(verdict => [verdict, 0]));
        steps.forEach(step => {
            if (step.verdict in counts) counts[step.verdict] += 1;
        });
        const gradable = steps.length - counts['不適用'] - counts['無法判定'];
        return {
            counts,
            total: steps.length,
            gradable,
            correct: counts['正確'],
            percent: gradable > 0 ? Math.round(counts['正確'] / gradable * 1000) / 10 : null,
            needsReview: buildStepRows(result).filter(row => row.needsReview).length
        };
    }

    function buildGradingCsv(job, names = {}) {
        const rows = [];
        (job?.result?.sop_grading || []).forEach(run => {
            const videoName = names[run.video_uri] || CsvExport.videoName(run.video_uri);
            stepsOf(run.result).forEach(step => {
                const row = buildStepRows({ steps: [step] })[0];
                const criteria = buildCriterionRows(step, run.criterion_texts || {})
                    .map(c => `${c.id} ${c.verdict}：${c.explanation}`).join('\n');
                rows.push([
                    job.id || '', videoName, run.video_uri, row.stepId, row.stepName, row.verdict,
                    row.verdict === '正確' ? 1 : 0, row.confidence, row.needsReview ? '是' : '否',
                    row.explanation, criteria
                ]);
            });
        });
        return CsvExport.toCsv(
            ['工作編號', '影片名稱', '影片路徑', '步驟編號', '步驟', '判定', '正確', '信心', '需人工複核',
             '說明', '評分項目'],
            rows
        );
    }

    function fileUrl(apiBase, jobId, position, kind) {
        return `${apiBase}/api/v1/evaluations/${encodeURIComponent(jobId)}/sop-grading/${position}/${kind}`;
    }

    return {
        VERDICTS, VERDICT_CLASSES, SOURCE_LABELS, buildStepRows, buildCriterionRows, scoreSummary,
        buildGradingCsv, fileUrl
    };
});
