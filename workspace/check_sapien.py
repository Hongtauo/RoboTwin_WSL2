"""Check CPU physics separately from SAPIEN's raster renderer."""

import argparse
import json
import os
from pathlib import Path
import traceback

os.environ['SAPIEN_DISABLE_RAY_TRACING'] = '1'

import numpy as np
import sapien


def run(mode):
    physics = sapien.physx.PhysxCpuSystem()
    systems = [physics]
    if mode == 'render':
        sapien.render.set_camera_shader_dir('default')
        sapien.render.set_viewer_shader_dir('default')
        print('Creating Vulkan raster renderer', flush=True)
        systems.append(sapien.render.RenderSystem())
    scene = sapien.Scene(systems=systems)
    scene.set_timestep(1 / 120)
    scene.add_ground(0, render=mode == 'render')
    builder = scene.create_actor_builder()
    builder.add_box_collision(half_size=[0.2, 0.2, 0.2])
    if mode == 'render':
        builder.add_box_visual(half_size=[0.2, 0.2, 0.2], material=[1, 0, 0])
    box = builder.build(name='red_box')
    box.set_pose(sapien.Pose(p=[0, 0, 1]))
    for _ in range(240):
        scene.step()
    height = float(box.get_pose().p[2])
    result = {'mode': mode, 'physics': type(physics).__name__, 'box_final_z': height}
    assert 0.15 < height < 0.3, 'Box did not settle on the ground'
    if mode == 'render':
        from PIL import Image

        scene.set_ambient_light([0.5, 0.5, 0.5])
        scene.add_directional_light([0, 1, -1], [0.5, 0.5, 0.5])
        camera = scene.add_camera('smoke', 128, 128, 1.0, 0.01, 10.0)
        camera.entity.set_pose(sapien.Pose(p=[-3, 0, 0.45]))
        scene.update_render()
        camera.take_picture()
        rgba = np.asarray(camera.get_picture('Color')).copy()
        rgb = (rgba[..., :3].clip(0, 1) * 255).astype(np.uint8)
        output = Path('/workspace/diagnostics/sapien_raster.png')
        Image.fromarray(rgb).save(output)
        red = ((rgb[..., 0] > 80) & (rgb[..., 1] < rgb[..., 0] * 0.6)
               & (rgb[..., 2] < rgb[..., 0] * 0.6)).sum()
        result.update(shape=list(rgba.shape), finite=bool(np.isfinite(rgba).all()),
                      rgb_std=float(rgb.std()), red_pixels=int(red), output=str(output))
        assert result['finite'] and red > 10, 'Invalid or unexpected rendered image'
    result['passed'] = True
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['physics', 'render'])
    mode = parser.parse_args().mode
    try:
        report = run(mode)
    except Exception as error:
        report = {'mode': mode, 'passed': False, 'error': str(error),
                  'traceback': traceback.format_exc()}
    destination = Path(f'/workspace/diagnostics/sapien_{mode}.json')
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)
    raise SystemExit(0 if report['passed'] else 1)
