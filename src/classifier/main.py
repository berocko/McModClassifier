"""分类器主流程 — 元数据提取 + 并发分类 + CSV 输出."""

import csv
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .limiter import tprint, _print_lock, MAX_WORKERS
from .extractor import extract_mod_info
from .core import process_one_mod

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "mod_classification.csv"


def run_classification(mods_dir: Path, output_csv: Path = None):
    """执行分类流程，返回结果列表。output_csv 为 None 则不写文件."""
    t0 = time.monotonic()
    if output_csv is None:
        output_csv = DEFAULT_OUTPUT_CSV

    jar_files = sorted(mods_dir.glob("*.jar"))
    if not jar_files:
        print("[ERR] 没有找到 JAR 文件")
        return []

    total = len(jar_files)
    print(f"找到 {total} 个 JAR\n")

    # Phase 1: 提取元数据
    print("=" * 50)
    print("Phase 1: 提取元数据")
    print("=" * 50)
    mods_info = []
    for jar in jar_files:
        info = extract_mod_info(jar)
        mods_info.append(info)
        tag = " [无元数据]" if info.get('_no_metadata') else ""
        print(f"  {info['name'][:45]:<45s} modid={info['modid']:<28s} "
              f"v={info['version']}{tag}")

    print(f"  共 {len(mods_info)} 个, 耗时 {time.monotonic() - t0:.1f}s\n")

    # Phase 2: 并发分类
    print("=" * 50)
    print(f"Phase 2: 并发分类 ({MAX_WORKERS} 线程)")
    print("=" * 50)

    results_by_idx = {}
    done_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_one_mod, mod, i + 1, total): i
            for i, mod in enumerate(mods_info)
        }

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                results_by_idx[idx] = result
            except Exception as e:
                tprint(f" [ERR] mod[{idx}] 处理异常: {e}")
            done_count += 1
            with _print_lock:
                print(f"\r  进度: {done_count}/{total}", end='', flush=True)

    print()

    results = [results_by_idx[i] for i in range(total)]

    # Phase 3: CSV
    if output_csv:
        print("\n" + "=" * 50)
        print("Phase 3: 输出 CSV")
        print("=" * 50)

        fieldnames = [
            'filename', 'name', 'modid', 'version', 'mcversion', 'author',
            'classification', 'broad_category', 'categories_cn',
            'categories_raw', 'client_side', 'server_side', 'downloads',
            'confidence', 'source', 'url',
        ]

        with open(output_csv, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames,
                                    extrasaction='ignore')
            writer.writeheader()
            for r in results:
                writer.writerow(r)

    # 汇总
    class_counts = Counter(r['classification'] for r in results)
    cat_counts = Counter(r['broad_category'] for r in results)
    src_counts = Counter(r['source'] for r in results)
    conf_counts = Counter(r['confidence'] for r in results)

    elapsed = time.monotonic() - t0
    print(f"\n{'=' * 60}")
    print(f"分类结果汇总 ({len(results)} 个模组)  —  总耗时 {elapsed:.1f}s")
    print(f"{'=' * 60}")
    print(f"  数据来源: Modrinth={src_counts.get('modrinth', 0)}  "
          f"mcmod.cn={src_counts.get('mcmod', 0)}  "
          f"启发式={src_counts.get('heuristic', 0)}")
    print(f"  置信度:   高={conf_counts.get('high', 0)}  "
          f"中={conf_counts.get('medium', 0)}  "
          f"低={conf_counts.get('low', 0)}")

    print("\n[端类型分布]")
    for k, v in class_counts.most_common():
        print(f"  {k:<22s} {v:>3d}")

    print("\n[大类分布]")
    for k, v in cat_counts.most_common():
        print(f"  {k:<22s} {v:>3d}")

    print(f"\n结果已保存 -> {output_csv}")

    return results


