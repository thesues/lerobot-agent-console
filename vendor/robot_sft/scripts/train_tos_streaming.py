#!/usr/bin/env python3
"""Train ACT on a TOS dataset via StreamingTOSRobotDataset (no local download).

Standalone custom training loop — use when ``lerobot-train``'s ``make_dataset`` doesn't yet
recognize ``tos://`` URLs (the integration may not be upstreamed). Reuses lerobot's own
building blocks (policy, processors, optimizer, checkpoint format) so checkpoints stay
resumable and inference-loadable.

Usage::

    export TOS_ACCESS_KEY=... TOS_SECRET_KEY=*** TOS_ENDPOINT=... TOS_REGION=...
    python train_tos_streaming.py \\
        --dataset-url tos://bucket/prefix/name \\
        --output-dir /opt/data/robot_sft/runs/act_my_task \\
        --steps 10544 --batch-size 48 --num-workers 4 --save-freq 1318 \\
        --train-episodes "0,1,2,3,..."

Differences from ``lerobot-train``:
    - ``IterableDataset`` → buffer-shuffled, no ``EpisodeAwareSampler`` / ``drop_n_last_frames``
    - ``num_workers=1`` recommended (streaming shard count limits parallelism)
    - First step is slow (downloading ResNet18 weights + initial TOS data fetch);
      steady-state ~4–10 s/step for batch 48 on A30 (vs ~0.6 s/step for local).
      For small datasets (< 5 GB total), prefer downloading to local ``--dataset.root``.
"""
from __future__ import annotations

import argparse, logging, os, sys, time
import torch
from tqdm import tqdm

