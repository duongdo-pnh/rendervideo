"""
Run FireRed-Image-Edit-1.1 once: remove the person from one image. Started by fun5b/plate.py in its own environment
(.venv-edit: diffusers 0.42 for QwenImageEditPlusPipeline; .venv-fun keeps the diffusers the video model was built
with), so this file imports nothing from the repo.

usage: engines/Fun5B/.venv-edit/bin/python -I fun5b/edit_worker.py --model-dir checkpoints/fun5b/firered-image-edit --in a.png --out b.png
       --prompt "..." [--steps 40] [--cfg 4.0] [--size 768x1376] [--seed 0]

The 16GB Qwen2.5-VL text encoder is put on the GPU first to encode the prompt and the " " negative, then freed, and
only then the GGUF transformer (Q5_K_M, 14.9GB) is loaded: both at once do not fit in 24GB. Measured on an RTX 3090:
~5 min of denoising at 40 steps, ~6.5 min with loading, 18.8GB peak.
"""

import argparse
import gc
import os
import time

import torch
from PIL import Image


@torch.no_grad()
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--gguf", default="FireRed-Image-Edit-1.1-Q5_K_M.gguf")
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--cfg", type=float, default=4.0)
    ap.add_argument("--size", default="768x1376")  # the model's own 9:16 size; plate.py resizes the result
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    width, height = map(int, a.size.split("x"))

    from diffusers import GGUFQuantizationConfig, QwenImageEditPlusPipeline, QwenImageTransformer2DModel
    from diffusers.pipelines.qwenimage.pipeline_qwenimage_edit_plus import CONDITION_IMAGE_SIZE, calculate_dimensions
    from transformers import Qwen2_5_VLForConditionalGeneration

    tic = time.time()
    te = Qwen2_5_VLForConditionalGeneration.from_pretrained(os.path.join(a.model_dir, "text_encoder"),
                                                            dtype=torch.bfloat16, device_map="cuda")
    pipe = QwenImageEditPlusPipeline.from_pretrained(a.model_dir, transformer=None, text_encoder=te,
                                                     torch_dtype=torch.bfloat16)
    image = Image.open(a.src).convert("RGB").resize((width, height), Image.LANCZOS)
    cw, ch = calculate_dimensions(CONDITION_IMAGE_SIZE, width / height)  # the encoder sees the image at 384x384 area
    cond = pipe.image_processor.resize(image, ch, cw)
    embeds = [[None if x is None else x.cpu() for x in pipe.encode_prompt(prompt=text, image=[cond], device="cuda")]
              for text in (a.prompt, " ")]
    pipe.text_encoder = None
    del te
    gc.collect()
    torch.cuda.empty_cache()

    transformer = QwenImageTransformer2DModel.from_single_file(
        os.path.join(a.model_dir, a.gguf), config=os.path.join(a.model_dir, "transformer"),
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16), torch_dtype=torch.bfloat16)
    pipe.register_modules(transformer=transformer)
    pipe.transformer.to("cuda")
    pipe.vae.to("cuda")
    cuda = lambda x: None if x is None else x.cuda()  # noqa: E731
    out = pipe(image=[image], prompt_embeds=cuda(embeds[0][0]), prompt_embeds_mask=cuda(embeds[0][1]),
               negative_prompt_embeds=cuda(embeds[1][0]), negative_prompt_embeds_mask=cuda(embeds[1][1]),
               true_cfg_scale=a.cfg, num_inference_steps=a.steps, height=height, width=width,
               generator=torch.Generator("cuda").manual_seed(a.seed)).images[0]
    out.save(a.out)
    print(f"edited in {time.time() - tic:.0f}s", flush=True)


if __name__ == "__main__":
    main()
