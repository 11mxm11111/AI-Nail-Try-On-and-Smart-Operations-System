#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import requests
import torch
from tqdm import tqdm
from pathlib import Path
from loguru import logger
import hashlib

# Configure logging
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
           "<level>{level: <8}</level> | "
           "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
           "<level>{message}</level>"
)
logger.add("model_download.log", rotation="10 MB")

# Model configurations
MODELS = {
    "sam_vit_h": {
        "filename": "sam_vit_h_4b8939.pth",
        "url": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth",
        "sha256": "placeholder",
        "size": int(2.4 * 1024 * 1024 * 1024),
        "description": "Segment Anything Model (SAM) - 用于精确的指甲区域分割"
    },
    "grounding_dino": {
        "filename": "groundingdino_swint_ogc.pth",
        "url": "https://github.com/IDEA-Research/GroundingDINO/releases/download/v0.1.0-alpha/groundingdino_swint_ogc.pth",
        "sha256": "placeholder",
        "size": int(1.2 * 1024 * 1024 * 1024),
        "description": "Grounding DINO - 用于手部检测和定位"
    }
}


def calculate_sha256(filepath):
    """Calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def download_file(url, filename, expected_size):
    """Download a file with progress bar."""
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        if total_size == 0:
            logger.warning(f"无法获取文件大小: {filename}")
            total_size = expected_size

        with open(filename, "wb") as f, tqdm(
            desc=str(filename),
            total=total_size,
            unit="iB",
            unit_scale=True,
            unit_divisor=1024,
        ) as pbar:
            for data in response.iter_content(chunk_size=1024 * 1024):
                if data:
                    size = f.write(data)
                    pbar.update(size)

        actual_size = os.path.getsize(filename)
        logger.info(f"下载完成: {filename}, 文件大小: {actual_size / 1024**3:.2f}GB")

        if actual_size < 1024 * 1024:
            logger.error(f"文件过小，可能下载失败: {filename}")
            return False

        return True

    except Exception as e:
        logger.error(f"下载失败 {filename}: {str(e)}")
        if os.path.exists(filename):
            os.remove(filename)
        return False


def download_models():
    """Download all required models."""
    models_dir = Path("models")
    models_dir.mkdir(exist_ok=True)

    cuda_available = torch.cuda.is_available()
    logger.info(f"CUDA可用: {cuda_available}")

    if cuda_available:
        logger.info(f"CUDA设备: {torch.cuda.get_device_name(0)}")
        logger.info(f"CUDA版本: {torch.version.cuda}")
        logger.info(
            f"GPU内存: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f}GB"
        )

    for model_name, model_info in MODELS.items():
        model_path = models_dir / model_info["filename"]

        logger.info(f"\n准备下载: {model_name}")
        logger.info(f"描述: {model_info['description']}")
        logger.info(f"大小: {model_info['size'] / 1024**3:.1f}GB")

        if model_path.exists():
            actual_size = os.path.getsize(model_path)
            if actual_size > 1024 * 1024:
                logger.info(f"{model_name} 已存在，跳过下载: {model_path}")
                continue
            else:
                logger.warning(f"{model_name} 文件过小，重新下载...")
                os.remove(model_path)

        logger.info(f"开始下载 {model_name}...")

        success = download_file(
            model_info["url"],
            model_path,
            model_info["size"]
        )

        if success:
            logger.success(f"成功下载 {model_name}")
        else:
            logger.error(f"下载 {model_name} 失败")
            return False

    logger.success("\n所有模型下载完成！")
    logger.info(f"模型文件保存在: {models_dir.absolute()}")
    return True


if __name__ == "__main__":
    try:
        print("\n=== 指甲颜色预览系统 - 模型下载工具 ===\n")
        print("本工具将下载以下模型：")

        for model_name, model_info in MODELS.items():
            print(f"\n{model_name}:")
            print(f"- 描述: {model_info['description']}")
            print(f"- 大小: {model_info['size'] / 1024**3:.1f}GB")

        print("\n开始下载...\n")

        success = download_models()
        if not success:
            logger.error("模型下载失败，请检查日志了解详情")
            sys.exit(1)

    except Exception:
        logger.exception("下载过程中发生意外错误")
        sys.exit(1)