"""Exercise installed PyTorch3D and CuRobo CUDA kernels on the GPU."""

import importlib.metadata as metadata
import json
from pathlib import Path
import traceback

report = {"passed": False}
try:
    import torch
    from pytorch3d.ops import knn_points

    assert torch.cuda.is_available()
    points = torch.tensor([[[0., 0., 0.], [2., 0., 0.]]], device="cuda")
    reference = torch.tensor([[[1., 0., 0.], [4., 0., 0.]]], device="cuda")
    nearest = knn_points(points, reference, K=1)
    torch.cuda.synchronize()
    assert torch.allclose(nearest.dists, torch.ones((1, 2, 1), device="cuda"))
    report["pytorch3d_knn_squared_distances"] = nearest.dists.cpu().tolist()

    from curobo.cuda_robot_model.cuda_robot_model import CudaRobotModel
    from curobo.types.base import TensorDeviceType
    from curobo.types.robot import RobotConfig
    from curobo.util_file import get_robot_path, join_path, load_yaml

    tensor_args = TensorDeviceType()
    config = load_yaml(join_path(get_robot_path(), "franka.yml"))["robot_cfg"]
    robot = RobotConfig.from_dict(config, tensor_args)
    model = CudaRobotModel(robot.kinematics)
    joints = torch.zeros((2, model.get_dof()), **tensor_args.as_torch_dict())
    state = model.get_state(joints)
    torch.cuda.synchronize()
    assert state.ee_position.is_cuda and torch.isfinite(state.ee_position).all()
    assert tuple(state.ee_position.shape) == (2, 3)
    assert torch.allclose(state.ee_position[0], state.ee_position[1])
    report.update(
        passed=True,
        gpu=torch.cuda.get_device_name(0),
        curobo_dof=model.get_dof(),
        curobo_ee_position=state.ee_position.cpu().tolist(),
        versions={name: metadata.version(name) for name in (
            "torch", "torchvision", "pytorch3d", "nvidia-curobo", "warp-lang")},
    )
except Exception as error:
    report.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())

destination = Path("/workspace/diagnostics/cuda_extensions/smoke.json")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2), flush=True)
raise SystemExit(0 if report["passed"] else 1)
