"""Download required models with checked errors; never upgrade dependencies."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    # Rust-backed multi-connection transfer (hf-transfer, in requirements.txt) —
    # noticeably faster than huggingface_hub's default single-stream downloader
    # for the several-GB checkpoint set below. Must be set before the import.
    os.environ.setdefault('HF_HUB_ENABLE_HF_TRANSFER', '1')
    from huggingface_hub import hf_hub_download
    os.chdir(ROOT)
    def hf(repo, names, destination):
        for name in names:
            hf_hub_download(repo, name, local_dir=str(ROOT / destination))
    def url(source, name):
        target = ROOT / name
        if target.is_file() and target.stat().st_size > 0:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + '.part')
        subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--connect-timeout', '30', source, '-o', str(partial)], check=True)
        partial.replace(target)
    hf('ByteDance/LatentSync-1.6', ['latentsync_unet.pt', 'whisper/tiny.pt'], 'checkpoints')
    # diffusers' AutoencoderKL.from_pretrained() prefers .safetensors and only falls back to
    # .bin (with a "Defaulting to unsafe serialization" warning, logged on every render) when
    # the .safetensors file is missing locally — download both so that fallback never triggers.
    hf('stabilityai/sd-vae-ft-mse',
       ['config.json', 'diffusion_pytorch_model.bin', 'diffusion_pytorch_model.safetensors'], 'checkpoints/sd-vae')
    base = 'engines/MuseTalk/models'
    hf('TMElyralab/MuseTalk', ['musetalkV15/musetalk.json', 'musetalkV15/unet.pth'], base)
    hf('stabilityai/sd-vae-ft-mse',
       ['config.json', 'diffusion_pytorch_model.bin', 'diffusion_pytorch_model.safetensors'], base + '/sd-vae')
    hf('openai/whisper-tiny', ['config.json', 'pytorch_model.bin', 'preprocessor_config.json'], base + '/whisper')
    hf('yzd-v/DWPose', ['dw-ll_ucoco_384.pth'], base + '/dwpose')
    parse = ROOT / base / 'face-parse-bisent/79999_iter.pth'
    parse.parent.mkdir(parents=True, exist_ok=True)
    if not parse.is_file() or not parse.stat().st_size:
        import gdown
        partial = str(parse) + '.part'
        if not gdown.download(id='154JgKpzCPW82qINcVieuPH3fZ2e0P812', output=partial, quiet=False):
            raise RuntimeError('Failed downloading face parsing weights')
        Path(partial).replace(parse)
    url('https://download.pytorch.org/models/resnet18-5c106cde.pth', base + '/face-parse-bisent/resnet18-5c106cde.pth')
    url('https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth', 'gfpgan/weights/GFPGANv1.4.pth')
    url('https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth',
        'engines/MuseTalk/musetalk/utils/face_detection/detection/sfd/s3fd.pth')
    sys.path.insert(0, str(ROOT))
    # These libraries prepare their own auxiliary weights as well.
    from latentsync.utils.face_detector import FaceDetector
    FaceDetector()
    from restore_mouth_gfpgan import get_restorer
    get_restorer()
    print('All LatentSync and MuseTalk model downloads completed.', flush=True)

if __name__ == '__main__':
    main()
