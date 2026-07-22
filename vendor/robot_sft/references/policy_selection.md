# Policy selection for lerobot SFT

Quick decision tree for picking a policy when training on robot demonstration data.

## Decision tree

```
User wants to train on a robot dataset
├── Has working HF token + gated repo access?
│   ├── Yes → pi05 (gemma_300m for ≤24GB GPU, gemma_2b for ≥40GB)
│   └── No → from-scratch policy (below)
│
├── Dataset size?
│   ├── ≤200 episodes → ACT (proven, fast, no gated deps)
│   ├── 200-1000 episodes → ACT or Diffusion Policy
│   └── >1000 episodes → ACT still works; Diffusion/VQ-BeT may generalize better
│
├── Task complexity?
│   ├── Single-mode (simple pick-place) → ACT or Gaussian Actor
│   ├── Multi-modal (multiple strategies) → Diffusion Policy or VQ-BeT
│   └── Long-horizon → TD-MPC (model-based, needs more compute)
│
└── GPU memory?
    ├── <12 GB → ACT (batch=8-16), Gaussian Actor
    ├── 12-24 GB → ACT, Diffusion, VQ-BeT (batch=16-64)
    └── ≥40 GB → pi05 gemma_2b, SmolVLA, full VLA policies
```

## Policy reference card

### From-scratch (no gated model dependencies)

| Policy | `--policy.type` | GPU fit | Typical batch | Steps/epoch (70k frames) | Checkpoint size |
|--------|----------------|---------|---------------|--------------------------|-----------------|
| ACT | `act` | 8-24 GB | 16-64 | ~1.1k-4.4k | ~0.5-2 GB |
| Diffusion Policy | `diffusion` | 12-24 GB | 32-128 | ~550-2.2k | ~0.3-1 GB |
| VQ-BeT | `vqbet` | 12-24 GB | 32-64 | ~1.1k-2.2k | ~0.5-1 GB |
| TD-MPC | `tdmpc` | 8-16 GB | 16-32 | ~2.2k-4.4k | ~1-3 GB |
| Gaussian Actor | `gaussian_actor` | 4-12 GB | 32-128 | ~550-2.2k | ~0.1-0.5 GB |
| Multi-Task DiT | `multi_task_dit` | 12-24 GB | 16-32 | ~2.2k-4.4k | ~1-2 GB |

### VLA (need HF token + gated license)

| Policy | `--policy.type` | Gated dep | GPU fit | Batch |
|--------|----------------|-----------|---------|-------|
| pi05 (300m) | `pi05` + `gemma_300m` | PaliGemma tokenizer | ≥12 GB | 4-8 |
| pi05 (2b) | `pi05` + `gemma_2b` | PaliGemma tokenizer | ≥40 GB | 1-4 |
| pi0 | `pi0` | PaliGemma backbone | ≥40 GB | 1-2 |
| pi0_fast | `pi0_fast` | PaliGemma backbone | ≥40 GB | 1-2 |
| SmolVLA | `smolvla` | SmolVLM base | ≥24 GB | 1-4 |

## Recommendation by dataset scale

| Episodes | Frames | Recommended | Fallback |
|----------|--------|-------------|----------|
| 10-50 | <50k | **ACT** (batch=8-16, 8 epochs) | Gaussian Actor |
| 50-200 | 50k-200k | **ACT** (batch=16-32, 5-8 epochs) | Diffusion Policy |
| 200-1k | 200k-1M | ACT or Diffusion (batch=32-64, 3-5 epochs) | pi05 (300m) |
| 1k+ | >1M | pi05/VLA or ACT (batch=64+, 2-3 epochs) | Diffusion Policy |

## pi05-specific notes

- `gemma_300m` variant: ~8-10 GB VRAM, batch=4-8 on A30 24GB
- `gemma_2b` variant: ~23 GB VRAM at batch=1, OOM on 24GB GPUs
- Always run preflight to verify memory before launching
- PaliGemma tokenizer (`google/paligemma-3b-pt-224`) is gated — must accept license on HF
- `hf auth login --token` has shell glob issues with `***` — use `export HF_TOKEN=...` instead

### fp8 (float8) training — TEMPORARILY DISABLED

fp8 is being reworked. The old **torchao** path (`--use_float8=true --float8_recipe=rowwise`)
was **removed from lerobot** and no longer exists — an online benchmark on pi05/H20 showed
plain **bf16 without compile was actually the fastest** (1.54 s/step steady, ~67 GB), while
torchao fp8 was *slower* and used *more* memory. Don't pass `--use_float8`; the flag is gone.

The replacement uses **NVIDIA TransformerEngine** (`te.LayerNormMLP` with delayed-scaling
HYBRID fp8 on the Gemma FFN layers), **scoped to pi0/pi05 only**. It is not wired into lerobot
yet, so for now `plan_training.py --float8` and `preflight.py --float8` **error out on purpose**
rather than emit dead flags. **Train in plain bf16** until the TE path lands and this section is
rewritten with the real `--policy.vlm_mlp_fp8_enable=true` flags.
