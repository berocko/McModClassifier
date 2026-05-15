#!/usr/bin/env python3
"""
Minecraft Mod 分类器
从 mods/ 目录读取 JAR 文件，提取 mcmod.info 元数据，
通过 Modrinth API + mcmod.cn 查询客户端/服务端分类并自动归类。

输出: mod_classification.csv
"""

import json
import csv
import re
import time
import urllib.request
import urllib.parse
import zipfile
import tkinter as tk
from tkinter import filedialog
from collections import Counter
from pathlib import Path
from io import StringIO

try:
    import requests
except ImportError:
    print("需要安装 requests: pip install requests")
    exit(1)

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import toml as tomllib
    except ImportError:
        tomllib = None  # 降级到 regex 手写解析

OUTPUT_CSV = Path("mod_classification.csv")  # 输出到当前工作目录

MODRINTH_SEARCH = "https://api.modrinth.com/v2/search"
HEADERS = {"User-Agent": "MCModClassifier/1.0"}
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REQUEST_DELAY = 0.35

CATEGORY_MAP = {
    "technology": "科技", "magic": "魔法", "storage": "存储",
    "transportation": "运输", "adventure": "冒险", "worldgen": "世界生成",
    "decoration": "装饰", "farming": "农业", "food": "食物",
    "equipment": "装备", "utility": "工具", "optimization": "优化",
    "library": "前置库", "education": "教育", "misc": "杂项",
}

BROAD_CATEGORY_KEYWORDS = {
    "科技": ["technology", "tech", "energy", "power", "machine", "factory",
             "industrial", "rf", "eu", "applied energistics", "mekanism",
             "gregtech", "thermal", "create", "mechanism", "automation",
             "fluid", "grate", "afsu", "flux"],
    "魔法": ["magic", "thaum", "wand", "spell", "arcane", "sorcery",
             "botania", "blood magic", "alchemical"],
    "存储": ["storage", "chest", "inventory", "barrel", "backpack", "tome"],
    "冒险/RPG": ["adventure", "dungeon", "boss", "quest", "rpg", "explore",
                 "amunra", "amun-ra", "amun", "moon", "planet", "galacticraft",
                 "dimension", "space"],
    "农业/食物": ["farm", "crop", "food", "cook", "plant", "seed", "harvest"],
    "装饰/建筑": ["decor", "block", "furniture", "build", "trophies", "trophy"],
    "优化/性能": ["optimize", "performance", "fps", "lag", "fix", "sodium",
                  "angelica", "shader"],
    "工具/辅助": ["tool", "utility", "util", "waila", "jei", "map", "tome",
                  "angermod", "anger"],
    "世界生成": ["worldgen", "world", "biome", "dimension", "terrain"],
    "装备": ["equipment", "armor", "weapon", "sword", "bow"],
    "前置库/API": ["library", "api", "core"],
}

# 智能推断: (端类型, 大类)
HEURISTIC_RULES = {
    # 纯客户端模组特征 (只有视觉效果/UI/声音/输入)
    "only_client": [
        "shader", "shaders", "texture", "textures", "animation",
        "hud", "sound", "sounds", "music", "ambient", "input",
        "keybinding", "keybind", "keyboard", "mouse", "chat",
        "fps", "performance", "optimization", "sodium", "optifine",
        "angelica", "rubidium", "embeddium", "iris", "oculus",
    ],
    # 纯服务端模组特征 (只处理服务端逻辑/数据)
    "only_server": [
        "coremod", "tick", "thread", "chunk", "worldgen",
        "tps", "profiler", "profiling", "lagfix", "server",
    ],
    # 常见库/API模组(通用)
    "library": [
        "api", "lib", "library", "core", "base", "fabric",
        "forge", "neoforge",
    ],
}


def _parse_mcmod_info(raw_bytes):
    """解析 mcmod.info，兼容 flat / modListVersion 两种格式."""
    data = json.loads(raw_bytes)

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and 'modList' in data:
        items = data['modList']
    elif isinstance(data, dict) and 'modid' in data:
        items = [data]
    else:
        return {}

    if not items:
        return {}
    item = items[0]

    authors = item.get('authorList', []) or item.get('authors', [])
    if isinstance(authors, list):
        author_str = ', '.join(a for a in authors if a)
    else:
        author_str = str(authors) if authors else ''

    return {
        'modid': item.get('modid', ''),
        'name': item.get('name', ''),
        'version': item.get('version', ''),
        'mcversion': item.get('mcversion', ''),
        'author': author_str,
        'description': (item.get('description', '') or '')[:200],
    }


