"""No model requests: independent step-10 conversations and how their grades merge."""
from copy import deepcopy
import json
import pytest
from app.services.sop_segment_grading import split_tasks, merge_outcomes


def task():
    return {'key':'10', 'window_steps':[10], 'window':[0,90], 'needs_clip':False,
            'criteria':[{'criterion_id':'10b','window':[0,90], 'package_spans':[[5,8],[40,42]],'first_use':70}]}


def seg():
    return {'video_id':'VID_0001','meta':{'duration':100},'steps':[
        {'step_id':10,'status':'detected','start':5,'end':8},
        {'step_id':10,'status':'detected','start':40,'end':42}]}


def grade(verdict, window):
    return {'criterion_id':'10b','verdict':verdict,'source':'vlm','explanation':'00:05 看清雙手與空針後判定。',
            'times':[window], 'window':window, 'evidence':['clip@5-8'],'confidence':.8,
            'reason':'資訊不足' if verdict=='無法判定' else None}


def test_split_has_independent_ranges_and_does_not_change_input():
    original=task(); before=deepcopy(original)
    tasks=split_tasks([original],seg())
    assert original==before
    assert [x['key'] for x in tasks]==['10-seg1','10-seg2']
    assert [x['window'] for x in tasks]==[[3,10],[38,44]]
    assert all(x['criteria'][0]['package_spans']==[[5,8],[40,42]] for x in tasks)


@pytest.mark.parametrize('verdicts,expected',[
    (['錯誤','正確'],'正確'),(['正確','錯誤'],'正確'),
    (['錯誤','無法判定'],'無法判定'),(['無法判定','錯誤'],'無法判定'),
    (['錯誤','錯誤'],'錯誤')])
def test_merge_is_not_last_writer_wins(verdicts,expected):
    tasks=split_tasks([task()],seg())
    out=[(t,[grade(v,t['window'])],None,None) for t,v in zip(tasks,verdicts)]
    result=merge_outcomes(out)['10b']
    assert result['verdict']==expected
    assert '區間 5-8 秒' in result['explanation'] and '區間 40-42 秒' in result['explanation']


def test_failed_candidate_cannot_become_global_wrong():
    tasks=split_tasks([task()],seg())
    out=[(tasks[0],[grade('錯誤',tasks[0]['window'])],None,None),
         (tasks[1],[],None,ConnectionError('interrupted'))]
    assert '10b' not in merge_outcomes(out)


def test_late_candidate_cannot_supply_a_passing_grade():
    original=task();original['criteria'][0]['first_use']=20
    tasks=split_tasks([original],seg())
    out=[(t,[grade(v,t['window'])],None,None) for t,v in zip(tasks,['錯誤','正確'])]
    assert merge_outcomes(out)['10b']['verdict']=='錯誤'


def test_step4_and_other_steps_do_not_change():
    ordinary={'key':'4','criteria':[{'criterion_id':'4a'},{'criterion_id':'4b'}]}
    assert split_tasks([ordinary],seg())==[ordinary]


def test_failed_candidate_key_does_not_mark_other_steps_failed():
    from app.services import sop_grading_service as grading
    rr=grading.load_grading_module('run_record')
    tasks=split_tasks([task()],seg())
    assert rr.failed_steps([{'task':t['key']} for t in tasks])=={10}


def test_passing_candidate_cannot_cite_another_window_or_first_use():
    from app.services import sop_grading_service as grading
    validator=grading.load_grading_module('validate_grading_action')
    rubric=grading.load_grading_module('rubric').load_rubric()
    t=split_tasks([task()],seg())[0]
    t['criteria'][0]['first_use']=7
    c=grade('正確',[5,8]);c.pop('source');c.pop('window')
    errors=validator.validate({'action':'final','criteria':[c]},t,{},100,rubric)
    assert any('before the syringe' in x for x in errors)
    t['criteria'][0]['first_use']=70;c['times']=[[40,41]]
    errors=validator.validate({'action':'final','criteria':[c]},t,{},100,rubric)
    assert any('this candidate window' in x for x in errors)
