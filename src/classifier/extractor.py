"""从 JAR 文件提取模组元数据.

支持: mcmod.info (flat + modListVersion), fabric.mod.json,
       META-INF/mods.toml, META-INF/neoforge.mods.toml, MANIFEST.MF
"""

import json
import re
import zipfile

try:
    import tomllib
except ImportError:
    try:
        import toml as tomllib
    except ImportError:
        tomllib = None


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
    """
    raw_text = raw_bytes.decode('utf-8', errors='ignore')

    data = None
    if tomllib:
        try:
            data = tomllib.loads(raw_text)
        except Exception:
            data = None
    if data is None:
        data = _regex_parse_mods_toml(raw_text)
        if data is None:
            return {}, None

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

    authors_raw = mod.get('authors', mod.get('author', ''))
    if isinstance(authors_raw, list):
        author_str = ', '.join(str(a) for a in authors_raw if a)
    else:
        author_str = str(authors_raw) if authors_raw else ''

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
        'modid': modid, 'name': name, 'version': version,
        'mcversion': mcversion, 'author': author_str,
        'description': description,
    }, side_hint


def _regex_parse_mods_toml(text):
    """TOML 库不可用时的回退手写解析."""
    result = {'mods': []}
    current_mod = {}
    current_dep = None
    deps = {}

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if line.startswith('[[') and 'mods' in line:
            if current_mod:
                result['mods'].append(current_mod)
            current_mod = {}
            current_dep = None
            continue

        if 'dependencies' in line and line.startswith('['):
            dep_match = re.match(r'\[\[?dependencies\.(\w+)\]\]?', line)
            if dep_match:
                current_dep = dep_match.group(1)
                if current_dep not in deps:
                    deps[current_dep] = []
                deps[current_dep].append({})
            continue

        if '=' in line:
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            elif value.lower() == 'true':
                value = True
            elif value.lower() == 'false':
                value = False

            if current_dep and deps.get(current_dep):
                deps[current_dep][-1][key] = value
            elif current_mod is not None:
                current_mod[key] = value

    if current_mod:
        result['mods'].append(current_mod)
    if deps:
        result['dependencies'] = deps
    return result


def _parse_manifest(raw_bytes):
    """从 META-INF/MANIFEST.MF 提取基本信息."""
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
            base = {'filename': fn}

            # 1) mcmod.info
            if 'mcmod.info' in namelist:
                meta = _parse_mcmod_info(zf.read('mcmod.info'))
                if meta.get('modid'):
                    return {**base, **meta}

            # 2) fabric.mod.json
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
                    '_fabric_side': data.get('environment', ''),
                }

            # 3) META-INF/mods.toml
            if 'META-INF/mods.toml' in namelist:
                meta, side_hint = _parse_mods_toml(zf.read('META-INF/mods.toml'))
                if meta.get('modid'):
                    result = {**base, **meta}
                    if side_hint:
                        result['_side_hint'] = side_hint
                    return result

            # 4) META-INF/neoforge.mods.toml
            if 'META-INF/neoforge.mods.toml' in namelist:
                meta, side_hint = _parse_mods_toml(zf.read('META-INF/neoforge.mods.toml'))
                if meta.get('modid'):
                    result = {**base, **meta}
                    if side_hint:
                        result['_side_hint'] = side_hint
                    return result

            # 5) META-INF/MANIFEST.MF
            if 'META-INF/MANIFEST.MF' in namelist:
                mf = _parse_manifest(zf.read('META-INF/MANIFEST.MF'))
                name = mf.get('Implementation-Title') or mf.get(
                    'Specification-Title') or mf.get('Bundle-Name', '')
                if name:
                    return {
                        **base,
                        'modid': name.lower().replace(' ', '-'),
                        'name': name,
                        'version': mf.get('Implementation-Version')
                        or mf.get('Specification-Version')
                        or mf.get('Bundle-Version', ''),
                        'mcversion': '', 'author': '', 'description': '',
                    }
    except Exception as e:
        pass  # 静默，由调用方处理

    # 无元数据 — 从文件名推断
    stem = fn.rsplit('.', 1)[0]
    return {
        'modid': stem.lower(), 'name': stem, 'version': '',
        'mcversion': '', 'author': '', 'description': '',
        'filename': fn, '_no_metadata': True,
    }
