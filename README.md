# McModClassifier

Minecraft 模组批量分类与整理工具。读取 JAR 文件元数据，通过 Modrinth API / mcmod.cn 自动判定每个模组的**客户端/服务端类型**及**功能大类**，并可按端类型自动整理到对应目录。

## 功能

- **元数据提取** — 支持 `mcmod.info`、`fabric.mod.json`、`mods.toml`、`neoforge.mods.toml`、`MANIFEST.MF` 五种格式
- **三层分类** — Modrinth API（优先）→ mcmod.cn 网页解析（后备）→ 启发式推断（兜底）
- **并发查询** — 20 线程 + 令牌桶限速，不触发 API 上限
- **置信度分级** — high / medium / low 三档，高分匹配跳过后续步骤
- **自动整理** — 按端类型将 JAR 拷贝到 `output/client/` / `server/` / `both/`

## 项目结构

```
├── main.py                      # 统一入口
├── src/
│   ├── classifier/              # 分类表生成模块
│   │   ├── main.py              #   主流程
│   │   ├── core.py              #   API 搜索 + 分类逻辑
│   │   ├── extractor.py         #   JAR 元数据提取
│   │   └── limiter.py           #   速率控制 + 线程安全日志
│   └── organizer/               # 模组整理模块
│       └── organizer.py         #   按 CSV 分类拷贝 JAR
├── mod_classification.csv       # 分类结果（自动生成）
└── output/                      # 整理输出（自动生成）
    ├── client/
    ├── server/
    └── both/
```

## 环境要求

- Python >= 3.10
- `requests`（HTTP 请求）

可选依赖（自动降级到内置解析器）：

- `toml` 或 `tomllib`（3.11+ 内置）— 解析 `mods.toml`

## 使用
```bash(uv)
# 安装依赖
uv init
uv add requests

# 运行（会弹出文件夹选择窗口）
uv run main.py
```
```bash
# 安装依赖
pip install requests

# 运行（会弹出文件夹选择窗口）
python main.py
```

流程：

1. 弹出文件夹选择 → 选定 `mods` 目录
2. 自动生成 `mod_classification.csv`
3. 自动按端类型拷贝 JAR 到 `output/` 下对应子目录

也可在代码中单独调用：

```python
from src.classifier import run_classification
from src.organizer import run_organizer

# 仅生成分类表
run_classification(mods_dir)

# 仅整理模组（依赖已有 CSV）
run_organizer(mods_dir)
```

## 输出说明

### mod_classification.csv 字段

| 字段 | 说明 |
|------|------|
| `filename` | JAR 文件名 |
| `name` | 模组名称 |
| `modid` | 模组 ID |
| `classification` | 端类型：通用 / 仅客户端 / 仅服务端 |
| `broad_category` | 功能大类：科技 / 魔法 / 存储 / 冒险/RPG 等 |
| `confidence` | 置信度：high / medium / low |
| `source` | 数据来源：modrinth / mcmod / heuristic |
| `url` | 模组页面链接 |
| `client_side` / `server_side` | Modrinth 原始 side 字段 |

### output 目录

| 目录 | 端类型 |
|------|--------|
| `output/client/` | 仅客户端（如优化/渲染/UI 模组） |
| `output/server/` | 仅服务端（如性能分析/世界生成） |
| `output/both/` | 通用（绝大多数模组） |

## 分类策略

```
每个 mod:
  ├─ Modrinth API 搜索
  │   ├─ 高置信度 (score >= 60) → 直接采用
  │   ├─ 中置信度 (score >= 25) → 采用但不查 mcmod
  │   └─ 低置信度 / 未命中 ↓
  ├─ mcmod.cn 网页搜索
  │   ├─ 命中 → 采用
  │   └─ 未命中 ↓
  └─ 启发式推断（名称/描述关键词）
```