sys.path.insert(0, "/lerobot/src")
from lerobot.datasets import StreamingTOSRobotDataset
from lerobot.datasets.factory import resolve_delta_timestamps
from lerobot.policies import make_policy, make_pre_post_processors
from lerobot.optim.factory import make_optimizer_and_scheduler
from lerobot.common.train_utils import get_step_checkpoint_dir, save_checkpoint, update_last_checkpoint
from lerobot.configs.train import TrainPipelineConfig
from lerobot.configs.default import DatasetConfig
from lerobot.policies.act.configuration_act import ACTConfig
from lerobot.optim import AdamWConfig
from lerobot.utils.logging_utils import AverageMeter, MetricsTracker
from lerobot.utils.random_utils import set_seed
from lerobot.utils.utils import cycle, format_big_number

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s")
logger = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset-url", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--steps", type=int, required=True)
    p.add_argument("--batch-size", type=int, default=48)
    p.add_argument("--num-workers", type=int, default=1)
    p.add_argument("--save-freq", type=int, required=True)
    p.add_argument("--log-freq", type=int, default=100)
    p.add_argument("--seed", type=int, default=1000)
    p.add_argument("--lr", type=float, default=1e-5)
    p.add_argument("--weight-decay", type=float, default=1e-4)
    p.add_argument("--grad-clip-norm", type=float, default=10.0)
    p.add_argument("--train-episodes", type=str, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    set_seed(args.seed)
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Device: %s", device)

    policy_cfg = ACTConfig()
    logger.info("Loading metadata from %s ...", args.dataset_url)
    meta_ds = StreamingTOSRobotDataset(args.dataset_url, episodes=[0], return_uint8=True, buffer_size=1, shuffle=False)
    ds_meta = meta_ds.meta
    delta_timestamps = resolve_delta_timestamps(policy_cfg, ds_meta)
    logger.info("delta_timestamps: %s  camera_keys: %s", delta_timestamps, ds_meta.camera_keys)

    train_episodes = None
    if args.train_episodes:
        train_episodes = [int(x.strip()) for x in args.train_episodes.split(",")]

    logger.info("Creating StreamingTOSRobotDataset (training) ...")
    dataset = StreamingTOSRobotDataset(
        args.dataset_url, episodes=train_episodes, delta_timestamps=delta_timestamps,
        return_uint8=True, buffer_size=4096, shuffle=True, seed=args.seed,
    )
    logger.info("num_frames=%s num_episodes=%d fps=%d", format_big_number(dataset.num_frames), dataset.num_episodes, dataset.fps)

    logger.info("Creating ACT policy ...")
    policy = make_policy(cfg=policy_cfg, ds_meta=ds_meta)
    policy.to(device)
    logger.info("Policy params: %s", format_big_number(sum(p.numel() for p in policy.parameters())))

    preprocessor, postprocessor = make_pre_post_processors(policy_cfg=policy_cfg, dataset_stats=ds_meta.stats)

    dataset_cfg = DatasetConfig(repo_id="finish_sandwich", root="/tmp")
    cfg = TrainPipelineConfig(dataset=dataset_cfg)
    cfg.policy = policy_cfg
    cfg.optimizer = AdamWConfig(lr=args.lr, weight_decay=args.weight_decay, grad_clip_norm=args.grad_clip_norm)
    optimizer, lr_scheduler = make_optimizer_and_scheduler(cfg, policy)
    logger.info("Optimizer: %s LR=%.1e", type(optimizer).__name__, args.lr)

    dataloader = torch.utils.data.DataLoader(
        dataset, num_workers=args.num_workers, batch_size=args.batch_size,
        shuffle=False, pin_memory=True, drop_last=False,
        prefetch_factor=4 if args.num_workers > 0 else None,
        persistent_workers=args.num_workers > 0,
    )
    dl_iter = cycle(dataloader)

    train_metrics = {
        "loss": AverageMeter("loss", ":.3f", reduction="mean"),
        "grad_norm": AverageMeter("grdn", ":.3f"),
        "lr": AverageMeter("lr", ":0.1e"),
        "update_s": AverageMeter("updt_s", ":.3f", reduction="max"),
        "dataloading_s": AverageMeter("data_s", ":.3f", reduction="max"),
        "samples_per_s": AverageMeter("smp/s", ":.0f"),
        "gpu_mem_gb": AverageMeter("mem_gb", ":.2f", reduction="max"),
    }
    train_tracker = MetricsTracker(args.batch_size, dataset.num_frames, dataset.num_episodes, train_metrics, initial_step=0)

    from pathlib import Path
    args.output_dir = Path(args.output_dir)
    os.makedirs(str(args.output_dir), exist_ok=True)
    effective_bs = args.batch_size
    logger.info("Output dir: %s  Steps=%d Batch=%d Save=%d", str(args.output_dir), args.steps, args.batch_size, args.save_freq)

    policy.train()
    progbar = tqdm(total=args.steps, desc="Training", unit="step")

    for step in range(args.steps):
        start_time = time.perf_counter()
        batch = next(dl_iter)
        for cam_key in ds_meta.camera_keys:
            if cam_key in batch:
                if batch[cam_key].dtype == torch.uint8:
                    batch[cam_key] = batch[cam_key].to(dtype=torch.float32, device=device) / 255.0
                else:
                    batch[cam_key] = batch[cam_key].to(device=device)
        for k in list(batch.keys()):
            if isinstance(batch[k], torch.Tensor) and batch[k].device != device:
                batch[k] = batch[k].to(device=device)

        batch = preprocessor(batch)
        train_tracker.dataloading_s = time.perf_counter() - start_time

        optimizer.zero_grad()
        loss, _ = policy.forward(batch)
        loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(policy.parameters(), args.grad_clip_norm)
        optimizer.step()
        if lr_scheduler is not None:
            lr_scheduler.step()

        train_tracker.loss = loss.item()
        gn = grad_norm.item() if isinstance(grad_norm, torch.Tensor) else grad_norm
        train_tracker.grad_norm = gn
        train_tracker.lr = optimizer.param_groups[0]["lr"]
        train_tracker.update_s = time.perf_counter() - start_time
        train_tracker.gpu_mem_gb = torch.cuda.max_memory_allocated() / (1024**3)
        train_tracker.step()
        progbar.update(1)

        if args.log_freq > 0 and (step + 1) % args.log_freq == 0:
            step_time = train_tracker.update_s.avg + train_tracker.dataloading_s.avg
            if step_time > 0:
                train_tracker.samples_per_s = effective_bs / step_time
            logger.info(train_tracker)
            train_tracker.reset_averages()

        is_saving = (step + 1) % args.save_freq == 0 or (step + 1) == args.steps
        if is_saving:
            logger.info("Checkpoint after step %d", step + 1)
            checkpoint_dir = get_step_checkpoint_dir(args.output_dir, args.steps, step + 1)
            save_checkpoint(checkpoint_dir=checkpoint_dir, step=step + 1, cfg=cfg, policy=policy,
                            optimizer=optimizer, scheduler=lr_scheduler, preprocessor=preprocessor,
                            postprocessor=postprocessor, num_processes=1, batch_size=args.batch_size)
            update_last_checkpoint(checkpoint_dir)

    progbar.close()
    logger.info("Training complete. Output: %s", str(args.output_dir))


if __name__ == "__main__":
    main()
