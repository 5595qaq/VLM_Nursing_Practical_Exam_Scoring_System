"""離線逐字核對保存快照與本 PR 的 SOP 4 規則；不呼叫模型。"""
import hashlib
import json
from pathlib import Path


def sha(text):
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def section(text, start, end):
    text = text.replace('\r\n', '\n')
    left = text.index(start)
    return text[left:text.index(end, left)]


def main():
    data = Path(__file__).resolve().parent
    repo = Path(__file__).resolve().parents[3]
    source = json.loads((data / 'source-rules.json').read_text(encoding='utf-8'))
    skill = (repo / 'sop-video-skill/SKILL.md').read_text(encoding='utf-8')
    refs = (repo / 'sop-video-skill/references/sop_steps.md').read_text(encoding='utf-8')
    checks = []
    for step, profile, start, end in [
        (4, 'readsheet', '  - **4 抽藥前確認的三類動作**', '- **步驟 29 使用原有切分方式**'),
    ]:
        previous = section(source[profile]['segmentation_skill'], start, end)
        current = section(skill, start, end)
        checks.append({'step_id': step, 'source_profile': profile,
                       'path': 'sop-video-skill/SKILL.md',
                       'normalization': 'CRLF to LF only; preserve all section whitespace',
                       'source_section_sha256': sha(previous),
                       'pr_section_sha256': sha(current), 'equal': previous == current})
        previous = next(line for line in source[profile]['step_definitions'].splitlines()
                        if line.startswith(f'{step}. **'))
        current = next(line for line in refs.splitlines() if line.startswith(f'{step}. **'))
        checks.append({'step_id': step, 'source_profile': profile,
                       'path': 'sop-video-skill/references/sop_steps.md',
                       'normalization': 'single complete definition line without line terminator',
                       'source_section_sha256': sha(previous),
                       'pr_section_sha256': sha(current), 'equal': previous == current})
    result = {'scope': 'target_rule_text_only', 'model_calls': 0,
              'source': 'source-rules.json', 'checks': checks,
              'all_equal': all(item['equal'] for item in checks),
              'does_not_prove_full_prompt_or_main_grading_equivalence': True}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['all_equal'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