def _parse_mods_toml(raw_bytes):
    """解析 META-INF/mods.toml 或 neoforge.mods.toml.

    返回 (meta_dict, toml_side_hint).
    meta_dict: 标准元数据字段
    toml_side_hint: 从依赖推断的端类型提示 (CLIENT/SERVER/BOTH/None)
    """
    raw_text = raw_bytes.decode('utf-8', errors='ignore')

    # 方法1: 使用 toml 库
    data = None
    if tomllib:
        try:
            data = tomllib.loads(raw_text)
        except Exception:
            data = None

    # 方法2: 手写简化 TOML 解析 (当 toml 库不可用或解析失败时)
    if data is None:
        data = _regex_parse_mods_toml(raw_text)
        if data is None:
            return {}, None

    # 提取 [[mods]] 数组
    mods = data.get('mods', [])
    if isinstance(mods, dict):
        mods = [mods]
    if not mods:
        return {}, None
    mod = mods[0]

    modid = mod.get('modId', mod.get('modId', ''))
    name = mod.get('displayName', mod.get('displayName', modid))
    version = mod.get('version', '')
    description = (mod.get('description', '') or '')[:200]

    # 作者: 可能是字符串或列表
    authors_raw = mod.get('authors', mod.get('author', ''))
    if isinstance(authors_raw, list):
        author_str = ', '.join(str(a) for a in authors_raw if a)
    else:
        author_str = str(authors_raw) if authors_raw else ''

    # 从依赖中提取 MC 版本
    mcversion = ''
    deps = data.get('dependencies', {})
    if isinstance(deps, dict):
        for dep_key, dep_list in deps.items():
            if isinstance(dep_list, list):
                for dep in dep_list:
                    if dep.get('modId', '').lower() == 'minecraft':
                        mcversion = dep.get('versionRange', '')
                        break
            if mcversion:
                break

    # 从依赖推断端类型提示
    side_hint = None
    all_sides = set()
    if isinstance(deps, dict):
        for dep_key, dep_list in deps.items():
            if isinstance(dep_list, list):
                for dep in dep_list:
                    s = dep.get('side', '').upper()
                    if s in ('CLIENT', 'SERVER', 'BOTH'):
                        all_sides.add(s)
    if all_sides == {'CLIENT'}:
        side_hint = 'CLIENT'
    elif all_sides == {'SERVER'}:
        side_hint = 'SERVER'

    return {
        'modid': modid,
        'name': name,
        'version': version,
        'mcversion': mcversion,
        'author': author_str,
        'description': description,
    }, side_hint


def _regex_parse_mods_toml(text):
    """TOML 库不可用时的回退手写解析，仅提取关键字段."""
    result = {'mods': []}
    current_mod = {}
    current_dep = None
    deps = {}

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # [[mods]] 开始新 mod
        if line.startswith('[[') and 'mods' in line:
            if current_mod:
                result['mods'].append(current_mod)
            current_mod = {}
            current_dep = None
            continue

        # [dependencies.xxx] 或 [[dependencies.xxx]]
        if 'dependencies' in line and (line.startswith('[')):
            dep_match = re.match(r'\[\[?dependencies\.(\w+)\]\]?', line)
            if dep_match:
                current_dep = dep_match.group(1)
                if current_dep not in deps:
                    deps[current_dep] = []
                deps[current_dep].append({})
            continue

        # 普通键值对
        if '=' in line:
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()

            # 去掉引号
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            # 布尔值
            elif value.lower() == 'true':
                value = True
            elif value.lower() == 'false':
                value = False

            if current_dep and deps.get(current_dep):
                deps[current_dep][-1][key] = value
            elif current_mod is not None:
                current_mod[key] = value
                # 兼容: modLoader, license 等顶层 key
                if result.get(key) is None and key not in ('modId', 'version', 'displayName', 'authors', 'description'):
                    result[key] = value

    if current_mod:
        result['mods'].append(current_mod)
    if deps:
        result['dependencies'] = deps
    return result


def _parse_manifest(raw_bytes):
    """从 META-INF/MANIFEST.MF 提取基本信息 (最后手段)."""
    text = raw_bytes.decode('utf-8', errors='ignore')
    info = {}
    for key in ('Implementation-Title', 'Implementation-Version',
                 'Specification-Title', 'Specification-Version',
                 'Bundle-Name', 'Bundle-Version'):
        m = re.search(rf'{key}\s*:\s*(.+)', text)
        if m:
            info[key] = m.group(1).strip()
    return info


