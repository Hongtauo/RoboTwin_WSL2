# 兼容性与验证范围

本仓库复现的是 **WSL2 + Docker Desktop 中的 RoboTwin 无窗口 GPU 光栅渲染**。它已经运行了一个真实任务的专家动作，但还不能视为原生 Linux 上所有 RoboTwin 功能的等价替代。

## 固定的验证基线

| 组件 | 本次验证版本 |
| --- | --- |
| 容器系统 | Ubuntu 22.04，Linux x86_64 |
| Python | 3.10.22，独立 `robotwin` Conda 环境 |
| RoboTwin | `ea8b21121ebb3cd201ff5b3fe361944ac94eda3f` |
| SAPIEN | `3.0.0b1`，源码提交 `d9031a351c39986e51bd0716bc3b51f9a05070d8`，本地版本标识 `+wsldzn` |
| SAPIEN Vulkan 子模块 | `d764f627e784bcd4fc5a6271d211bea6744270b4` |
| Vulkan 驱动 | Mesa DZN，`mesa-vulkan-drivers=25.0.7~kisak3~j` |
| PyTorch / CUDA | `2.4.1+cu121` / 12.1 |
| torchvision / NumPy | `0.19.1+cu121` / `1.26.4` |
| MPLib | `0.2.1` |
| cuRobo / PyTorch3D | `0.7.8` / `0.7.8` |
| 验证显卡 | NVIDIA GeForce RTX 4070 Ti SUPER，CUDA 架构 `8.9` |

这是一个已验证组合，不代表任意 WSL、Windows 驱动、显卡和 Mesa 版本都已经兼容。补丁带有固定源码或片段校验，升级版本时需要重新审阅。

## 与原生 Linux 的区别

以下原生 Linux 一栏指相同 RoboTwin/SAPIEN 版本在具备所需能力的原生 NVIDIA Vulkan 驱动上的通常路径，不是一组已完成的 Linux 对照实验。

| 能力 | 原生 Linux 路径 | 本仓库 WSL2 配置 |
| --- | --- | --- |
| 默认刚体物理 | `PhysxCpuSystem` | 同样是 `PhysxCpuSystem`，没有改成另一种物理后端 |
| GPU 图形接口 | Vulkan → 原生 NVIDIA Linux 驱动 | Vulkan → Mesa DZN → D3D12 → Windows GPU 驱动 |
| 相机渲染 | RoboTwin 默认选择 `rt` 光追 shader | 选择 `default` 光栅 shader，已验证 GPU 渲染 |
| CUDA 运算 | PyTorch、cuRobo 等各自调用 CUDA | PyTorch CUDA、cuRobo 和 PyTorch3D 的指定运算已通过 |
| 图形与 CUDA 互通 | 可使用兼容驱动提供的互通路径 | 当前配置绕过渲染设备的 CUDA 映射，不能直接照搬原 GPU 图像/点云路径 |
| RGB 获取 | 取决于上层调用接口 | 经 `get_picture("Color")` 回读到 CPU，保存 PNG |
| 深度、分割、点云 | 上游提供相应接口 | 本次任务关闭这些输出，没有完成验证或适配 |
| 交互窗口 | 原生桌面/显示服务提供窗口环境 | 只配置 headless；没有配置或验证 GUI |
| 维护 | 通常可以沿用上游支持的驱动和安装路径 | 需要维护 DZN 与固定版本源码补丁 |

这里的 GPU 使用包含两个独立路径：CUDA 计算能运行，并不能证明 Vulkan 满足渲染器要求；Vulkan 成功出图，也不能证明 CUDA 图形互通可用。容器提供依赖隔离，不会把 WSL2 的 GPU 接口变成原生 Linux NVIDIA 驱动接口。

### CPU 物理是原版默认行为

