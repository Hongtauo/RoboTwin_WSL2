#!/usr/bin/env python3
"""Check one real RoboTwin task, optionally execute its expert demonstration.

This checks stack_blocks_two with the aloha-agilex embodiment, seed 0 by
default. It follows collect_data.py's embodiment/config argument assembly,
but directly calls setup_demo/get_obs once by default. --play-once then calls
the real task's play_once exactly once, checks planning, joint limits and task
success, and saves its final RGB and block poses. No seed search, collector,
policy training or language-generation API is used.
Exceptions are saved in the report and re-raised with their full traceback.

Prerequisites: the patched SAPIEN build, applied patch_robotwin_raster.py,
official aloha-agilex assets with configured absolute-path CuRobo YAMLs,
and the normal RoboTwin runtime dependencies including CuRobo.
No packages or assets are installed/downloaded by this script.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import traceback


TASK_NAME = "stack_blocks_two"
EMBODIMENT = "aloha-agilex"
PINNED_ROBOTWIN = "ea8b21121ebb3cd201ff5b3fe361944ac94eda3f"


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def repository_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build_task_args(root: Path, output: Path, seed: int) -> dict:
    """Mirror the collector's single dual-arm embodiment argument assembly."""
    import yaml

    config_root = root / "env_cfg" / "task_config"
    with (config_root / "_config_template.yml").open(encoding="utf-8") as source:
        args = yaml.safe_load(source)
    with (config_root / "_embodiment_config.yml").open(encoding="utf-8") as source:
        embodiments = yaml.safe_load(source)
    if not isinstance(args, dict) or not isinstance(embodiments, dict):
        raise ValueError("unexpected task or embodiment YAML structure")

    # These overrides form the complete dzn_smoke configuration in memory.
    args.update(task_name=TASK_NAME, task_config="dzn_smoke", embodiment=[EMBODIMENT],
                embodiment_name=EMBODIMENT, episode_num=1, language_num=1,
                render_freq=0, save_data=False, collect_data=False, eval_mode=False,
                need_plan=True, use_seed=False, seed=seed, now_ep_num=0)
    args["domain_randomization"].update(
        random_background=False, cluttered_table=False, clean_background_rate=1,
        random_head_camera_dis=0, random_table_height=0, random_light=False,
        crazy_random_light_rate=0,
    )
    args["camera"].update(head_camera_type="D435", wrist_camera_type="D435",
                          collect_head_camera=True, collect_wrist_camera=False)
    args["data_type"].update(rgb=True, third_view=False, depth=False, pointcloud=False,
                             endpose=True, qpos=True, mesh_segmentation=False,
                             actor_segmentation=False)

    robot_file = embodiments[EMBODIMENT]["file_path"]
    if not isinstance(robot_file, str) or not robot_file:
        raise ValueError("missing aloha-agilex embodiment file_path")
    # Relative paths are interpreted from RoboTwin's root, as in the collector.
    args["left_robot_file"] = robot_file
    args["right_robot_file"] = robot_file
    args["dual_arm_embodied"] = True
    for side in ("left", "right"):
        with (Path(robot_file) / "config.yml").open(encoding="utf-8") as source:
            args[f"{side}_embodiment_config"] = yaml.safe_load(source)

    embodiment_dir = re.sub(r"[^A-Za-z0-9_]+", "_", EMBODIMENT).lower()
    args["save_path"] = str(output / "native_data" / args["task_config"] / TASK_NAME / embodiment_dir)
    return args


