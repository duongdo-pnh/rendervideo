"""Load the actual engines and render a short bundled sample before declaring ready."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def run(command, **kwargs):
    subprocess.run([str(x) for x in command], check=True, timeout=1800, **kwargs)

def musetalk(video, audio, output):
    os.chdir(ROOT / 'engines/MuseTalk')
    import musetalk_render_server as server
    server._load_runtime()  # Includes DWPose, face detector, Whisper, UNet, VAE and parsing.
    server.AVATARS_DIR_NAME = 'avatars_setup_test'
    import shutil
    avatar_id = 'setup_' + uuid.uuid4().hex
    try:
        server._render_group([dict(avatar_id=avatar_id, video_path=video,
                                  audio_path=audio, output_path=output, batch_size=2, fps=25)])
    finally:
        shutil.rmtree(server.ROOT / 'results/v15/avatars_setup_test' / avatar_id, ignore_errors=True)

def verify():
    from install.environment import missing_files
    missing = missing_files()
    if missing:
        raise RuntimeError('Missing model/environment files: ' + ', '.join(missing))
    os.chdir(ROOT)
    # Import the API without invoking lifespan/startup or touching the job queue.
    run([ROOT / 'venv/bin/python', '-c', 'import backend.app'])
    env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
    with tempfile.TemporaryDirectory(prefix='setup-render-', dir=ROOT / 'logs') as directory:
        work = Path(directory)
        video, audio = work / 'input.mp4', work / 'input.wav'
        run(['ffmpeg', '-v', 'error', '-y', '-i', ROOT / 'assets/demo1_video.mp4', '-t', '1', '-r', '25', '-an', video])
        run(['ffmpeg', '-v', 'error', '-y', '-i', ROOT / 'assets/demo1_audio.wav', '-t', '1', '-ar', '16000', '-ac', '1', audio])
        for engine in ['latentsync', 'musetalk']:
            output = work / (engine + '.mp4')
            if engine == 'latentsync':
                run([ROOT / 'venv/bin/python', ROOT / 'render_job.py', '--engine', engine,
                     '--video', video, '--audio', audio, '--output', output,
                     '--config', 'configs/unet/stage2_512.yaml', '--checkpoint', 'checkpoints/latentsync_unet.pt',
                     '--steps', '2', '--enhance_mouth', '1'], env=env)
            else:
                run([ROOT / 'engines/MuseTalk/.venv/bin/python', __file__, '--musetalk',
                     '--video', video, '--audio', audio, '--output', output], env=env)
            data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)], text=True))
            kinds = {stream['codec_type'] for stream in data['streams']}
            if not {'video', 'audio'} <= kinds or float(data['format'].get('duration', 0)) < 0.5:
                raise RuntimeError(f'{engine} produced an invalid render')
            run(['ffmpeg', '-v', 'error', '-xerror', '-i', output, '-f', 'null', '-'])
            print(f'{engine}: model loading, render, audio/video decode passed', flush=True)
    receipt = ROOT / '.environment-ready.json'
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(json.dumps({'schema': 1, 'engines': ['latentsync', 'musetalk']}))
    temporary.replace(receipt)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--musetalk', action='store_true')
    parser.add_argument('--prepare-musetalk', action='store_true')
    for arg in ['video', 'audio', 'output']:
        parser.add_argument('--' + arg)
    args = parser.parse_args()
    if args.prepare_musetalk:
        os.chdir(ROOT / 'engines/MuseTalk')
        import musetalk_render_server as server
        server._load_runtime()
    elif args.musetalk:
        musetalk(args.video, args.audio, args.output)
    else:
        verify()
