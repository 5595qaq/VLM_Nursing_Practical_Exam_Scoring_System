"""Independent step-10 conversations; the published criterion id stays 10b."""
from copy import deepcopy


def split_tasks(tasks, segmentation):
    duration = float(segmentation['meta']['duration'])
    spans = sorted({(float(e['start']), float(e['end'])) for e in segmentation['steps']
                    if e['step_id'] == 10 and e['status'] == 'detected'
                    and e.get('start') is not None and e.get('end') is not None})
    result = []
    for task in tasks:
        if [c['criterion_id'] for c in task['criteria']] != ['10b'] or not spans:
            result.append(task)
            continue
        for index, (start, end) in enumerate(spans, 1):
            item = deepcopy(task)
            item['key'] = f"10-seg{index}" if len(spans) > 1 else task['key']
            item['segment'] = [start, end]
            item['window'] = [max(0.0, start - 2.0), min(duration, end + 2.0)]
            criterion = item['criteria'][0]
            criterion['window'] = item['window'][:]
            # Keep the original wrapper context to locate when unpacking actually ended.
            # A final 正確 must still cite THIS candidate, before first_use (action validator).
            result.append(item)
    return result


def merge_outcomes(outcomes):
    grouped = {}
    for task, criteria, _, error in outcomes:
        for criterion in criteria:
            grouped.setdefault(criterion['criterion_id'], []).append((task, criterion))
    graded = {}
    for cid, items in grouped.items():
        if cid != '10b' or not any('segment' in task for task, _ in items):
            if len(items) != 1:
                raise ValueError(f'Duplicate criterion outside segment grading: {cid}')
            graded[cid] = items[0][1]
            continue
        expected = [t for t, _, _, _ in outcomes if 'segment' in t]
        # An incomplete candidate must never silently become a global negative.
        if len(items) != len(expected):
            continue
        eligible = [(t,c) for t,c in items if t['criteria'][0].get('first_use') is None
                    or t['segment'][0] < t['criteria'][0]['first_use']]
        correct = [(t, c) for t, c in eligible if c['verdict'] == '正確']
        unknown = [(t, c) for t, c in eligible if c['verdict'] == '無法判定']
        chosen = correct or unknown or eligible or items
        base = deepcopy(chosen[0][1])
        base['window'] = [min(t['window'][0] for t, _ in items), max(t['window'][1] for t, _ in items)]
        # Fixing once after unpacking is sufficient under the existing 10b rubric.
        base['explanation'] = '；'.join(f"區間 {t['segment'][0]:g}-{t['segment'][1]:g} 秒：{c['verdict']}。{c['explanation']}"
                                      for t, c in items)
        base['times'] = sorted({tuple(span) for _, c in chosen for span in c.get('times', [])})
        base['times'] = [list(span) for span in base['times']]
        base['evidence'] = list(dict.fromkeys(e for _, c in chosen for e in c.get('evidence', [])))
        base['confidence'] = min(c['confidence'] for _, c in chosen)
        if not eligible:
            base.update(verdict='無法判定',reason='資訊不足',confidence=0.0,
                        explanation='所有候選區間皆在第一次使用空針之後，無法判定取出包裝後、第一次使用前的固定。'+base['explanation'])
        if correct:
            base.pop('needs_review', None)
            base.pop('review_reason', None)
            base['reason'] = None
        graded[cid] = base
    return graded
