// Pure helpers for sop-video-skill results (result.pipeline === 'sop_skill').
// The result JSON follows sop-video-skill/schema.json; rendering lives in app.js.
(function (root, factory) {
    const csv = typeof module === 'object' && module.exports ? require('./csv-export.js') : root.CsvExport;
    const api = factory(csv);
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.SopResult = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (CsvExport) {
    // Step names from sop-video-skill/references/sop_steps.md.
    const STEP_NAMES = {
        1: '洗手', 2: '看給藥紀錄單取出所需藥物（1讀）', 3: '檢查藥物有無變質、過期',
        4: '看給藥治療紀錄單確認藥物（2讀）', 5: '將 vial 瓶口打開', 6: '以酒精棉片消毒瓶口',
        7: '將稀釋液瓶口消毒', 8: '將稀釋液瓶口打開', 9: '撕開空針包裝袋', 10: '固定針頭與針心，取出空針',
        11: '（已自評分表移除）將瓶口上酒精棉片移除', 12: '以左手中指及食指夾住稀釋液瓶',
        13: '針頭斜面朝下插入稀釋液瓶', 14: '抽出稀釋液至所需量之刻度', 15: '酒棉墊在針心和針頭交接處',
        16: '針心往後抽', 17: '手指輕彈空針，排出空氣', 18: '將稀釋液插入 vial 中心並打入',
        19: '抽出針頭，單手套上針頭套', 20: '雙手旋轉 vial 瓶', 21: '空針拔蓋後，抽取空氣',
        22: '將空針插入 vial 中心打入空氣', 23: '將 vial 舉起，空針朝上，瓶口朝下',
        24: '抽出藥液至所需藥量之刻度', 25: '備用酒精棉片墊針柄', 26: '針筒垂直拿起，針心往後抽',
        27: '手指輕彈空針，排出空氣', 28: '單手套上針頭套', 29: '看給藥治療紀錄單丟棄藥物空瓶（3讀）',
        30: '空針由針心處放入空針包裝或針架'
    };

    const STATUS_LABELS = {
        detected: '偵測到',
        not_visually_determinable: '無法由畫面判定',
        not_performed: '未執行'
    };

    const OBSERVATION_LABELS = {
        out_of_order: '順序偏離',
        repeated: '重複',
        extra_action: '額外動作',
        interrupted: '中斷',
        skipped: '略過',
        left_work_area: '離開工作區',
        other: '其他'
    };

    function formatSeconds(value) {
        if (typeof value !== 'number' || !Number.isFinite(value)) return '';
        const minutes = Math.floor(value / 60);
        const seconds = value - minutes * 60;
        return `${String(minutes).padStart(2, '0')}:${seconds.toFixed(1).padStart(4, '0')}`;
    }

    function formatSpan(start, end) {
        const from = formatSeconds(start);
        const to = formatSeconds(end);
        return from || to ? `${from}–${to}` : '';
    }

    function stepNotes(step) {
        const notes = [];
        if (Array.isArray(step.logical_window)) notes.push(`邏輯區間 ${formatSpan(...step.logical_window)}`);
        if (step.merged_with?.length) notes.push(`與步驟 ${step.merged_with.join('、')} 合併`);
        if (step.handoff) notes.push(`交給 ${step.handoff}`);
        return notes.join('；');
    }

    // One row per step entry (a repeated step has several), ordered by step then start time.
    function buildStepRows(result) {
        const steps = Array.isArray(result?.steps) ? [...result.steps] : [];
        steps.sort((a, b) => (a.step_id - b.step_id) || ((a.start ?? Infinity) - (b.start ?? Infinity)));
        return steps.map(step => ({
            stepId: step.step_id,
            stepName: STEP_NAMES[step.step_id] || '',
            status: step.status,
            statusLabel: STATUS_LABELS[step.status] || step.status || '',
            start: formatSeconds(step.start),
            end: formatSeconds(step.end),
            confidence: typeof step.confidence === 'number' ? step.confidence : '',
            evidence: step.evidence || '',
            notes: stepNotes(step)
        }));
    }

    function buildObservationRows(result, sequenceCheck = []) {
        const rows = (result?.observations || []).map(item => ({ ...item, source: '模型' }));
        sequenceCheck.forEach(item => rows.push({ ...item, source: 'sequence_check.py' }));
        return rows.map(item => ({
            type: OBSERVATION_LABELS[item.type] || item.type,
            span: formatSpan(item.start, item.end),
            steps: (item.steps || []).join('、'),
            description: item.description || '',
            evidence: item.evidence || '',
            confidence: typeof item.confidence === 'number' ? item.confidence : '',
            source: item.source
        }));
    }

    function statusCounts(result) {
        const counts = { detected: 0, not_visually_determinable: 0, not_performed: 0 };
        new Map((result?.steps || []).map(step => [step.step_id, step.status])).forEach(status => {
            if (status in counts) counts[status] += 1;
        });
        return counts;
    }

    function buildStepsCsv(job, names = {}) {
        const rows = [];
        (job?.result?.sop_skill || []).forEach(run => {
            const videoName = names[run.video_uri] || CsvExport.videoName(run.video_uri);
            buildStepRows(run.result).forEach(row => rows.push([
                job.id || '', videoName, run.video_uri, row.stepId, row.stepName, row.statusLabel,
                row.start, row.end, row.confidence, row.evidence, row.notes
            ]));
        });
        return CsvExport.toCsv(
            ['工作編號', '影片名稱', '影片路徑', '步驟編號', '步驟', '判定狀態', '開始', '結束', '信心', '依據', '備註'],
            rows
        );
    }

    function fileUrl(apiBase, jobId, position, kind) {
        return `${apiBase}/api/v1/evaluations/${encodeURIComponent(jobId)}/sop-skill/${position}/${kind}`;
    }

    return {
        STEP_NAMES, STATUS_LABELS, OBSERVATION_LABELS, formatSeconds, formatSpan,
        buildStepRows, buildObservationRows, statusCounts, buildStepsCsv, fileUrl
    };
});
