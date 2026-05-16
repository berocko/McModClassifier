"""模组整理器 — 根据 mod_classification.csv 按端类型拷贝 JAR 到 output/."""

import csv
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CSV = PROJECT_ROOT / "mod_classification.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "output"

CLASS_MAP = {
    "仅客户端": "client",
    "仅服务端": "server",
    "通用": "both",
    "通用 (客户端可选)": "both",
    "通用 (服务端可选)": "both",
    "未知": "both",
}


def _resolve_dir(classification: str) -> str:
    """将 classification 映射到 output 子目录名."""
    if classification in CLASS_MAP:
        return CLASS_MAP[classification]
    cl = classification
    if '仅客户' in cl or ('客户端' in cl and '服务端' not in cl):
        return 'client'
    if '仅服务' in cl or ('服务端' in cl and '客户端' not in cl):
        return 'server'
    return 'both'


def run_organizer(mods_dir: Path, csv_path: Path = None,
                  output_dir: Path = None):
    """执行整理流程.

    Args:
        mods_dir: 原始 mods JAR 所在目录
        csv_path: 分类 CSV，默认项目根目录的 mod_classification.csv
        output_dir: 输出目录，默认项目根目录的 output/
    """
    if csv_path is None:
        csv_path = DEFAULT_CSV
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT

    print(f"CSV: {csv_path}")
    print(f"Mods 源: {mods_dir}")
    print(f"输出: {output_dir}\n")

    if not csv_path.exists():
        print(f"[ERR] CSV 文件不存在: {csv_path}")
        print("  请先运行 classify 生成分类表")
        return

    if not mods_dir.exists():
        print(f"[ERR] Mods 目录不存在: {mods_dir}")
        return

    # 读取 CSV
    rows = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        print("[ERR] CSV 为空")
        return

    # 清理并创建 output 子目录
    for sub in ['client', 'server', 'both']:
        d = output_dir / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    stats = {'client': 0, 'server': 0, 'both': 0, 'missing': 0}

    for row in rows:
        filename = row.get('filename', '').strip()
        classification = row.get('classification', '未知').strip()

        subdir = _resolve_dir(classification)
        src = mods_dir / filename
        dst = output_dir / subdir / filename

        if not src.exists():
            print(f"  [缺] {filename} — 源文件不存在，跳过")
            stats['missing'] += 1
            continue

        shutil.copy2(src, dst)
        stats[subdir] += 1
        print(f"  [{subdir:6s}] {filename}  ({classification})")

    total = stats['client'] + stats['server'] + stats['both']
    print(f"\n{'=' * 50}")
    print("整理结果汇总")
    print(f"{'=' * 50}")
    print(f"  仅客户端 -> output/client/  : {stats['client']:>4d}")
    print(f"  仅服务端 -> output/server/  : {stats['server']:>4d}")
    print(f"  通用     -> output/both/    : {stats['both']:>4d}")
    if stats['missing']:
        print(f"  源文件缺失 (跳过)          : {stats['missing']:>4d}")
    print(f"  总计                       : {total:>4d}")
    print(f"\n输出目录: {output_dir.resolve()}")
