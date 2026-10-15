(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.CsvExport = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const EXPECTED_CRITERIA = [
        ['Agent_A', 1], ['Agent_A', 2], ['Agent_A', 3], ['Agent_A', 4],
        ['Agent_B', 1], ['Agent_B', 5], ['Agent_B', 6], ['Agent_B', 7],
        ['Agent_B', 8], ['Agent_B', 9], ['Agent_B', 10], ['Agent_B', 12],
        ...Array.from({ length: 8 }, (_, i) => ['Agent_C', i + 13]),
        ...Array.from({ length: 10 }, (_, i) => ['Agent_D', i + 21])
    ];

    const SUMMARY_HEADERS = EXPECTED_CRITERIA.map(([agent, step]) =>
        step === 1 ? `Step 1 (${agent.replace('_', ' ')})` : `Step ${step}`
    );

    function firstDefined(...values) {
        return values.find(value => value !== undefined && value !== null && value !== '');
    }

    function parseBinary(value) {
        if (value === true || value === 1 || value === '1') return 1;
        if (value === false || value === 0 || value === '0') return 0;
        if (typeof value !== 'string') return null;
        const normalized = value.trim().toLowerCase();
        if (['通過', 'pass', 'passed', 'true'].includes(normalized)) return 1;
        if (['未通過', 'fail', 'failed', 'false'].includes(normalized)) return 0;
        return null;
    }

    function normalizeBoolean(value) {
        if (value === true || value === false) return value;
        if (typeof value === 'string') {
            if (value.toLowerCase() === 'true') return true;
            if (value.toLowerCase() === 'false') return false;
        }
        return '';
    }

    function itemVideoPath(item) {
        return firstDefined(item.Video_Path, item.video_path, item['影片路徑']) || '';
    }

    function itemAgent(item) {
        return firstDefined(item.Agent_Name, item.agent_name, item['評分代理']) || '';
    }

    function videoName(path) {
        if (!path) return '';
        const tail = String(path).split('/').filter(Boolean).pop() || String(path);
        try { return decodeURIComponent(tail); } catch (_) { return tail; }
    }

    function displayVideoName(path, names) {
        return names && Object.prototype.hasOwnProperty.call(names, path) && typeof names[path] === 'string'
            ? names[path] : videoName(path);
    }

    function normalizeItems(items, names) {
        const agentACounters = new Map();
        return (Array.isArray(items) ? items : []).map(item => {
            const videoPath = itemVideoPath(item);
            const agent = itemAgent(item);
            let stepNumber = Number(firstDefined(item['步驟編號'], item.step_number));
            if (!Number.isInteger(stepNumber)) {
                stepNumber = null;
                if (agent === 'Agent_A') {
                    const counterKey = `${videoPath}\u0000${agent}`;
                    const next = (agentACounters.get(counterKey) || 0) + 1;
                    agentACounters.set(counterKey, next);
                    if (next <= 4) stepNumber = next;
                }
            }

            const score = parseBinary(firstDefined(item['得分'], item.score, item.passed, item['判定'], item.status));
            const verdictSource = firstDefined(item['判定'], item.status, item.passed, item.score, item['得分']);
            const verdictScore = parseBinary(verdictSource);
            const verdict = verdictScore === 1 ? '通過' : verdictScore === 0 ? '未通過' : '';
            const start = firstDefined(item.start_time);
            const end = firstDefined(item.end_time);
            const evidenceTime = firstDefined(
                item['證據時間點'], item.timestamp, item.timecode,
                start && end ? `${start}-${end}` : start || end
            ) || '';
            const reason = firstDefined(
                item['理由'],
                score === 1 && item.success_reason !== 'none' ? item.success_reason : undefined,
                score === 0 && item.failure_reason !== 'none' ? item.failure_reason : undefined,
                item.success_reason !== 'none' ? item.success_reason : undefined,
                item.failure_reason !== 'none' ? item.failure_reason : undefined,
                item.reasoning
            ) || '';

            const missing = [];
            if (!videoPath) missing.push('影片路徑');
            if (!agent) missing.push('Agent');
            if (!Number.isInteger(stepNumber)) missing.push('步驟編號');
            if (score === null) missing.push('有效分數');

            return {
                videoPath,
                videoName: displayVideoName(videoPath, names),
                agent,
                stepNumber,
                stepName: firstDefined(item['步驟'], item.step_description) || (stepNumber ? `Step ${stepNumber}` : ''),
                verdict,
                score,
                evidenceTime,
                observation: firstDefined(item['看到什麼'], item.evidence, item.physical_evidence) || '',
                confidence: firstDefined(item['信心'], item.confidence) ?? '',
                reason,
                humanReview: normalizeBoolean(firstDefined(item['需人工複核'], item.needs_human_review)),
                dataStatus: missing.length ? `缺漏：${missing.join('、')}` : '完整',
                rawJson: JSON.stringify(item)
            };
        });
    }

    function createSummaryRows(job, names) {
        const normalized = normalizeItems(job?.result?.items, names);
        const paths = Array.from(new Set([
            ...(Array.isArray(job?.video_paths) ? job.video_paths : []),
            ...normalized.map(item => item.videoPath).filter(Boolean)
        ]));

        return paths.map(path => {
            const videoItems = normalized.filter(item => item.videoPath === path);
            const byCriterion = new Map();
            videoItems.forEach(item => {
                if (item.agent && Number.isInteger(item.stepNumber)) {
                    const key = `${item.agent}:${item.stepNumber}`;
                    if (!byCriterion.has(key)) byCriterion.set(key, item.score);
                }
            });
            const scores = EXPECTED_CRITERIA.map(([agent, step]) => {
                const value = byCriterion.get(`${agent}:${step}`);
                return value === 0 || value === 1 ? value : '';
            });
            const validScores = scores.filter(value => value === 0 || value === 1);
            const total = validScores.reduce((sum, value) => sum + value, 0);
            const missingCount = EXPECTED_CRITERIA.length - validScores.length;
            return [
                job?.id || '', displayVideoName(path, names), path,
                missingCount ? `缺漏 ${missingCount} 項` : '完整',
                validScores.length, total,
                validScores.length ? `${((total / validScores.length) * 100).toFixed(2)}%` : '',
                ...scores
            ];
        });
    }

    function createDetailRows(job, names) {
        return normalizeItems(job?.result?.items, names).map(item => [
            job?.id || '', job?.exam_topic || '', item.videoName, item.videoPath,
            item.agent, item.stepNumber ?? '', item.stepName, item.verdict,
            item.score ?? '', item.evidenceTime, item.observation, item.confidence,
            item.reason, item.humanReview, item.dataStatus, item.rawJson
        ]);
    }

    function protectFormula(value) {
        const text = value === null || value === undefined ? '' : String(value);
        return /^[\t\r\n ]*[=+\-@]/.test(text) ? `'${text}` : text;
    }

    function csvCell(value) {
        const safe = protectFormula(value).replace(/"/g, '""');
        return `"${safe}"`;
    }

    function toCsv(headers, rows) {
        return '\uFEFF' + [headers, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n');
    }

    function buildSummaryCsv(job, names) {
        return toCsv(
            ['工作編號', '影片名稱', '影片路徑', '資料狀態', '有效評分數', '總分', '通過率', ...SUMMARY_HEADERS],
            createSummaryRows(job, names)
        );
    }

    function buildDetailCsv(job, names) {
        return toCsv(
            ['工作編號', '考試項目', '影片名稱', '影片路徑', 'Agent', '步驟編號', '步驟名稱', '判定', '得分', '證據時間', '看到什麼', '信心', '理由', '需人工複核', '資料狀態', '原始項目 JSON'],
            createDetailRows(job, names)
        );
    }

    return {
        EXPECTED_CRITERIA, SUMMARY_HEADERS, normalizeItems, createSummaryRows,
        createDetailRows, buildSummaryCsv, buildDetailCsv, videoName, toCsv
    };
});
