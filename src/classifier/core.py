"""分类核心 — Modrinth API / mcmod.cn / 启发式推断 + 并发调度."""

import json
import re
import urllib.request
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    requests = None

from .limiter import (
    _modrinth_limiter, _mcmod_limiter, tprint, _print_lock,
    MAX_WORKERS,
)
from .extractor import extract_mod_info

MODRINTH_SEARCH = "https://api.modrinth.com/v2/search"
API_HEADERS = {"User-Agent": "MCModClassifier/1.0"}
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

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

HEURISTIC_RULES = {
    "only_client": [
        "shader", "shaders", "texture", "textures", "animation",
        "hud", "sound", "sounds", "music", "ambient", "input",
        "keybinding", "keybind", "keyboard", "mouse", "chat",
        "fps", "performance", "optimization", "sodium", "optifine",
        "angelica", "rubidium", "embeddium", "iris", "oculus",
    ],
    "only_server": [
        "coremod", "tick", "thread", "chunk", "worldgen",
        "tps", "profiler", "profiling", "lagfix", "server",
    ],
    "library": [
        "api", "lib", "library", "core", "base", "fabric", "forge", "neoforge",
    ],
}


def clean_search_query(name):
    """清理名称用于搜索."""
    q = name
    q = re.sub(r'[.。]\s*[Mm]akes?\s.*$', '', q)
    q = re.sub(r'[.。]\s*[Aa]dds?\s.*$', '', q)
    q = re.sub(r'[-_]\d+[.\d_-]*[a-zA-Z]*$', '', q)
    q = re.sub(r'[-_]GTNH$', '', q, flags=re.IGNORECASE)
    q = re.sub(r'\s*[\[(](?:Fabric|Forge|NeoForge|Quilt)[\])]\s*$', '', q,
               flags=re.IGNORECASE)
    return q.strip()


# ─── Modrinth ───────────────────────────────────────