def extract_mod_info(jar_path):
    """从 JAR 提取元数据 — 支持所有主流格式."""
    fn = jar_path.name
    try:
        with zipfile.ZipFile(jar_path, 'r') as zf:
            namelist = zf.namelist()

            # 优先级: mcmod.info > fabric.mod.json > mods.toml > neoforge.mods.toml > MANIFEST.MF
            base = {'filename': fn}

            # 1) mcmod.info (Forge 1.7.10 ~ 1.12.2, GTNH)
            if 'mcmod.info' in namelist:
                meta = _parse_mcmod_info(zf.read('mcmod.info'))
                if meta.get('modid'):
                    return {**base, **meta}

            # 2) fabric.mod.json (Fabric 全版本)
            if 'fabric.mod.json' in namelist:
                data = json.loads(zf.read('fabric.mod.json'))
                authors = data.get('authors', [])
                auth_str = ', '.join(
                    a.get('name', '') if isinstance(a, dict) else str(a)
                    for a in authors
                ) if isinstance(authors, list) else str(authors)
                return {
                    **base,
                    'modid': data.get('id', ''),
                    'name': data.get('name', data.get('id', '')),
                    'version': data.get('version', ''),
                    'mcversion': '',
                    'author': auth_str,
                    'description': (data.get('description', '') or '')[:200],
                    # fabric 中有 side 字段
                    '_fabric_side': data.get('environment', ''),  # client / server / universal
                }

            # 3) META-INF/mods.toml (Forge 1.13+)
            if 'META-INF/mods.toml' in namelist:
                meta, side_hint = _parse_mods_toml(zf.read('META-INF/mods.toml'))
                if meta.get('modid'):
                    result = {**base, **meta}
                    if side_hint:
                        result['_side_hint'] = side_hint
                    return result

            # 4) META-INF/neoforge.mods.toml (NeoForge 1.21+)
            if 'META-INF/neoforge.mods.toml' in namelist:
                meta, side_hint = _parse_mods_toml(zf.read('META-INF/neoforge.mods.toml'))
                if meta.get('modid'):
                    result = {**base, **meta}
                    if side_hint:
                        result['_side_hint'] = side_hint
                    return result

            # 5) META-INF/MANIFEST.MF (最后手段)
            if 'META-INF/MANIFEST.MF' in namelist:
                mf = _parse_manifest(zf.read('META-INF/MANIFEST.MF'))
                name = mf.get('Implementation-Title') or mf.get('Specification-Title') or mf.get('Bundle-Name', '')
                if name:
                    return {
                        **base,
                        'modid': name.lower().replace(' ', '-'),
                        'name': name,
                        'version': mf.get('Implementation-Version') or mf.get('Specification-Version') or mf.get('Bundle-Version', ''),
                        'mcversion': '', 'author': '', 'description': '',
                    }

    except Exception as e:
        print(f"  [WARN] {fn}: {e}")

    # 完全无元数据 — 从文件名推断
    stem = fn.rsplit('.', 1)[0]
    return {
        'modid': stem.lower(), 'name': stem, 'version': '',
        'mcversion': '', 'author': '', 'description': '',
        'filename': fn, '_no_metadata': True,
    }


def clean_search_query(name):
    """清理名称用于搜索: 去掉版本号/后缀/副标题等."""
    q = name
    # 去掉 "... Makes your Mobs angry!" 之类的副标题
    q = re.sub(r'[.。]\s*[Mm]akes?\s.*$', '', q)
    q = re.sub(r'[.。]\s*[Aa]dds?\s.*$', '', q)
    # 去掉末尾版本模式: name-1.2.3, name-1.2.3a, name_1.2.3-GTNH 等
    q = re.sub(r'[-_]\d+[.\d_-]*[a-zA-Z]*$', '', q)
    # 去掉末尾 -GTNH 标签
    q = re.sub(r'[-_]GTNH$', '', q, flags=re.IGNORECASE)
    # 去掉 (Fabric) / [Forge] 标注
    q = re.sub(r'\s*[\[(](?:Fabric|Forge|NeoForge|Quilt)[\])]\s*$', '', q, flags=re.IGNORECASE)
    return q.strip()


# ─── Modrinth ───────────────────────────────────────────

