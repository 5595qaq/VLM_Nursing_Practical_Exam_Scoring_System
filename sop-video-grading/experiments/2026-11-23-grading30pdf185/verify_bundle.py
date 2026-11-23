"""Verify archived source/results offline. No video, network or model calls."""
from pathlib import Path
import hashlib
import importlib.util
import inspect
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

def verify_archived_step(step, meta, profiles):
    """Check the saved verdict with the rubric that actually produced it."""
    name = meta['rule_profile']
    if name == 'original':
        # Step 3 reuses original results; that profile has no archived source code.
        assert step['step_id'] == 3
        return 0
    rubric, logic = profiles[name]
    assert rubric['version'] == meta['rubric_version'], (step['step_id'], name, 'rubric version')
    rule = next(s for s in rubric['steps'] if s['step_id'] == step['step_id'])
    criteria = step['criteria']
    ids = [c['criterion_id'] for c in criteria]
    assert len(ids) == len(set(ids)) and set(ids) == {c['id'] for c in rule['criteria']}, (step['step_id'], 'criteria')
    verdicts = {c['criterion_id']: c['verdict'] for c in criteria}
    reasons = {c['criterion_id']: c.get('reason') for c in criteria}
    options = {'reasons': reasons} if 'reasons' in inspect.signature(logic.aggregate).parameters else {}
    actual = logic.aggregate(rule, verdicts, **options)
    assert actual == step['verdict'], (step['step_id'], name, actual, step['verdict'])
    return 1


def historical_validator(scripts, rubric_module):
    """Bind historical validator imports without leaking into other tools/tests."""
    previous = sys.modules.get('rubric')
    sys.modules['rubric'] = rubric_module
    try:
        return module(scripts / 'validate_grading.py', 'historical_validator')
    finally:
        if previous is None:
            sys.modules.pop('rubric', None)
        else:
            sys.modules['rubric'] = previous


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
    old = {}
    for name in source:
        root = HERE / 'sources' / name / 'sop-video-grading'
        old[name] = (read(root / 'rubric.json'), module(root / 'scripts/rubric.py', name))
    # This archive combines historical runs. Current main is not their source.
    aggregations = 0
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
            original = read(path)
            assert original['rubric_version'] == meta['rubric_version'], (item['video_id'], step['step_id'], 'source version')
            assert step == next(s for s in original['steps'] if s['step_id'] == step['step_id'])
            aggregations += verify_archived_step(step, meta, old)
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
    validator = historical_validator(scripts, old['separate174'][1])
    for item in index:
        grading = read((REPO/item['cli_record']).parent/'grading.json')
        errors = validator.validate_grading(grading, rubric=old['separate174'][0], final=True)
        selected_errors = []
        for error in errors:
            step_id = re.search(r'\(step (\d+)\)', error)
            if step_id and int(step_id[1]) not in {4, 10} and 'still 待判定' in error:
                continue  # AGENTS permits only ungraded-step pending errors here.
            selected_errors.append(error)
        assert not selected_errors, (item['video_id'], selected_errors)
    summary = read(HERE/'metrics.json')['summary']
    assert (summary['compared'],summary['agreement'],summary['error_tp'],summary['error_total']) == (180,152,34,42)
    result = {'videos':30,'steps_with_separate10':steps,'pdf_steps':180,'difference_source_hashes':28,
                      'source_files':source_files,'archived_files':len(archived),'historical_step_aggregations':aggregations,'native_cli_records':30,
                      'historical_selected_final_validations':30,'model_calls':0,
                      'current_main_equivalence_checked':False,'new_video_regression':False}
    print(json.dumps(result, ensure_ascii=False))
    return result

if __name__ == '__main__':
    verify()
