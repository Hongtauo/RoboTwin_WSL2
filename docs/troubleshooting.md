# 故障排查

以下问题来自这套固定版本的实际安装和测试。优先用仓库 [README](../README.md) 中的 `setup.ps1` / `workspace/bootstrap_robotwin.sh` 入口复现；容器内仓库只读挂载于 `/opt/robotwin-repro`，可写工作区是 `/workspace`。

## 先区分 CUDA、Vulkan 和上层任务

按顺序检查，而不是用 `nvidia-smi` 代替所有检查：

```bash
cd /workspace
python /workspace/check_base.py
vulkaninfo --summary
python /workspace/check_sapien.py physics
python /workspace/check_sapien.py render
python /workspace/check_cuda_extensions.py
python /workspace/check_robotwin_task.py --seed 0 --play-once
```

这些命令在已完成安装的容器、`robotwin` Python 3.10 环境中运行。最后一个命令会实际执行专家演示。报告保存在 `/workspace/diagnostics`；失败时保留异常和 traceback，先查最早失败的一层。

## `vulkaninfo` 找不到 DZN 或只看到软件设备

确认容器使用仓库 Compose 配置，具备 GPU 访问，并能读取 WSL 提供的 `/usr/lib/wsl` 和 `/dev/dxg`。本方案将整个 `/usr/lib/wsl` 只读挂入容器：只有 `lib` 子目录可能不足，因为 D3D12 还会使用 `drivers` 中的驱动文件。

```bash
test -e /dev/dxg
test -d /usr/lib/wsl/drivers
test -f /usr/share/vulkan/icd.d/dzn_icd.x86_64.json
test -f /usr/lib/x86_64-linux-gnu/libvulkan_dzn.so
printf '%s\n' "$VK_ICD_FILENAMES" "$VK_DRIVER_FILES"
vulkaninfo --summary
```

预期驱动文件变量指向 `dzn_icd.x86_64.json`，设备名称包含 `Microsoft Direct3D12` 及实际显卡名。`llvmpipe`/`lavapipe` 是另一条软件渲染路径，不是本仓库的验证结果。

[`install_dzn.sh`](../workspace/install_dzn.sh) 在容器内安装固定 Jammy PPA 包，并检查签名公钥指纹 `EB8B81E14DA65431D7504EA8F63F0F2B90935439`。依赖隔离和只读 WSL 挂载用于避免修改宿主 Mesa；不需要为了本方案在 WSL 中安装 Linux NVIDIA 内核驱动。`LD_LIBRARY_PATH` 加入 WSL 库时也应保留基础镜像已有的 NVIDIA 库路径。

## 能看到 GPU，SAPIEN 却报 `failed to find a rendering device`

这曾是原版 SAPIEN wheel 的实际错误。`vulkaninfo` 能识别显卡只证明 Vulkan loader/驱动工作，不保证满足渲染器要求。

当前已定位的阻断是原版 SVulkan2 无条件要求 `VK_EXT_extended_dynamic_state`，所用 DZN 不提供。需要对 SAPIEN 固定子模块应用 [`patch_svulkan2_static_cull.py`](../patches/patch_svulkan2_static_cull.py)，然后从源码构建并安装 `3.0.0b1+wsldzn`；只改 Python 或重装原始 wheel 无法解决该 C++ 层要求。

补丁替换的是 G-buffer 动态面剔除，具体语义见[兼容性文档](compatibility.md)。不要仅删掉扩展检测、保留 `setCullMode` 动态命令；那会留下实际渲染命令与设备能力不一致的问题。

## 已设置禁用光追，任务仍加载 `rt` shader

需要两个开关都生效：

```bash
export SAPIEN_DISABLE_RAY_TRACING=1
export ROBOTWIN_CAMERA_SHADER=default
```

第二个开关依赖已应用 [`patch_robotwin_raster.py`](../patches/patch_robotwin_raster.py)。它在创建渲染器前选择光栅 shader，并跳过 RoboTwin 的 RT 专用参数设置。检查任务报告的 `render_environment`，不要在渲染器创建后才修改环境变量。

`cuda_id: -1` 在本方案中是预期现象：固定版本禁用光追时跳过渲染设备 CUDA 映射。不要因此强制指定渲染设备为 `cuda:0`；先沿用自动选择，并用 CUDA 专用检查确认计算设备。

## RGB 正常，但 GPU 点云或 CUDA 图像接口失败

