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

### fp8 (float8) training — pi05 supported

pi05 can train with fp8 matmuls via torchao. Its Gemma FFN layers
(`...layers.N.mlp.gate_proj/up_proj/down_proj`) are swapped to fp8; attention q/k/v/o
projections and the action heads stay bf16 (safer numerics). Enable with:

```bash
lerobot-train --policy.type=pi05 --policy.dtype=bfloat16 \
  --use_float8=true --float8_recipe=rowwise \
  --dataset.repo_id=<...> ...
```

- **Flag is `--use_float8=true`** (with optional `--float8_recipe=rowwise`). Keep
  `--policy.dtype=bfloat16` — fp8 composes with bf16 autocast; master weights stay bf16.
- **Hopper/Ada GPU only** — needs fp8 tensor cores: H20 / H100 / L40S (sm_89/90+). On an
  **A30 (Ampere, sm_80) it errors out** ("fp8 training needs compute capability >= 8.9") —
  do NOT pass `--use_float8` on the A30 nodes; train those in plain bf16.
- Needs `torchao` in the image (baked into the current lerobot image via `[fp8]`).
- Correctness is fine without `torch.compile`, but the throughput win is small until compile
  is on — treat fp8 as "runs correctly now, faster once compiled".
- On a startup log line `fp8: converted N nn.Linear layer(s)`, N>0 confirms it took effect;
  `0 layers converted` means fp8 had no effect (wrong hardware or a frozen backbone).
