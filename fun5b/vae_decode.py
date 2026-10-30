"""
Wan2.2 VAE (VideoX-Fun's wan_vae3_8) decode straight to the uint8 frames render() keeps, bitwise identical to
Wan2_2FunControlPipeline.decode_latents followed by render()'s (x * 255).round().astype(uint8), with less GPU memory
and time:
- one latent frame at a time with one causal cache updated in place (as Wan's own decoder does).
  AutoencoderKLWan2_2_.decode copies the cache list per frame (for gradient checkpointing), so the previous frame's
  whole cache (2.5GB at 704x1056) stays alive through the next frame, then holds every decoded frame on the GPU 3-4
  times over (list, cat, unpatchify, stack, /2+0.5) and converts to uint8 on the CPU in float32 (~5s per chunk);
- each frame is converted to uint8 on the GPU and only that (2.2MB a frame) is copied to the host;
- causal: frames past `stop` (the padding to 4k+1 frames) are never computed.
lean_vae() also patches VideoX-Fun's building blocks to the same arithmetic with fewer full-size temporaries.
Every change was checked bitwise on real latents and frames (decode and encode).
"""

import numpy as np
import torch


def lean_vae():
    """Same values, fewer copies: the convolution, normalisation and activation kernels see identical inputs."""
    import videox_fun.models.wan_vae3_8 as wv

    if getattr(wv, "_lean", False):
        return

    def causal_conv(self, x, cache_x=None):
        # torch.cat([cache, x]) then F.pad made two full copies of the input; one zero-bordered buffer holds the same
        # values in the same (contiguous) layout.
        pw, ph, pt = self._padding[0], self._padding[2], self._padding[4]
        if pw == 0 and ph == 0 and pt == 0:
            return torch.nn.Conv3d.forward(self, x)
        ct = 0
        if cache_x is not None and pt > 0:
            cache_x = cache_x.to(x.device)
            ct = cache_x.shape[2]
        tz = pt - ct
        b, c, t, h, w = x.shape
        buf = x.new_empty((b, c, tz + ct + t, h + 2 * ph, w + 2 * pw))
        if tz:
            buf[:, :, :tz].zero_()
        if ph:
            buf[:, :, tz:, :ph].zero_()
            buf[:, :, tz:, -ph:].zero_()
        if pw:
            buf[:, :, tz:, ph : ph + h, :pw].zero_()
            buf[:, :, tz:, ph : ph + h, -pw:].zero_()
        if ct:
            buf[:, :, tz : tz + ct, ph : ph + h, pw : pw + w] = cache_x
        buf[:, :, tz + ct :, ph : ph + h, pw : pw + w] = x
        return torch.nn.Conv3d.forward(self, buf)

    def upsample(self, x):
        # nearest-exact only copies values: in bf16 directly, not through a float32 copy twice the size
        return torch.nn.Upsample.forward(self, x)

    def rms_norm(self, x):
        # F.normalize(x) * scale * gamma + bias: the same ops in the same order, in place after the division
        n = torch.linalg.vector_norm(x, 2, dim=(1 if self.channel_first else -1), keepdim=True).clamp_min_(1e-12)
        y = x / n.expand_as(x)
        y.mul_(self.scale).mul_(self.gamma)
        if not isinstance(self.bias, float):
            y.add_(self.bias)
        return y

    def up_residual(self, x, feat_cache=None, feat_idx=[0], first_chunk=False):
        # Up_ResidualBlock.forward without x.clone(): no module of the main path writes into its input
        x_main = x
        for module in self.upsamples:
            x_main = module(x_main, feat_cache, feat_idx)
        if self.avg_shortcut is not None:
            return x_main + self.avg_shortcut(x, first_chunk)
        return x_main

    wv.CausalConv3d.forward = causal_conv
    wv.Upsample.forward = upsample
    wv.RMS_norm.forward = rms_norm
    wv.Up_ResidualBlock.forward = up_residual
    wv._lean = True


@torch.no_grad()
def decode_frames(vae, latents, stop=None):
    """The first `stop` (default: all) uint8 frames the video `latents` (1, C, T, h, w) decodes to, (stop, H, W, 3)."""
    from videox_fun.models.wan_vae3_8 import unpatchify

    model = vae.model
    z = latents.to(vae.dtype)
    assert z.shape[0] == 1
    mean, inv_std = (s.to(z.device, z.dtype) for s in vae.scale)
    z = z / inv_std.view(1, model.z_dim, 1, 1, 1) + mean.view(1, model.z_dim, 1, 1, 1)  # AutoencoderKLWan2_2_.decode
    total = 1 + vae.config.temporal_compression_ratio * (z.shape[2] - 1)
    stop = total if stop is None else min(stop, total)
    frames = None
    done = 0
    model.clear_cache()
    try:
        x = model.conv2(z)
        feat_map = model._feat_map
        for i in range(z.shape[2]):
            if done >= stop:  # causal: the latents after the last kept frame do not change it
                break
            out = model.decoder(x[:, :, i : i + 1], feat_cache=feat_map, feat_idx=[0], first_chunk=i == 0)
            out = out[:, :, : stop - done]
            video = unpatchify(out, patch_size=2).clamp_(-1, 1)  # AutoencoderKLWan3_8._decode
            video = (video / 2 + 0.5).clamp(0, 1)  # Wan2_2FunControlPipeline.decode_latents
            rgb = (video[0].permute(1, 2, 3, 0).float() * 255).round().to(torch.uint8).cpu().numpy()  # render()
            if frames is None:
                frames = np.empty((stop, *rgb.shape[1:]), np.uint8)
            frames[done : done + len(rgb)] = rgb
            done += len(rgb)
            del out, video
    finally:
        model.clear_cache()
    return frames
