"""Validate explicit reuse of a saved model segmentation without inventing provenance."""
import hashlib
import json
from pathlib import Path


def load_bundle(path, video_id, validator):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    result = data['segmentation']
    if data.get('source_kind') != 'saved_model_segmentation' or result.get('video_id') != video_id:
        raise ValueError('Saved segmentation source or video id does not match')
    if not isinstance(data.get('video_sha256'), str) or len(data['video_sha256']) != 64:
        raise ValueError('Saved segmentation needs the original video SHA-256')
    errors = validator(result)
    if errors:
        raise ValueError('Invalid saved segmentation: ' + '; '.join(errors[:5]))
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(serialized).hexdigest()
    return data, 'saved-model-' + digest