def search_modrinth(name, modid):
    """搜索 Modrinth，返回最佳匹配 hit 或 None."""
    queries = []
    if name:
        queries.append(clean_search_query(name))
    if modid:
        queries.append(modid)

    for query in queries:
        if not query:
            continue
        try:
            resp = requests.get(
                MODRINTH_SEARCH,
                params={
                    "query": query,
                    "facets": '[["project_type:mod"]]',
                    "limit": 5,
                },
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            hits = resp.json().get('hits', [])
        except Exception:
            continue

        if not hits:
            continue

        q_lower = query.lower()
        mid_lower = modid.lower()
        best, best_score = None, -1

        for hit in hits:
            score = 0
            tl = hit['title'].lower()
            sl = hit['slug'].lower()
            pl = hit.get('project_id', '').lower()

            if tl == q_lower:
                score += 50
            if q_lower in tl or tl in q_lower:
                score += 30
            if mid_lower and (mid_lower == sl or mid_lower == pl):
                score += 40
            if mid_lower and (mid_lower in sl or mid_lower in pl):
                score += 20
            q_words = set(q_lower.replace('-', ' ').split())
            t_words = set(tl.replace('-', ' ').split())
            score += len(q_words & t_words) * 5
            score += min(hit.get('downloads', 0) / 500_000, 10)

            if score > best_score:
                best_score = score
                best = hit

        if best and best_score >= 15:
            return best

    return None


def classify_side_modrinth(hit):
    """根据 client_side / server_side 判定端类型."""
    c = hit.get('client_side', 'unknown')
    s = hit.get('server_side', 'unknown')

    if c == 'required' and s == 'required':
        return '通用'
    if c == 'required' and s == 'unsupported':
        return '仅客户端'
    if c == 'unsupported' and s == 'required':
        return '仅服务端'
    if c == 'required':
        return '仅客户端'
    if s == 'required':
        return '仅服务端'
    if c == 'optional' and s == 'required':
        return '通用 (客户端可选)'
    if c == 'required' and s == 'optional':
        return '通用 (服务端可选)'
    if c == 'optional' and s == 'optional':
        return '通用'
    return '未知'


def map_categories(categories):
    result = []
    for cat in categories:
        cn = CATEGORY_MAP.get(cat, '')
        if cn and cn not in result:
            result.append(cn)
    return ', '.join(result) if result else '其他'


# ─── mcmod.cn ────────────────────────────────────────────

def search_mcmod(name, modid):
    """在 mcmod.cn 搜索模组，返回 (class_id, display_name) 或 None."""
    queries = []
    if name:
        queries.append(clean_search_query(name))
    if modid and modid not in queries:
        queries.append(modid)

    for query in queries:
        if not query:
            continue
        try:
            qs = urllib.parse.urlencode({'key': query})
            url = f'https://www.mcmod.cn/s?{qs}'
            req = urllib.request.Request(url, headers=WEB_HEADERS)
            resp = urllib.request.urlopen(req, timeout=10)
            html = resp.read().decode('utf-8', errors='ignore')
        except Exception:
            continue

        # 提取搜索结果: /class/ID.html 和名称
        items = re.findall(
            r'class=\"result-item\"[^>]*>.*?href=\"(?:https://www\.mcmod\.cn)?/class/(\d+)\.html\"[^>]*>(.*?)</a>',
            html, re.DOTALL
        )
        if not items:
            continue

        # 选最佳匹配 (类似 Modrinth 打分)
        q_lower = query.lower()
        mid_lower = modid.lower()
        best, best_score = None, -1

        for cid, raw_name in items:
            cname = re.sub(r'<[^>]+>', '', raw_name).strip()
            cn_lower = cname.lower()
            score = 0
            if cn_lower == q_lower:
                score += 50
            if q_lower in cn_lower or cn_lower in q_lower:
                score += 30
            if mid_lower and mid_lower in cn_lower:
                score += 25
            q_words = set(q_lower.replace('-', ' ').split())
            c_words = set(cn_lower.replace('-', ' ').split())
            score += len(q_words & c_words) * 5

            if score > best_score:
                best_score = score
                best = (cid, cname)

        if best and best_score >= 20:
            return best
        # 如果只用 modid 搜都找不到，且 query != modid，给第一个结果
        if best_score > 10 and query == modid:
            return best

    return None


def get_mcmod_detail(class_id):
    """获取 mcmod.cn 模组详情页的端类型和分类."""
    try:
        url = f'https://www.mcmod.cn/class/{class_id}.html'
        req = urllib.request.Request(url, headers=WEB_HEADERS)
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode('utf-8', errors='ignore')
    except Exception:
        return None

    info = {}

    # 提取运行环境
    env_match = re.search(r'运行环境[：:]\s*([^<]+)', html)
    if env_match:
        env_text = env_match.group(1).strip()
        info['env_raw'] = env_text
        if '客户端需装' in env_text and '服务端需装' in env_text:
            info['classification'] = '通用'
        elif '客户端需装' in env_text and '服务端不需装' in env_text:
            info['classification'] = '仅客户端'
        elif '客户端不需装' in env_text and '服务端需装' in env_text:
            info['classification'] = '仅服务端'
        elif '客户端需装' in env_text:
            info['classification'] = '仅客户端'
        elif '服务端需装' in env_text:
            info['classification'] = '仅服务端'
        elif '客户端可选' in env_text or '服务端可选' in env_text:
            info['classification'] = '通用'

    # 提取分类标签 (class-category 里的链接)
    cat_links = re.findall(r'class-category[^>]*>.*?<a[^>]*>(.*?)</a>', html, re.DOTALL)
    cats = []
    for raw in cat_links:
        c = re.sub(r'<[^>]+>', '', raw).strip()
        if c:
            cats.append(c)
    info['categories_raw'] = ', '.join(cats) if cats else ''

    return info


# ─── 启发式推断 ──────────────────────────────────────────

def heuristic_classify(name, modid, description, side_hint=None, fabric_side=None):
    """当 API 都找不到时，基于名称/描述/元数据 side 提示智能推断."""
    # 优先信任 TOML 依赖的 side 推断
    if side_hint == 'CLIENT':
        return '仅客户端'
    if side_hint == 'SERVER':
        return '仅服务端'

    # Fabric mods 有明确的 environment 字段
    if fabric_side:
        if fabric_side == 'client':
            return '仅客户端'
        if fabric_side == 'server':
            return '仅服务端'

    text = f"{name} {modid} {description}".lower()

    # 纯客户端检测
    for kw in HEURISTIC_RULES["only_client"]:
        if kw in text:
            return "仅客户端"

    # 纯服务端检测
    for kw in HEURISTIC_RULES["only_server"]:
        if kw in text:
            return "仅服务端"

    return "通用"  # 绝大多数模组都是通用的


def heuristic_broad_category(name, modid, description):
    """基于名称/描述推断大类."""
    text = f"{name} {modid} {description}".lower()
    for cat, kws in BROAD_CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return cat
    return "其他"


def broad_classify(categories, name, desc):
    """自动归入大类."""
    cats = [c.lower() for c in categories]
    text = (name + ' ' + (desc or '')).lower()

    if any(c in cats for c in ['technology', 'tech', 'automation']):
        return '科技'
    if any(c in cats for c in ['magic']):
        return '魔法'
    if any(c in cats for c in ['storage']):
        return '存储'
    if any(c in cats for c in ['transportation']):
        return '运输/交通'
    if any(c in cats for c in ['adventure']):
        return '冒险/RPG'
    if any(c in cats for c in ['farming', 'food']):
        return '农业/食物'
    if any(c in cats for c in ['decoration']):
        return '装饰/建筑'
    if any(c in cats for c in ['optimization']):
        return '优化/性能'
    if any(c in cats for c in ['utility']):
        return '工具/辅助'
    if any(c in cats for c in ['worldgen']):
        return '世界生成'
    if any(c in cats for c in ['equipment']):
        return '装备'
    if any(c in cats for c in ['library']):
        return '前置库/API'

    for cat, kws in BROAD_CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text:
                return cat
    return '其他'


# ─── 主流程 ──────────────────────────────────────────────

def choose_mods_dir():
    """打开文件夹选择对话框，让用户指定 mods 目录."""
    root = tk.Tk()
    root.withdraw()  # 隐藏主窗口
    root.attributes('-topmost', True)  # 置顶对话框

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
    print("Minecraft Mod 分类器 - Modrinth + mcmod.cn + 启发式")
    print("=" * 60)

    print("\n请在弹出的文件夹选择窗口中选定 mods 目录...")
    mods_dir = choose_mods_dir()
    if mods_dir is None:
        return

    print(f"已选择: {mods_dir}\n")

    if not mods_dir.exists():
        print(f"[ERR] 目录不存在: {mods_dir}")
        return

    jar_files = sorted(mods_dir.glob("*.jar"))
    if not jar_files:
        print("[ERR] mods/ 中没有 JAR")
        return
    print(f"\n找到 {len(jar_files)} 个 JAR\n")

    # Phase 1: 提取元数据
    print("--- Phase 1: 提取元数据 ---")
    mods_info = []
    for jar in jar_files:
        info = extract_mod_info(jar)
        mods_info.append(info)
        tag = " [无元数据]" if info.get('_no_metadata') else ""
        print(f"  {info['name'][:45]:<45s} modid={info['modid']:<25s} v={info['version']}{tag}")

    print(f"\n  共 {len(mods_info)} 个\n")

    # Phase 2: 分类
    print("--- Phase 2: 查询 & 分类 ---")
    results = []
    stats = {'modrinth': 0, 'mcmod': 0, 'heuristic': 0}

    for i, mod in enumerate(mods_info):
        name, modid = mod['name'], mod['modid']
        print(f"\n[{i+1}/{len(mods_info)}] {name}")
        time.sleep(REQUEST_DELAY)

        classification = None
        broad = None
        cat_cn = ''
        cat_raw = ''
        source = ''
        url = ''
        downloads = 0
        client_side = ''
        server_side = ''

        # 策略1: Modrinth API
        hit_mr = search_modrinth(name, modid)
        if hit_mr:
            classification = classify_side_modrinth(hit_mr)
            broad = broad_classify(hit_mr.get('categories', []), name, mod.get('description', ''))
            cat_cn = map_categories(hit_mr.get('categories', []))
            cat_raw = ', '.join(hit_mr.get('categories', []))
            client_side = hit_mr.get('client_side', '')
            server_side = hit_mr.get('server_side', '')
            downloads = hit_mr.get('downloads', 0)
            source = 'modrinth'
            url = f"https://modrinth.com/mod/{hit_mr['slug']}"
            stats['modrinth'] += 1
            print(f"  [Modrinth] {hit_mr['title']}  |  {classification}  |  {broad}  |  dl={downloads:,}")

        # 策略2: mcmod.cn
        if not hit_mr:
            mc_result = search_mcmod(name, modid)
            if mc_result:
                cid, cname = mc_result
                detail = get_mcmod_detail(cid) or {}
                classification = detail.get('classification') or classify_side_modrinth({'client_side': 'unknown', 'server_side': 'unknown'})
                cat_raw = detail.get('categories_raw', '')
                broad = broad_classify([], name, mod.get('description', ''))
                source = 'mcmod'
                url = f"https://www.mcmod.cn/class/{cid}.html"
                stats['mcmod'] += 1
                print(f"  [mcmod.cn] {cname} (class/{cid})  |  {classification}  |  {cat_raw}")
            else:
                # 策略3: 启发式推断 (结合元数据 side 提示)
                classification = heuristic_classify(name, modid, mod.get('description', ''),
                                                    mod.get('_side_hint'), mod.get('_fabric_side'))
                broad = heuristic_broad_category(name, modid, mod.get('description', ''))
                source = 'heuristic'
                stats['heuristic'] += 1
                print(f"  [启发式] {classification}  |  {broad}")

        results.append({
            'filename': mod['filename'],
            'name': mod['name'],
            'modid': mod['modid'],
            'version': mod['version'],
            'mcversion': mod['mcversion'],
            'author': mod['author'],
            'description': mod['description'],
            'classification': classification or '未知',
            'broad_category': broad or '未分类',
            'categories_cn': cat_cn,
            'categories_raw': cat_raw,
            'client_side': client_side,
            'server_side': server_side,
            'downloads': downloads,
            'source': source,
            'url': url,
        })

    # Phase 3: CSV
    print("\n--- Phase 3: 输出 CSV ---")

    fieldnames = [
        'filename', 'name', 'modid', 'version', 'mcversion', 'author',
        'classification', 'broad_category', 'categories_cn', 'categories_raw',
        'client_side', 'server_side', 'downloads', 'source', 'url',
    ]

    with open(OUTPUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    # 汇总
    class_counts = Counter(r['classification'] for r in results)
    cat_counts = Counter(r['broad_category'] for r in results)

    print(f"\n{'=' * 60}")
    print(f"分类结果汇总 ({len(results)} 个模组)")
    print(f"{'=' * 60}")
    print(f"  数据来源: Modrinth={stats['modrinth']}  mcmod.cn={stats['mcmod']}  启发式={stats['heuristic']}")

    print("\n[端类型分布]")
    for k, v in class_counts.most_common():
        print(f"  {k:<20s} {v:>3d}")

    print("\n[大类分布]")
    for k, v in cat_counts.most_common():
        print(f"  {k:<20s} {v:>3d}")

    print(f"\n结果已保存 -> {OUTPUT_CSV}")


if __name__ == '__main__':
    main()