def search_modrinth(name, modid):
    """搜索 Modrinth，返回 (hit, score)."""
    queries = []
    if name:
        queries.append(clean_search_query(name))
    if modid:
        queries.append(modid)

    for query in queries:
        if not query:
            continue
        _modrinth_limiter.acquire()
        try:
            resp = requests.get(
                MODRINTH_SEARCH,
                params={
                    "query": query,
                    "facets": '[["project_type:mod"]]',
                    "limit": 5,
                },
                headers=API_HEADERS,
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
            return best, best_score

    return None, 0


def modrinth_confidence(hit, score):
    """根据匹配分数 + side 明确度返回置信度档位."""
    level = 'low'
    if score >= 60:
        level = 'high'
    elif score >= 25:
        level = 'medium'

    c = hit.get('client_side', 'unknown')
    s = hit.get('server_side', 'unknown')
    if c != 'unknown' and s != 'unknown':
        if level == 'low':
            level = 'medium'
    elif level == 'medium':
        level = 'high' if c != 'unknown' or s != 'unknown' else level

    return level


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


# ─── mcmod.cn ────────────────────────────────────────

def search_mcmod(name, modid):
    """在 mcmod.cn 搜索，返回 (class_id, display_name) 或 None."""
    queries = []
    if name:
        queries.append(clean_search_query(name))
    if modid and modid not in queries:
        queries.append(modid)

    for query in queries:
        if not query:
            continue
        _mcmod_limiter.acquire()
        try:
            qs = urllib.parse.urlencode({'key': query})
            url = f'https://www.mcmod.cn/s?{qs}'
            req = urllib.request.Request(url, headers=WEB_HEADERS)
            resp = urllib.request.urlopen(req, timeout=10)
            html = resp.read().decode('utf-8', errors='ignore')
        except Exception:
            continue

        items = re.findall(
            r'class=\"result-item\"[^>]*>.*?href=\"(?:https://www\.mcmod\.cn)?'
            r'/class/(\d+)\.html\"[^>]*>(.*?)</a>',
            html, re.DOTALL)
        if not items:
            continue

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
        if best_score > 10 and query == modid:
            return best

    return None


def get_mcmod_detail(class_id):
    """获取 mcmod.cn 模组详情页的端类型和分类."""
    _mcmod_limiter.acquire()
    try:
        url = f'https://www.mcmod.cn/class/{class_id}.html'
        req = urllib.request.Request(url, headers=WEB_HEADERS)
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode('utf-8', errors='ignore')
    except Exception:
        return None

    info = {}

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

    cat_links = re.findall(r'class-category[^>]*>.*?<a[^>]*>(.*?)</a>',
                           html, re.DOTALL)
    cats = []
    for raw in cat_links:
        c = re.sub(r'<[^>]+>', '', raw).strip()
        if c:
            cats.append(c)
    info['categories_raw'] = ', '.join(cats) if cats else ''

    return info


# ─── 启发式 ──────────────────────────────────────────

def heuristic_classify(name, modid, description, side_hint=None,
                       fabric_side=None):
    """当 API 都找不到时智能推断."""
    if side_hint == 'CLIENT':
        return '仅客户端'
    if side_hint == 'SERVER':
        return '仅服务端'
    if fabric_side:
        if fabric_side == 'client':
            return '仅客户端'
        if fabric_side == 'server':
            return '仅服务端'

    text = f"{name} {modid} {description}".lower()
    for kw in HEURISTIC_RULES["only_client"]:
        if kw in text:
            return "仅客户端"
    for kw in HEURISTIC_RULES["only_server"]:
        if kw in text:
            return "仅服务端"
    return "通用"


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


# ─── 单模处理 (线程池 worker) ───────────────────────

def process_one_mod(mod, idx, total):
    """处理单个模组的完整分类流程."""
    name, modid = mod['name'], mod['modid']
    result = {
        'filename': mod['filename'],
        'name': name, 'modid': modid,
        'version': mod['version'], 'mcversion': mod['mcversion'],
        'author': mod['author'], 'description': mod['description'],
        'classification': '未知', 'broad_category': '未分类',
        'categories_cn': '', 'categories_raw': '',
        'client_side': '', 'server_side': '',
        'downloads': 0, 'source': '', 'url': '', 'confidence': '',
    }

    # 策略1: Modrinth
    hit_mr, mr_score = search_modrinth(name, modid)

    if hit_mr:
        conf = modrinth_confidence(hit_mr, mr_score)
        result['classification'] = classify_side_modrinth(hit_mr)
        result['broad_category'] = broad_classify(
            hit_mr.get('categories', []), name, mod.get('description', ''))
        result['categories_cn'] = map_categories(hit_mr.get('categories', []))
        result['categories_raw'] = ', '.join(hit_mr.get('categories', []))
        result['client_side'] = hit_mr.get('client_side', '')
        result['server_side'] = hit_mr.get('server_side', '')
        result['downloads'] = hit_mr.get('downloads', 0)
        result['source'] = 'modrinth'
        result['url'] = f"https://modrinth.com/mod/{hit_mr['slug']}"
        result['confidence'] = conf

        tprint(f" [{idx}/{total}] {name}  ->  [Modrinth:{conf}] "
               f"{hit_mr['title']}  |  {result['classification']}  |  "
               f"score={mr_score:.0f}  dl={hit_mr.get('downloads',0):,}")

        if conf != 'low':
            return result
    else:
        tprint(f" [{idx}/{total}] {name}  ->  [Modrinth] 未命中 "
               f"(score={mr_score:.0f})")

    # 策略2: mcmod.cn
    mc_result = search_mcmod(name, modid)
    if mc_result:
        cid, cname = mc_result
        detail = get_mcmod_detail(cid) or {}
        if detail.get('classification'):
            result['classification'] = detail['classification']
        if detail.get('categories_raw'):
            result['categories_raw'] = detail['categories_raw']
        if not result['broad_category'] or \
           result['broad_category'] == '未分类':
            result['broad_category'] = broad_classify(
                [], name, mod.get('description', ''))
        result['source'] = 'mcmod'
        result['url'] = f"https://www.mcmod.cn/class/{cid}.html"
        result['confidence'] = 'medium'
        tprint(f" [{idx}/{total}] {name}  ->  [mcmod.cn] {cname} "
               f"(class/{cid})  |  {result['classification']}")
        return result

    # 策略3: 启发式
    if not result['classification'] or result['classification'] == '未知':
        result['classification'] = heuristic_classify(
            name, modid, mod.get('description', ''),
            mod.get('_side_hint'), mod.get('_fabric_side'))
    result['broad_category'] = heuristic_broad_category(
        name, modid, mod.get('description', ''))
    result['source'] = 'heuristic'
    result['confidence'] = 'low'
    tprint(f" [{idx}/{total}] {name}  ->  [启发式] "
           f"{result['classification']}  |  {result['broad_category']}")

    return result
