"""
A faster Wan2.2 VAE encode with bitwise the same output, for the masked start video of each chunk.

That video is the known frames (the reference for the first chunk, 13 finished frames for the next ones) followed by
grey frames, all bitwise the same. The encoder runs over it group by group (1 frame, then 4 at a time), carrying a
cache of the last frames each causal conv saw. Split the encoder into its stages (conv1, the 4 down blocks, middle,
head): the output of the first stages depends only on the encoder's input and the caches of those stages. Inside the
run of identical grey groups, once a group leaves the caches of the first s stages bitwise unchanged, every later
group gives those stages the same input and caches again, so they return the same output and leave their caches
unchanged: their output is reused instead of computed again. The whole encoder never settles within a chunk (its deep
1-frame-per-group layers remember ~20 groups), but the high-resolution first stages, most of the cost, settle within
a few groups: 9.0 -> 3.0 s per 121-frame chunk on an RTX 3090, the latent bitwise equal to the stock encode.
Other inputs (the control video) have no such tail and are encoded as before. A 1-frame encode (the reference image,
encoded again for every chunk) is remembered while its input stays bitwise the same.
"""

import types

import torch


def install(vae):
    """Replace the encode of `vae` (an AutoencoderKLWan3_8) by this one."""
    vae.model.encode = types.MethodType(_encode, vae.model)
    vae.model._single_frame = None


def _bitwise_equal(a, b):
    """torch.equal treats -0.0 and +0.0 (both occur in the masked video) as equal; this does not."""
    if a is None or b is None:
        return a is b
    if a is b:
        return True
    if a.shape != b.shape or a.dtype != b.dtype or a.device != b.device:
        return False
    return torch.equal(a.contiguous().view(torch.uint8), b.contiguous().view(torch.uint8))


def _cached_conv(layer, x, feat_cache, feat_idx):
    """A cached causal conv exactly as Encoder3d.forward runs conv1 and the head conv."""
    from videox_fun.models.wan_vae3_8 import CACHE_T

    idx = feat_idx[0]
    cache_x = x[:, :, -CACHE_T:, :, :].clone()
    if cache_x.shape[2] < 2 and feat_cache[idx] is not None:
        cache_x = torch.cat([feat_cache[idx][:, :, -1, :, :].unsqueeze(2).to(cache_x.device), cache_x], dim=2)
    x = layer(x, feat_cache[idx])
    feat_cache[idx] = cache_x
    feat_idx[0] += 1
    return x


def _stages(encoder):
    """Encoder3d.forward (with a feature cache) split into its stages, op for op."""
    from videox_fun.models.wan_vae3_8 import CausalConv3d, ResidualBlock

    def middle(x, feat_cache, feat_idx):
        for layer in encoder.middle:
            x = layer(x, feat_cache, feat_idx) if isinstance(layer, ResidualBlock) else layer(x)
        return x

    def head(x, feat_cache, feat_idx):
        for layer in encoder.head:
            x = _cached_conv(layer, x, feat_cache, feat_idx) if isinstance(layer, CausalConv3d) else layer(x)
        return x

    def down(layer):
        return lambda x, feat_cache, feat_idx: layer(x, feat_cache, feat_idx)

    return ([lambda x, feat_cache, feat_idx: _cached_conv(encoder.conv1, x, feat_cache, feat_idx)]
            + [down(layer) for layer in encoder.downsamples] + [middle, head])


def _encode(self, x, scale):
    """AutoencoderKLWan2_2_.encode, reusing the output of settled stages over a constant tail (see above)."""
    from videox_fun.models.wan_vae3_8 import patchify

    if x.shape[2] == 1 and self._single_frame is not None and _bitwise_equal(self._single_frame[0], x):
        return self._single_frame[1].clone()
    single = x.clone() if x.shape[2] == 1 else None
    self.clear_cache()
    scale = [item.to(x.device, x.dtype) for item in scale]
    x = patchify(x, patch_size=2)
    count = 1 + (x.shape[2] - 1) // 4
    groups = [x[:, :, :1]] + [x[:, :, 1 + 4 * (i - 1) : 1 + 4 * i] for i in range(1, count)]
    tail = count - 1  # from group `tail` on, every input group is bitwise the same
    while tail > 1 and _bitwise_equal(groups[tail - 1], groups[-1]):
        tail -= 1
    stages = _stages(self.encoder)
    cache = self._enc_feat_map
    settled, kept, end = 0, [None] * len(stages), [0] * len(stages)  # the first `settled` stages reuse `kept`
    outs = []
    for i in range(count):
        feat_idx = [0]
        h = groups[i]
        for s, stage in enumerate(stages):
            if s < settled:
                h, feat_idx[0] = kept[s], end[s]
                continue
            start = feat_idx[0]
            before = cache[start:] if i >= tail else None
            h = stage(h, cache, feat_idx)
            end[s] = feat_idx[0]
            if i >= tail and s == settled and all(
                    _bitwise_equal(a, b) for a, b in zip(before[: end[s] - start], cache[start : end[s]])):
                settled, kept[s] = s + 1, h
        outs.append(h)
        if settled == len(stages):  # the whole encoder settled: the remaining groups are copies
            outs.extend([h] * (count - 1 - i))
            break
    out = torch.cat(outs, 2)
    del outs, groups, kept
    mu, log_var = self.conv1(out).chunk(2, dim=1)
    if isinstance(scale[0], torch.Tensor):
        mu = (mu - scale[0].view(1, self.z_dim, 1, 1, 1)) * scale[1].view(1, self.z_dim, 1, 1, 1)
    else:
        mu = (mu - scale[0]) * scale[1]
    x = torch.cat([mu, log_var], dim=1)
    self.clear_cache()
    if single is not None:
        self._single_frame = (single, x.clone())
    return x