当前方案只验证 CPU 回读 RGB。原 RoboTwin 点云代码使用 `get_picture_cuda(...).torch()`，依赖图形与 CUDA 互通，本仓库尚未适配这一路径。任务测试保持 `depth: false`、`pointcloud: false`，分割输出也关闭。

PyTorch 和 CUDA 扩展能在 GPU 上计算，与渲染资源能否直接交给 PyTorch 是不同问题。暂时不要把原 GPU 点云调用当作已支持功能；深度和分割也需单独实现验证。相关边界见[兼容性文档](compatibility.md)。

## 构建缺少 `GL/gl.h`

实际错误位置是 GLFW 编译阶段：`fatal error: GL/gl.h: No such file or directory`。即使目标是无窗口 Vulkan 渲染，该固定构建仍会编译 GLFW，需要 OpenGL 开发头文件。

[`prepare_sapien_build.sh`](../workspace/prepare_sapien_build.sh) 已将 Ubuntu 的 `libgl-dev` 与所需 X11 开发包列入依赖。应补齐构建依赖后在原构建目录续编；这类编译错误本身不表示相机使用 OpenGL 渲染。

## pybind11 报 `fatal: invalid reference: smart_holder`

SAPIEN 3.0.0b1 两处 CMake 原来抓取 pybind11 的 `smart_holder` 分支；该分支已归档，旧声明会失败。直接换成当前 pybind11 最新版不能保证与这套旧绑定 API 兼容。

[`patch_pybind11_pin.py`](../patches/patch_pybind11_pin.py) 同时修改 `cmake/pybind11.cmake` 和 `pinocchio/cmake/pybind11.cmake`，改为校验官方仓库的固定归档：

