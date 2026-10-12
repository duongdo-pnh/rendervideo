"""BasicSR 1.4.2 uses a torchvision import removed by the pinned modern build."""
from importlib.util import find_spec
from pathlib import Path

spec = find_spec('basicsr')
if spec is None or spec.origin is None:
    raise RuntimeError('BasicSR was not installed')
path = Path(spec.origin).parent / 'data/degradations.py'
old = 'from torchvision.transforms.functional_tensor import rgb_to_grayscale'
new = 'from torchvision.transforms.functional import rgb_to_grayscale'
source = path.read_text()
if old in source:
    path.write_text(source.replace(old, new))
elif new not in source:
    raise RuntimeError('Unrecognized BasicSR version: cannot verify torchvision compatibility')