固定版本 RoboTwin 的 `envs/_base_task.py` 调用 `sapien.Engine().create_scene(scene_config)`。SAPIEN 的 `Engine.create_scene()` 返回默认 `Scene()`，后者创建 `PhysxCpuSystem` 和 `RenderSystem`。这是[原版 SAPIEN 的场景构造代码](https://github.com/haosulab/SAPIEN/blob/d9031a351c39986e51bd0716bc3b51f9a05070d8/python/py_package/wrapper/scene.py)，并非 WSL 补丁引入的降级。

任务报告记录的 `physics_system: "PhysxCpuSystem"` 与该默认实现一致。`SAPIEN_CUDA=ON` 保留 CUDA 相关编译能力，不会自动把默认场景切换为 GPU PhysX。本仓库没有验证 GPU PhysX 场景。

## 为什么只关闭光追还不够

在当前驱动上，`vulkaninfo` 能枚举到 `Microsoft Direct3D12 (NVIDIA GeForce RTX 4070 Ti SUPER)`，报告 Vulkan API `1.2.305`。但是，原版 SVulkan2 即使不启用光追，仍把 `VK_EXT_extended_dynamic_state` 当作必需扩展；所用 DZN 没有暴露该扩展，设备因而被筛除。原始行为可见[固定提交的设备枚举源码](https://github.com/haosulab/sapien-vulkan-2/blob/d764f627e784bcd4fc5a6271d211bea6744270b4/src/core/instance.cpp)。

[`patch_svulkan2_static_cull.py`](../patches/patch_svulkan2_static_cull.py) 修改六个 C++/头文件：移除该扩展和 feature 的硬性请求，并用静态 pipeline 替代 G-buffer 中的动态面剔除命令。保留原来的 BACK pipeline，同时建立 NONE、FRONT、FRONT_AND_BACK 变体，根据材质原有剔除模式选择。

这保留了这四种面剔除模式的选择语义，没有简单地把所有物体改为双面。pipeline layout、render pass 以及非 G-buffer、阴影、点光源阴影的创建和绑定路径保持原实现。已运行任务验证了这条渲染路径，但没有对所有材质、shader 和场景做穷举视觉回归。

运行时还需要同时设置：

```bash
export SAPIEN_DISABLE_RAY_TRACING=1
export ROBOTWIN_CAMERA_SHADER=default
```

第一个变量由 SAPIEN/SVulkan2 读取；第二个由 [`patch_robotwin_raster.py`](../patches/patch_robotwin_raster.py) 给 RoboTwin 增加的入口读取。原版 RoboTwin 会显式选择 `rt`，仅更换 SAPIEN 默认 shader 不够。环境变量必须在启动 Python、创建渲染器之前生效。

光栅输出使用真实 GPU，但与原版光追输出在光照、反射、阴影等方面可能不同。任务 PNG 证明出图和任务执行成功，不能作为光追画质等价的证据。当前没有原生 Linux 对照基准，不提供速度提升或下降比例。

## CUDA 图形互通与相机输出的边界

在这个固定的 SVulkan2 版本中，`SAPIEN_DISABLE_RAY_TRACING=1` 还会跳过渲染设备的 CUDA ID 匹配，因此报告中的 `render_device.cuda_id` 为 `-1`。这是渲染设备映射的状态，不能据此判定 PyTorch 没有用 GPU。

当前 RGB 路径使用 `camera.get_picture("Color")`，把 GPU 渲染结果回读为 CPU 数组。RoboTwin 原来的点云函数调用 `get_picture_cuda(...).torch()`；这条互通路径没有在当前配置中适配，不能仅打开 `pointcloud: true` 就认为可用。DZN 报告某些外部内存/信号量扩展，也不等于已经验证 CUDA 与图形资源共享。

深度与分割有不同的读取路径，不能由 RGB 成功推出它们成功；本次测试明确关闭深度、分割和点云。GUI 同样未配：这是本仓库的验证范围，不是断言 WSL2 绝对不能显示窗口。

## 已实际完成的验证

[`check_sapien.py`](../workspace/check_sapien.py) 分开检查 CPU 刚体落下和 GPU 光栅出图；[`check_cuda_extensions.py`](../workspace/check_cuda_extensions.py) 检查 PyTorch3D CUDA KNN 及 cuRobo CUDA 正向运动学。

[`check_robotwin_task.py`](../workspace/check_robotwin_task.py) 使用真实 `stack_blocks_two`、`aloha-agilex` 和 seed `0`，执行一次 `play_once()`，报告结果包括：

| 项目 | 结果 |
| --- | --- |
| 初始化与头部相机 RGB | 通过，RGB 为 `240 × 320 × 3` |
| 专家动作 | 8 次 move 调用均返回成功 |
| 规划结果 | `plan_success: true` |
| 规划关节范围检查 | 通过，最大越界量为 `0.0` rad |
| 任务自身成功判据 | `task_success: true` |

验证只使用所需的最小机器人资产、无杂物/随机背景配置；没有遍历全部任务或随机种子，没有验证批量数据采集、策略训练、策略评测、语言生成服务或所有机器人。cuRobo 的 CUDA 运算通过也不意味着该次专家演示的每个规划步骤都走 GPU。

复现入口与运行顺序见 [README](../README.md)，具体错误处理见[故障排查](troubleshooting.md)。
