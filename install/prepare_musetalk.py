"""Install an exact MuseTalk revision without overwriting an existing checkout."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def prepare(root=ROOT):
    spec = json.loads((root / 'install/musetalk-source.json').read_text())
    target = root / 'engines/MuseTalk'
    if not target.exists():
        target.mkdir(parents=True)
        subprocess.run(['git', 'init', str(target)], check=True)
        subprocess.run(['git', '-C', str(target), 'remote', 'add', 'origin', spec['repository']], check=True)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(target), *args], text=True).strip()
    try:
        head = git('rev-parse', 'HEAD')
    except subprocess.CalledProcessError:
        head = None
    if head is None:
        if git('status', '--porcelain'):
            raise RuntimeError('MuseTalk source is incomplete and contains files; move it aside before retrying.')
        subprocess.run(['git', '-C', str(target), 'fetch', '--depth', '1', spec['repository'], spec['commit']], check=True)
        subprocess.run(['git', '-C', str(target), 'checkout', '--detach', spec['commit']], check=True)
    elif head != spec['commit']:
        raise RuntimeError(f'MuseTalk revision {head} differs from required {spec["commit"]}. Preserve your existing checkout and move it aside before retrying; setup will not overwrite it.')
    # The application separates realtime and video avatar caches. Upstream lacks
    # this parameter; apply this small, idempotent compatibility patch explicitly.
    changed = set(git('diff', 'HEAD', '--name-only').splitlines())
    if changed - {'scripts/realtime_inference.py'}:
        raise RuntimeError('MuseTalk has local source edits; refusing to treat it as the pinned revision.')
    path = target / 'scripts/realtime_inference.py'
    original = git('show', f'{spec["commit"]}:scripts/realtime_inference.py') + '\n'
    patched = original.replace('batch_size, preparation):', 'batch_size, preparation, avatars_subdir="avatars", max_seconds=None):')
    patched = patched.replace('/avatars/{avatar_id}', '/{avatars_subdir}/{avatar_id}')
    patched = patched.replace('self.bbox_shift = bbox_shift', 'self.bbox_shift = bbox_shift\n        self.max_seconds = max_seconds')
    patched = patched.replace("video2imgs(self.video_path, self.full_imgs_path, ext='png')", """capture = cv2.VideoCapture(self.video_path)
            fps = capture.get(cv2.CAP_PROP_FPS) or 25
            capture.release()
            limit = max(1, int(self.max_seconds * fps)) if self.max_seconds else 10000000
            video2imgs(self.video_path, self.full_imgs_path, ext='png', cut_frame=limit - 1)""")
    current = path.read_text()
    if current.rstrip() not in (original.rstrip(), patched.rstrip()):
        raise RuntimeError('MuseTalk realtime_inference.py has local edits; refusing to overwrite them.')
    path.write_text(patched)

if __name__ == '__main__':
    prepare()
