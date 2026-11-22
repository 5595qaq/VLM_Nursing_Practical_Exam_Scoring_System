"""Verify archived source/results offline. No video, network or model calls."""
from pathlib import Path
import hashlib
import importlib.util
import inspect
import itertools
import json
import re
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

def verify():
    archived = read(HERE / 'files-sha256.json')
    for path, value in archived.items():
        assert digest(REPO / path) == value, path
    source = read(HERE / 'source-code.json')
    source_files = 0
    for profile in source.values():
        for item in profile['files'].values():
            assert digest(REPO / item['path']) == item['sha256'], item['path']
            source_files += 1
    current = module(REPO / 'sop-video-grading/scripts/rubric.py', 'current_rubric')
    rubric = current.load_rubric()
    old = {}
    for name in source:
        root = HERE / 'sources' / name / 'sop-video-grading'
        old[name] = (read(root / 'rubric.json'), module(root / 'scripts/rubric.py', name))
    for sid in [1, 2, 4, 29, 30]:
        profile = 'placement' if sid == 30 else 'separate174'
        assert rubric['steps'][sid - 1] == old[profile][0]['steps'][sid - 1], sid
    for group, profile in [('01-hand-washing.md', 'readsheet'), ('02-drug-sheet.md', 'readsheet'), ('09-disposal.md', 'placement')]:
        assert (REPO / 'sop-video-grading/criteria' / group).read_text(encoding='utf-8') == (
            HERE / 'sources' / profile / 'sop-video-grading/criteria' / group).read_text(encoding='utf-8'), group
    combinations = 0
    for sid in [1, 2, 4, 29, 30]:
        profile = 'placement' if sid == 30 else 'separate174'
        prev_rubric, previous = old[profile]
        step = rubric['steps'][sid - 1]
        for values in itertools.product(['正確','錯誤','部分正確','無法判定','待判定','不適用'], repeat=len(step['criteria'])):
            verdicts = dict(zip([c['id'] for c in step['criteria']], values))
            for reason in [None, '藥單看不見']:
                reasons = {c['id']: reason for c in step['criteria']}
                options = {'reasons': reasons} if 'reasons' in inspect.signature(previous.aggregate).parameters else {}
                assert current.aggregate(step, verdicts, reasons=reasons) == previous.aggregate(
                    prev_rubric['steps'][sid-1], verdicts, **options), (sid, values)
                combinations += 1
    index = read(HERE / 'index.json')
    assert len(index) == len({i['video_id'] for i in index}) == 30
    steps = 0
    for item in index:
        result = read(REPO / item['results'])
        assert result['video_id'] == item['video_id']
        assert {s['step_id'] for s in result['steps']} == {1,2,3,4,10,29,30}
        for step in result['steps']:
            meta = item['source_by_step'][str(step['step_id'])]
            path = REPO / meta['path']
            assert digest(path) == meta['original_sha256']
            assert step == next(s for s in read(path)['steps'] if s['step_id'] == step['step_id'])
            steps += 1
        record_path = REPO / item['cli_record']
        record = read(record_path)
        assert record['video_sha256'] == item['video_sha256']
        assert all(r['commit'] == source['separate174']['original_head'] for r in record['runs'])
        assert all(not r.get('failed_tasks') for r in record['runs'][-1:])
        assert read(record_path.parent / 'grading.json') == read(HERE/item['video_id']/'separate174/grading.json')
        assert not read(HERE/item['video_id']/'separate174/validation-selected.json')['errors']
    differences = read(HERE / 'differences.json')
    assert len(differences) == 28
    for case in differences:
        assert digest(REPO / case['source']) == case['original_sha256']
    # Validate current final records using the *actual historical CLI rubric*.
    scripts = HERE / 'sources/separate174/sop-video-grading/scripts'
    sys.path.insert(0, str(scripts))
    validator = module(scripts/'validate_grading.py', 'historical_validator')
    for item in index:
        grading = read((REPO/item['cli_record']).parent/'grading.json')
        errors = validator.validate_grading(grading, final=True)
        selected_errors = []
        for error in errors:
            step_id = re.search(r'\(step (\d+)\)', error)
            if step_id and int(step_id[1]) not in {4, 10} and 'still 待判定' in error:
                continue  # AGENTS permits only ungraded-step pending errors here.
            selected_errors.append(error)
        assert not selected_errors, (item['video_id'], selected_errors)
    summary = read(HERE/'metrics.json')['summary']
    assert (summary['compared'],summary['agreement'],summary['error_tp'],summary['error_total']) == (180,152,34,42)
    print(json.dumps({'videos':30,'steps_with_separate10':steps,'pdf_steps':180,'difference_source_hashes':28,
                      'source_files':source_files,'archived_files':len(archived),'aggregate_combinations':combinations,'native_cli_records':30,
                      'historical_selected_final_validations':30,'model_calls':0}, ensure_ascii=False))

if __name__ == '__main__':
    verify()