def write_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def json_native(value):
    """Preserve returned task info while converting NumPy scalars/arrays."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): json_native(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_native(item) for item in value]
    if hasattr(value, "tolist"):
        return json_native(value.tolist())
    if hasattr(value, "item"):
        return json_native(value.item())
    raise TypeError(f"unsupported task info type: {type(value).__name__}")


def task_motion_state(task) -> dict:
    state = {name: int(getattr(task, name)) for name in
             ("take_action_cnt", "left_cnt", "right_cnt") if hasattr(task, name)}
    for side in ("left", "right"):
        paths = getattr(task, f"{side}_joint_path", [])
        state[f"{side}_planned_segments"] = len(paths)
        state[f"{side}_planned_waypoints"] = sum(
            len(item["position"]) for item in paths
            if isinstance(item, dict) and item.get("position") is not None
        )
    state["blocks"] = {}
    for name in ("block1", "block2"):
        if hasattr(task, name):
            pose = getattr(task, name).get_pose()
            state["blocks"][name] = {
                "position": [float(value) for value in pose.p],
                "quaternion_wxyz": [float(value) for value in pose.q],
            }
    return state


def execute_play_once(task, report: dict, capture_final) -> None:
    """Execute once; instrument only move calls and preserve the original method."""
    play = report["play_once"] = {
        "completed": False, "move_calls": 0, "move_calls_returned_true": 0,
        "before": task_motion_state(task),
    }
    report["action_count_unit"] = "expert task.move groups returning True"
    original_move = task.move
    missing = object()
    original_override = task.__dict__.get("move", missing)

    def counted_move(*args, **kwargs):
        play["move_calls"] += 1
        result = original_move(*args, **kwargs)
        if result is True:
            play["move_calls_returned_true"] += 1
        return result

    report["phase"] = "play_once"
    task.move = counted_move
    try:
        play["return_info"] = json_native(task.play_once())
        play["completed"] = True
    finally:
        if original_override is missing:
            del task.move
        else:
            task.move = original_override
        play["plan_success"] = bool(task.plan_success)
        play["after"] = task_motion_state(task)
        report["actions_executed"] = play["move_calls_returned_true"]

    report["phase"] = "check_success"
    play["task_success"] = bool(task.check_success())
    report["phase"] = "check_planned_joints"
    legal, excess = task.planned_joints_legal()
    play["planned_joints_legal"] = bool(legal)
    play["max_joint_limit_excess_rad"] = float(excess)
    # Keep the final observation even when a completed attempt fails validation.
    capture_final()
    report["phase"] = "validate_play_once"
    if not play["plan_success"]:
        raise AssertionError("play_once failed: task.plan_success is False")
    if not play["task_success"]:
        raise AssertionError("play_once failed: task.check_success() is False")
    if not play["planned_joints_legal"]:
        raise AssertionError(f"play_once failed: planned joint limit excess is {excess} rad")
    if play["move_calls_returned_true"] == 0:
        raise AssertionError("play_once failed: no expert move group completed successfully")


def main() -> int:
    workspace = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robotwin-root", type=Path, default=workspace / "RoboTwin")
    parser.add_argument("--output-dir", type=Path,
                        default=workspace / "diagnostics" / "robotwin_task")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--play-once", action="store_true",
                        help="execute the real expert task once after the initial RGB check")
    parser.add_argument("--log-level", choices=("info", "debug", "trace"), default="info")
    args = parser.parse_args()

    root = args.robotwin_root.resolve(strict=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    run_name = f"{TASK_NAME}_seed{args.seed}_{started.strftime('%Y%m%dT%H%M%S%fZ')}_{os.getpid()}"
    image_path = output / f"{run_name}.png"
    final_image_path = output / f"{run_name}_final.png"
    report_path = output / f"{run_name}.json"
    report = {
        "check": "robotwin_play_once" if args.play_once else "robotwin_initial_observation",
        "passed": False, "play_once_requested": args.play_once,
        "task": TASK_NAME, "embodiment": EMBODIMENT, "seed": args.seed,
        "started_utc": started.isoformat(), "robotwin_root": str(root),
        "expected_repository_revision": PINNED_ROBOTWIN,
        "repository_revision": repository_revision(root),
        "python": platform.python_version(), "actions_executed": 0,
        "report": str(report_path), "phase": "preflight",
    }
    task = None
    previous_cwd = Path.cwd()
    start_time = time.perf_counter()
    try:
        # Set both controls before importing SAPIEN or any RoboTwin modules.
        os.environ["ROBOTWIN_CAMERA_SHADER"] = "default"
        os.environ["SAPIEN_DISABLE_RAY_TRACING"] = "1"
        report["render_environment"] = {
            name: os.environ.get(name) for name in (
                "ROBOTWIN_CAMERA_SHADER", "SAPIEN_DISABLE_RAY_TRACING",
                "VK_ICD_FILENAMES", "MESA_D3D12_DEFAULT_ADAPTER_NAME", "CUDA_VISIBLE_DEVICES",
            )
        }
        base_source = (root / "envs" / "_base_task.py").read_text(encoding="utf-8")
        if "ROBOTWIN_CAMERA_SHADER" not in base_source:
            raise RuntimeError("apply patch_robotwin_raster.py before running this task check")
        if report["repository_revision"] not in (None, PINNED_ROBOTWIN):
            raise RuntimeError("RoboTwin checkout is not the pinned revision")

        os.chdir(root)
        sys.path.insert(0, str(root))
        report["phase"] = "configure"
        task_args = build_task_args(root, output, args.seed)
        report["configuration"] = copy.deepcopy(task_args)

        report["phase"] = "import_dependencies"
        import numpy as np
        from PIL import Image
        import sapien
        sapien.set_log_level(args.log_level)
        sapien.render.set_log_level(args.log_level)
        report["versions"] = {name: package_version(name)
                              for name in ("sapien", "torch", "mplib", "numpy", "nvidia-curobo")}
        module = importlib.import_module(f"envs.{TASK_NAME}")
        task = getattr(module, TASK_NAME)()

        report["phase"] = "setup_demo"
        print(f"Initializing real {TASK_NAME}/{EMBODIMENT}, seed={args.seed}", flush=True)
        task.setup_demo(**task_args)
        report["physics_system"] = type(task.scene.physx_system).__name__
        device = task.scene.render_system.device
        report["render_device"] = {
            "alias": str(device), "name": device.name,
            "pci_string": device.pci_string, "cuda_id": device.cuda_id,
            "can_render": bool(device.can_render()),
        }

        report["phase"] = "get_obs"
        observation = task.get_obs()
        cameras = observation["observation"]
        rgb = np.asarray(cameras["head_camera"]["rgb"]).copy()
        report["camera_names"] = sorted(cameras)
        report["head_rgb"] = {
            "shape": list(rgb.shape), "dtype": str(rgb.dtype),
            "finite": bool(np.isfinite(rgb).all()),
            "min": int(rgb.min()), "max": int(rgb.max()),
            "mean": float(rgb.mean()), "std": float(rgb.std()),
            "mean_per_channel": rgb.mean(axis=(0, 1)).tolist(),
        }
        if rgb.shape != (240, 320, 3) or rgb.dtype != np.uint8:
            raise AssertionError(f"unexpected head RGB layout: {rgb.shape}/{rgb.dtype}")
        if not report["head_rgb"]["finite"] or report["head_rgb"]["max"] <= report["head_rgb"]["min"]:
            raise AssertionError("head RGB is non-finite or constant")

        report["phase"] = "save_png"
        Image.fromarray(rgb).save(image_path)
        with Image.open(image_path) as saved:
            saved.verify()
        report["image"] = str(image_path)
        report["initial_observation_passed"] = True
        # Persist the initial evidence before beginning a potentially long plan.
        if args.play_once:
            write_report(report_path, report)

            def capture_final():
                report["phase"] = "get_final_obs"
                final_observation = task.get_obs()
                final_cameras = final_observation["observation"]
                final_rgb = np.asarray(final_cameras["head_camera"]["rgb"]).copy()
                if final_rgb.shape != (240, 320, 3) or final_rgb.dtype != np.uint8:
                    raise AssertionError(f"unexpected final RGB layout: {final_rgb.shape}/{final_rgb.dtype}")
                finite = bool(np.isfinite(final_rgb).all())
                report["final_camera_names"] = sorted(final_cameras)
                report["final_head_rgb"] = {
                    "shape": list(final_rgb.shape), "dtype": str(final_rgb.dtype),
                    "finite": finite, "min": int(final_rgb.min()), "max": int(final_rgb.max()),
                    "mean": float(final_rgb.mean()), "std": float(final_rgb.std()),
                    "mean_per_channel": final_rgb.mean(axis=(0, 1)).tolist(),
                }
                if not finite or final_rgb.max() <= final_rgb.min():
                    raise AssertionError("final head RGB is non-finite or constant")
                report["phase"] = "save_final_png"
                Image.fromarray(final_rgb).save(final_image_path)
                with Image.open(final_image_path) as saved:
                    saved.verify()
                report["final_image"] = str(final_image_path)

            print("Executing one real stack_blocks_two expert demonstration", flush=True)
            execute_play_once(task, report, capture_final)
        report.update(passed=True, phase="complete", image=str(image_path),
                      elapsed_seconds=time.perf_counter() - start_time)
        write_report(report_path, report)
        result = {"passed": True, "image": str(image_path), "report": str(report_path)}
        if args.play_once:
            result["final_image"] = str(final_image_path)
            result["task_success"] = report["play_once"]["task_success"]
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except Exception as error:
        report.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc(),
                      elapsed_seconds=time.perf_counter() - start_time)
        write_report(report_path, report)
        print(f"Task check failed in {report['phase']}; report: {report_path}",
              file=sys.stderr, flush=True)
        raise
    finally:
        if task is not None:
            try:
                task.close_env()
            except Exception:
                traceback.print_exc()
        os.chdir(previous_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
