#!/usr/bin/env python3
"""Minecraft Mod 分类 & 整理 — 统一入口.

流程:
  1. 弹出文件夹选择 → 选定 mods 目录
  2. classifier 生成 mod_classification.csv
  3. organizer 按端类型拷贝 JAR 到 output/{client,server,both}/
"""

import tkinter as tk
from tkinter import filedialog
from pathlib import Path

from src.classifier import run_classification
from src.organizer import run_organizer


def choose_mods_dir():
    """弹出文件夹选择对话框."""
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    dir_path = filedialog.askdirectory(
        title="请选择 mods 文件夹",
        initialdir=str(Path.home()),
    )
    root.destroy()
    if not dir_path:
        print("未选择目录，已取消。")
        return None
    return Path(dir_path)


def main():
    print("=" * 60)
    print("Minecraft Mod 分类 & 整理工具")
    print("=" * 60)

    # 1. 选择 mods 目录
    print("\n请在弹出的文件夹选择窗口中选定 mods 目录...")
    mods_dir = choose_mods_dir()
    if mods_dir is None:
        return

    print(f"已选择: {mods_dir}\n")
    if not mods_dir.exists():
        print(f"[ERR] 目录不存在: {mods_dir}")
        return

    # 2. 运行分类 → 生成 mod_classification.csv
    print("\n" + "=" * 60)
    print("步骤 1/2: 生成模组分类表")
    print("=" * 60)
    results = run_classification(mods_dir)
    if not results:
        print("[ERR] 分类失败，终止")
        return

    # 3. 运行整理 → 拷贝到 output/
    print("\n" + "=" * 60)
    print("步骤 2/2: 按端类型整理模组")
    print("=" * 60)
    run_organizer(mods_dir)

    print("\n" + "=" * 60)
    print("全部完成!")
    print("=" * 60)


if __name__ == '__main__':
    main()
