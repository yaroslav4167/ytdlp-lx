"""Run `python -m bubbles.check` before deploying design or translation changes."""
from .service import ROOT, load_design
import zlib

for path in sorted((ROOT / 'locales').glob('*.json')):
    source = load_design(path.stem).encode()
    print(f'{path.stem}: {len(source)} HTML bytes, {len(zlib.compress(source, 9))}/4096 compressed bytes')
