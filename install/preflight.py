"""Check disk space and NVIDIA hardware before expensive installation work."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3

def check_space(root=ROOT):
    # Conservative working-space allowance, including downloads/builds and renders.
    for path, required in [(root, 60 * GIB), (Path('/tmp'), 8 * GIB), (Path('/'), 5 * GIB)]:
        free = shutil.disk_usage(path).free
        if free < required:
            raise RuntimeError(f'Không đủ dung lượng tại {path}: còn {free / GIB:.1f} GiB, cần ít nhất {required / GIB:.0f} GiB trống.')

def has_nvidia(sysfs=Path('/sys/bus/pci/devices')):
    for device in sysfs.glob('*'):
        try:
            if (device / 'vendor').read_text().strip() == '0x10de' and (device / 'class').read_text().strip().startswith('0x03'):
                return True
        except OSError:
            pass
    return False

if __name__ == '__main__':
    import sys
    if '--nvidia' in sys.argv:
        raise SystemExit(0 if has_nvidia() else 1)
    check_space()
