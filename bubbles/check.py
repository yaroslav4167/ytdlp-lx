"""Run `python -m bubbles.check` before deploying design or translation changes."""
from .service import ROOT, load_design, load_loading_design
import zlib

for path in sorted((ROOT / 'locales').glob('*.json')):
    source = load_design(path.stem).encode()
    print(f'{path.stem}: {len(source)} HTML bytes, {len(zlib.compress(source, 9))}/4096 compressed bytes')

    loader=load_loading_design(path.stem).encode()
    print(f"{path.stem} processing: {len(loader)} HTML bytes, {len(zlib.compress(loader,9))}/4096 compressed bytes")
