#!/usr/bin/env python3
"""Replace SVulkan2 dynamic face culling with equivalent static pipelines.

Target: haosulab/sapien-vulkan-2 @ d764f627e784bcd4fc5a6271d211bea6744270b4
(the submodule pinned by SAPIEN 3.0.0b1).

No downloads or build commands are run. The default mode is a read-only check:
  python patch_svulkan2_static_cull.py /path/to/SAPIEN/3rd_party/sapien-vulkan-2
  python patch_svulkan2_static_cull.py /path/to/sapien-vulkan-2 --diff
  python patch_svulkan2_static_cull.py /path/to/sapien-vulkan-2 --apply

All six upstream file hashes and all exact replacements are checked before any
file is written. Modified/already-patched sources are rejected without writing.
The original pipeline remains BACK culling; G-buffer passes receive NONE, FRONT
and FRONT_AND_BACK variants with the identical layout/render pass. Non-G-buffer,
shadow, and point-shadow pipeline creation and binding are unchanged.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
from pathlib import Path
import sys


REVISION = "d764f627e784bcd4fc5a6271d211bea6744270b4"

# Git blob hashes from the pinned upstream tree, not checksums of a local checkout.
SOURCE_BLOBS = {
    "src/core/instance.cpp": "e50e8b26ea07e239cab05d100b6094fc01070860",
    "src/core/device.cpp": "0e64d425a8f380d7032f19b6510daf700848a3fd",
    "src/shader/gbuffer.cpp": "8e833747cb16d9131aea0ee8d9aa1b830df31234",
    "include/svulkan2/shader/shader_pack_instance.h": "ab69d97f3faa9cb48e71ed9c58104ec9ad67c513",
    "src/shader/shader_pack_instance.cpp": "fad83fe95f31ac1c39c08c42e709165a0f4a8248",
    "src/renderer/renderer.cpp": "2f5297eaa6a206a5b63c08033278f5e9d406c0bd"
}

# Ordered exact substitutions. Empty replacement strings remove obsolete code.
EDITS = [
    (
        "src/core/instance.cpp",
        '''    std::vector<const char *> all_required_extensions = {
        VK_EXT_EXTENDED_DYNAMIC_STATE_EXTENSION_NAME};''',
        '''    // Face culling is baked into the G-buffer pipeline variants.
    std::vector<const char *> all_required_extensions{};''',
    ),
    (
        "src/core/device.cpp",
        '''  vk::PhysicalDeviceExtendedDynamicStateFeaturesEXT extendedDynamicStateFeature;
  extendedDynamicStateFeature.setExtendedDynamicState(true);
  timelineSemaphoreFeatures.setPNext(&extendedDynamicStateFeature);

''',
        '''''',
    ),
    (
        "src/core/device.cpp",
        '''    extendedDynamicStateFeature.setPNext(&asFeature);''',
        '''    timelineSemaphoreFeatures.setPNext(&asFeature);''',
    ),
    (
        "src/core/device.cpp",
        '''  deviceExtensions.push_back(VK_EXT_EXTENDED_DYNAMIC_STATE_EXTENSION_NAME);

''',
        '''''',
    ),
    (
        "src/shader/gbuffer.cpp",
        '''  std::vector<vk::DynamicState> dynamicStates = {
      vk::DynamicState::eViewport, vk::DynamicState::eScissor, vk::DynamicState::eCullMode};''',
        '''  std::vector<vk::DynamicState> dynamicStates = {
      vk::DynamicState::eViewport, vk::DynamicState::eScissor};''',
    ),
    (
        "include/svulkan2/shader/shader_pack_instance.h",
        '''    vk::UniquePipeline pipeline{};
  };''',
        '''    // The original pipeline is the back-culling variant for G-buffer passes.
    // Other pass types continue to use this member directly.
    vk::UniquePipeline pipeline{};
    std::map<vk::CullModeFlagBits, vk::UniquePipeline> cullModePipelines{};

    vk::Pipeline getPipeline(vk::CullModeFlagBits cullMode) const {
      if (cullMode == vk::CullModeFlagBits::eBack) {
        return pipeline.get();
      }
      return cullModePipelines.at(cullMode).get();
    }
  };''',
    ),
    (
        "src/shader/shader_pack_instance.cpp",
        '''      res.pipeline = pass->createPipeline(
          device, res.layout.get(), res.renderPass.get(), vk::CullModeFlagBits::eBack,
          vk::FrontFace::eCounterClockwise, alpha, msaa, mDesc.specializationConstants);
''',
        '''      res.pipeline = pass->createPipeline(
          device, res.layout.get(), res.renderPass.get(), vk::CullModeFlagBits::eBack,
          vk::FrontFace::eCounterClockwise, alpha, msaa, mDesc.specializationConstants);
      if (std::dynamic_pointer_cast<GbufferPassParser>(pass)) {
        // Reuse the exact layout and render pass so descriptor sets remain
        // compatible when switching the static face-culling pipeline.
        for (auto cullMode : {vk::CullModeFlagBits::eNone, vk::CullModeFlagBits::eFront,
                              vk::CullModeFlagBits::eFrontAndBack}) {
          res.cullModePipelines.emplace(
              cullMode,
              pass->createPipeline(device, res.layout.get(), res.renderPass.get(), cullMode,
                                   vk::FrontFace::eCounterClockwise, alpha, msaa,
                                   mDesc.specializationConstants));
        }
      }
''',
    ),
    (
        "src/renderer/renderer.cpp",
        '''    if (auto gbufferPass = std::dynamic_pointer_cast<shader::GbufferPassParser>(pass)) {
      for (uint32_t i = 0; i < shapes[gbufferIndex].size(); ++i) {
        auto shape = shapes[gbufferIndex][i];
        uint32_t objectIndex = shapeObjectIndex[gbufferIndex][i];''',
        '''    if (auto gbufferPass = std::dynamic_pointer_cast<shader::GbufferPassParser>(pass)) {
      auto const &passResources =
          mShaderPackInstance->getNonShadowPassResources().at(pass_index);
      vk::Pipeline boundPipeline = passResources.pipeline.get();
      for (uint32_t i = 0; i < shapes[gbufferIndex].size(); ++i) {
        auto shape = shapes[gbufferIndex][i];
        vk::Pipeline pipeline = passResources.getPipeline(shape->material->getCullMode());
        if (pipeline != boundPipeline) {
          mRenderCommandBuffer->bindPipeline(vk::PipelineBindPoint::eGraphics, pipeline);
          boundPipeline = pipeline;
        }
        uint32_t objectIndex = shapeObjectIndex[gbufferIndex][i];''',
    ),
    (
        "src/renderer/renderer.cpp",
        '''        mRenderCommandBuffer->setCullMode(shape->material->getCullMode());
''',
        '''''',
    ),
]


def git_blob_hash(raw: bytes) -> str:
    header = b"blob " + str(len(raw)).encode("ascii") + b"\0"
    return hashlib.sha1(header + raw).hexdigest()


def patch_texts(originals: dict[str, str]) -> dict[str, str]:
    updated = dict(originals)
    for relative, old, new in EDITS:
        count = updated[relative].count(old)
        if count != 1:
            raise ValueError(
                f"{relative}: expected one exact edit match, found {count}; no files written"
            )
        updated[relative] = updated[relative].replace(old, new, 1)

    # Guard against accidentally removing only device discovery while retaining
    # an unsupported device feature, dynamic pipeline state, or draw command.
    forbidden = (
        "VK_EXT_EXTENDED_DYNAMIC_STATE_EXTENSION_NAME",
        "PhysicalDeviceExtendedDynamicStateFeaturesEXT",
        "extendedDynamicStateFeature",
        "vk::DynamicState::eCullMode",
        "->setCullMode(",
    )
    for relative, text in updated.items():
        for token in forbidden:
            if token in text:
                raise ValueError(f"{relative}: obsolete token remains: {token}")
    return updated


def load_sources(root: Path) -> tuple[dict[str, Path], dict[str, str]]:
    paths: dict[str, Path] = {}
    originals: dict[str, str] = {}
    for relative, expected in SOURCE_BLOBS.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"{relative}: resolves outside the requested source tree")
        raw = path.read_bytes()
        actual = git_blob_hash(raw)
        if actual != expected:
            raise ValueError(
                f"{relative}: source hash {actual} differs from pinned {expected}; "
                "wrong revision, modified source, or patch already applied; no files written"
            )
        paths[relative] = path
        originals[relative] = raw.decode("utf-8")
    return paths, originals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "svulkan2_root",
        type=Path,
        help="SVulkan2 submodule root, NOT the containing SAPIEN repository",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only preflight (default)")
    mode.add_argument("--diff", action="store_true", help="print a unified diff; do not write")
    mode.add_argument("--apply", action="store_true", help="apply after checking every source")
    args = parser.parse_args()

    try:
        root = args.svulkan2_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"not a source directory: {root}")
        paths, originals = load_sources(root)
        updated = patch_texts(originals)

        if args.diff:
            for relative in SOURCE_BLOBS:
                sys.stdout.writelines(
                    difflib.unified_diff(
                        originals[relative].splitlines(keepends=True),
                        updated[relative].splitlines(keepends=True),
                        fromfile="a/" + relative,
                        tofile="b/" + relative,
                    )
                )
        elif args.apply:
            # Check again immediately before mutation; every edit was already
            # prepared and validated above, including all four cull modes.
            for relative, path in paths.items():
                if git_blob_hash(path.read_bytes()) != SOURCE_BLOBS[relative]:
                    raise ValueError(f"{relative}: source changed during preflight")
            for relative, path in paths.items():
                path.write_bytes(updated[relative].encode("utf-8"))
            print(f"Applied {len(EDITS)} edits to {len(paths)} files in {root}")
        else:
            print(
                f"PASS: {len(paths)} files match {REVISION}; "
                f"{len(EDITS)} exact edits validated; no files written"
            )
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
