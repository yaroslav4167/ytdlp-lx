"""Rebuild compact widget JavaScript after editing widget.js: python -m bubbles.build."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from .service import ROOT

name = os.environ.get('BUBBLES_DESIGN', 'default')
if not name.replace('-', '').replace('_', '').isalnum():
    raise ValueError('Invalid BUBBLES_DESIGN')
folder = ROOT / 'designs' / name
source = folder / 'widget.js'
output = folder / 'widget.min.js'
# Preserve inline HTML handlers and the I18N key marker; compact other bindings.
subprocess.run(['npx', '--yes', '--package=terser@5.51.2', 'terser', str(source),
                '--ecma', '2020', '--compress', 'passes=3', '--mangle', "toplevel,reserved=['I','download','recover','ask','selected','render']", '--output', str(output)], check=True)
(folder / 'widget.build.json').write_text(json.dumps({
    'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
    'terser': '5.51.2',
}) + '\n')
print(f'Built {output.relative_to(ROOT)}')