| 项目 | 固定值 |
| --- | --- |
| 提交 | `68a11bb65b774df161c898204d08e85ac2cbac5f` |
| URL | [GitHub 官方源码归档](https://github.com/pybind/pybind11/archive/68a11bb65b774df161c898204d08e85ac2cbac5f.tar.gz) |
| 大小 | `874379` bytes |
| SHA256 | `cc03b749e1e69faf0ef20927d3e9caa793009f87ed5e0ca8b164c82e0a5bd081` |

该提交保留 `smart_holder.h` 和 `PYBIND11_USE_SMART_HOLDER_AS_DEFAULT`。其源码版本标签为 `3.0.0.dev1`，不能把它视为已经发布的 pybind11 3.0 API。构建脚本会在原生库已经存在时采用 `--pybind-only`，避免为绑定依赖问题重编全部原生库。

## Pinocchio 下载大量 `example-robot-data`

Pinocchio `v2.6.18` 的默认子模块更新会抓取 `models/example-robot-data`，可能产生远超本次需求的 Git 下载。本构建已设置 `BUILD_TESTING OFF`，不需要这些测试模型。

[`patch_pinocchio_submodules.py`](../patches/patch_pinocchio_submodules.py) 对准确的 FetchContent 声明增加：

```cmake
GIT_SUBMODULES cmake
GIT_SUBMODULES_RECURSE FALSE
```

这样保留必需的 jrl CMake 子模块，省去测试模型数据。脚本检查仓库、`v2.6.18` 和关闭测试的前提。修改声明不会自动清除已下载的旧缓存；重新运行前先确认没有其他构建正在使用它，不要把下载测试资产的进程误当作编译过程。

## TinyXML 下载失败，或 CUDA 编译了许多无关架构

[`patch_builddeps.py`](../patches/patch_builddeps.py) 把 TinyXML 2.6.2 改为[SourceForge 项目官方归档入口](https://downloads.sourceforge.net/project/tinyxml/tinyxml/2.6.2/tinyxml_2_6_2.tar.gz)，保留原 MD5 校验 `c1b864c96804a10526540c664ade67f0`。不要通过删除校验绕过网络问题。

同一补丁把 SAPIEN 和 Simsense 两个 target 的 `CUDA_ARCHITECTURES` 固定为 `89`。CUDA 扩展构建另外使用 `TORCH_CUDA_ARCH_LIST=8.9`，并以 `MAX_JOBS=2` 限制 Ninja 并行任务。仅设置环境变量不一定覆盖 CMake target 中硬编码的架构；其他显卡应重新选择架构并验证，不能直接复用本次 SM89 产物。部分上游 NVCC 命令还有自身线程参数，`MAX_JOBS` 不等于整个编译器只使用两个 CPU 线程。

## Windows 盘挂载中解压报 `Operation not permitted` / `EPERM`

实际问题出现在机器人资产发布阶段：Linux `os.link()` 在 Windows 盘经 Docker/DrvFs 挂载的目录上无法建立硬链接。ZIP 下载、SHA 校验成功并不代表发布阶段必然能使用硬链接。

[`fetch_robot_asset.py`](../workspace/fetch_robot_asset.py) 先尝试硬链接；对 `EPERM`、`EOPNOTSUPP`、`EXDEV`、`ENOTSUP` 改用 `O_CREAT | O_EXCL` 独占创建后复制。已有文件或符号链接不会被覆盖。这是资产文件的兼容处理，不需要把整个挂载目录改成宽松权限。

资产下载使用 `source-lock.json` 中固定的 Hugging Face revision 与 LFS SHA，拒绝与记录不一致的 API 元数据。归档校验大小 `219847741` bytes 和 SHA256，只提取 `aloha-agilex`，校验所选成员 CRC 并拒绝路径穿越、符号链接和冲突文件。最小资产只覆盖当前测试；其他任务缺资产时应获取其真实官方资产，不要用空文件代替。

## WSL 路径变成 Docker Desktop 临时挂载别名

整理仓库时发现，直接对完整 Windows 路径调用 `wslpath` 可能返回 `/mnt/wsl/docker-desktop-bind-mounts/...`。Docker inspect 也可能把实际正确的 bind mount 显示为另一个源路径；应结合容器内 `/proc/self/mountinfo` 和文件内容判断。

PowerShell 入口先转换 Windows 驱动器根目录，再拼接 checkout 相对路径，避开临时别名，并核对 Windows 与 WSL 读取的版本锁文件摘要。容器归属通过入口计算的 checkout 标签检查，不依赖 Docker Desktop 的反向路径字符串。移动 checkout 后应建立新容器；同名但缺少归属标签的旧容器会被拒绝。

## 只复制 SAPIEN wheel 后无法导入，或找不到 `.so`

本次自编译 wheel 不是可随意搬到其他环境的完整运行时封装。实际动态链接检查显示，绑定依赖构建安装前缀中的 `libsapien.so`、`libsimsense.so` 和 `libsvulkan2.so`：

```text
/opt/robotwin-build/SAPIEN/sapien_build/_sapien_install/lib
```

因此不能只复制 `sapien-3.0.0b1+wsldzn-*.whl`，再删除构建目录或换一台机器期待直接运行。仓库入口采用完整容器运行时快照保留这些原生依赖；从源码复现时也需保留安装前缀及相关库。用目标 Python 实际执行 `import sapien`，结合 `ldd` 和报告检查依赖；单独对扩展运行 `ldd` 不能替代 Python 包初始化后的完整导入测试。

另一个常见混用是基础镜像的 Python 3.11 与 `robotwin` 的 Python 3.10。wheel 的 `cp310` 标签要求匹配解释器；检查 `python --version`、`which python` 和 `python -m pip --version`，使用同一环境安装及运行。

## Git 报 `detected dubious ownership`

Windows 盘经 DrvFs 挂载后，目录所有者可能显示为 UID 1000，而安装进程是容器 root。全新底图中安装 Git 后，继续操作该 checkout 会被所有权检查拒绝；原成功环境曾为两个具体目录单独配置此项。

`bootstrap_robotwin.sh` 通过进程级 `GIT_CONFIG_COUNT` 及 `safe.directory` 仅信任 `/workspace/RoboTwin` 与其 `XPolicyLab` 子模块，子进程同样继承，不修改 Windows 或 WSL 宿主 Git 配置。已用只读挂载验证：禁用全局配置时检查失败，加入这两个目录后通过。不需要设置通配符 `safe.directory=*`。

## 补丁因源码哈希不符而拒绝执行

这是固定版本校验的设计结果。渲染、RoboTwin 和可选依赖补丁会检查上游 Git blob/SHA256 或准确声明片段，防止把旧版本替换应用到升级后或已被其他工具改写的源码。

先确认提交、子模块和工作区修改，再查看对应脚本的 `--help`、`--dry-run`、`--diff` 支持情况。并非所有脚本都能重复应用：静态 cull 补丁会拒绝已经修改的输入，另一些会识别准确的已补丁状态。不要删除校验或盲目反复运行 `--apply`；升级时需要重新定位调用点、审阅差异并重跑验证。

当前没有配置交互窗口，也没有完成原生 Linux 性能对照。窗口报错或耗时较长时，先维持已验证的 headless 配置；不能据此给出所有 WSL GUI 均不可行或固定性能损失比例的结论。
