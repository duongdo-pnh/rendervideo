"""Shared, dependency-free inventory used by setup and desktop startup."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    'venv/bin/python', 'engines/MuseTalk/.venv/bin/python',
    'checkpoints/latentsync_unet.pt', 'checkpoints/whisper/tiny.pt',
    'checkpoints/sd-vae/config.json', 'checkpoints/sd-vae/diffusion_pytorch_model.bin',
    'checkpoints/auxiliary/models/buffalo_l/det_10g.onnx',
    'checkpoints/auxiliary/models/buffalo_l/2d106det.onnx',
    'gfpgan/weights/GFPGANv1.4.pth',
    'gfpgan/weights/detection_Resnet50_Final.pth',
    'gfpgan/weights/parsing_parsenet.pth',
    'engines/MuseTalk/models/musetalkV15/musetalk.json',
    'engines/MuseTalk/models/musetalkV15/unet.pth',
    'engines/MuseTalk/models/sd-vae/config.json',
    'engines/MuseTalk/models/sd-vae/diffusion_pytorch_model.bin',
    'engines/MuseTalk/models/whisper/config.json',
    'engines/MuseTalk/models/whisper/pytorch_model.bin',
    'engines/MuseTalk/models/whisper/preprocessor_config.json',
    'engines/MuseTalk/models/dwpose/dw-ll_ucoco_384.pth',
    'engines/MuseTalk/musetalk/utils/face_detection/detection/sfd/s3fd.pth',
    'engines/MuseTalk/models/face-parse-bisent/79999_iter.pth',
    'engines/MuseTalk/models/face-parse-bisent/resnet18-5c106cde.pth',
)

def missing_files(root=ROOT):
    missing = []
    for name in REQUIRED:
        try:
            if not (root / name).is_file() or (root / name).stat().st_size == 0:
                missing.append(name)
        except OSError:
            missing.append(name)
    return missing

def ready(root=ROOT):
    try:
        receipt = json.loads((root / '.environment-ready.json').read_text())
        return receipt.get('schema') == 1 and receipt.get('engines') == ['latentsync', 'musetalk'] and not missing_files(root)
    except (OSError, ValueError):
        return False

if __name__ == '__main__':
    raise SystemExit(0 if ready() else 1)
