import json
import os
import sys

target_files = [
    'models/models.py',
    'webhooks.py',
    'serializers_/events.py',
    'filtersets.py',
    'api/urls.py',
    'api/views.py',
]

path = r'C:/Users/Sayantan/.claude/projects/D--compareModels-c2-netbox/c9196b88-6dcd-4043-8bb2-f84a96ddf301.jsonl'

os.makedirs('_dump', exist_ok=True)
entries = []
with open(path, 'r', encoding='utf-8') as f:
    for i, line in enumerate(f):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get('message', {})
        content = msg.get('content', [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'tool_use' and c.get('name') in ('Edit', 'Write'):
                inp = c.get('input', {})
                fp = inp.get('file_path', '') or ''
                fp_norm = fp.replace('\\', '/')
                for t in target_files:
                    if t in fp_norm:
                        # save full input
                        slug = fp_norm.replace('/', '_').replace(':', '_')
                        outf = f'_dump/line{i+1:04d}_{c["name"]}_{slug}.json'
                        with open(outf, 'w', encoding='utf-8') as o:
                            json.dump(inp, o, indent=2)
                        print(f'wrote {outf}')
                        break
