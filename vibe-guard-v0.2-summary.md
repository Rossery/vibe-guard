# Vibe Guard v0.2 —— 总结报告

_日期：2026-06-16 · 单轮 headless 运行 · 三个任务全部完成_

本文件汇总本轮三件事：**① 架构简介 + 流程图**、**② GitHub 推送结果**、
**③ v0.2 幻觉检测独立模块的实现摘要**。

---

## 任务 1 · 架构简介与流程图

### 一段话讲清楚我们是谁、怎么做

**Vibe Guard 是一个面向「AI 生成代码」（vibe coding 产物）的端到端验证工具。**
当 AI Agent 用自然语言一口气生成整个代码仓库时，会反复出现三类风险：实现的**不是**你
要的（功能漂移）、能跑但**不对/不稳**（缺错误处理）、以及**不安全**（硬编码密钥、不安全
模式、引用根本不存在的依赖包）。Vibe Guard 回答一个问题：**这份代码做到了它声称的事
吗？能安全上线吗？**

架构上，Vibe Guard 是一个**确定性状态机编排层（thin orchestrator）**——它本身代码量
很小，只负责「编排 + prompt + 证据聚合 + 报告」，把所有重活外挂给成熟开源工具。整条
流水线分 **5 个阶段**：**Ingest（采集）→ Normalize（需求归一化）→ 三路并行验证 →
Judge（裁决）→ Report（报告）**。Ingest 用 **tree-sitter** 把仓库解析成符号图（函数/类/
方法 + 签名 + docstring + 调用关系）；Normalize 让 LLM 把 README/需求拆成离散、可测试的
功能点；随后三路并行：**路 A 功能对齐**（逐功能点检索真实源码片段，LLM 判定
已实现/部分/缺失并附 `file:line` 证据）、**路 B 测试执行**（参考 **Qodo Cover-Agent** 合成
测试并在沙箱真实运行）、**路 C 安全与供应链**（**Semgrep** SAST + **Trivy** SCA/CVE +
**Gitleaks** 密钥扫描）。

我们自研的核心创新是**幻觉依赖检测（hallucinated-dependency detection）**：解析项目里
**真实的依赖声明**（pyproject.toml / setup.py / requirements.txt / package.json）和源码
import，逐包查询 **PyPI / npm registry**，标记**不存在/404 的包**——这正是 AI 代码特有
的 **slopsquatting（投毒蹭名）** 失败模式（约 1/5 的 AI 代码会引用不存在的包，攻击者抢注
这些名字投放恶意包）。v0.2 进一步加入 **typo-squat 模糊匹配**、**新包/低星告警**、**npm
支持**、**本地缓存**和**可配置严重度**。

贯穿全程的哲学是 **execution > opinion（执行优先于观点）**：**LLM 只负责提出「该测什么」，
真实的执行结果（测试跑绿、扫描器命中、registry 查询）才决定「是否通过」。** 每一条结论
都必须挂上可复核的证据与置信度——没有证据，不进报告。

### 完整数据流（流程图）

```text
                                  ┌──────────────────────────┐
              repo (路径/Git URL) │  仓库 + 需求描述(可选)     │
              + PRD/README/对话   │  缺失时从代码反推          │
                                  └────────────┬─────────────┘
                                               │
              ╔════════════════════════════════▼════════════════════════════════╗
              ║   确定性状态机编排层 (thin orchestrator / deterministic FSM)      ║
              ╚════════════════════════════════╤════════════════════════════════╝
                                               │
                     ┌─────────────────────────▼─────────────────────────┐
                     │ ① INGEST 采集                                      │
                     │   tree-sitter → 符号图(函数/类/方法+签名+docstring │
                     │   +调用关系) · README · 依赖清单(py & npm)         │
                     └─────────────────────────┬─────────────────────────┘
                                               │
                     ┌─────────────────────────▼─────────────────────────┐
                     │ ② NORMALIZE 需求归一化                             │
                     │   LLM: README/需求 → 离散可测试功能点 (RequirementSpec)│
                     └─────────────────────────┬─────────────────────────┘
                                               │
            ┌──────────────────────────────────┼──────────────────────────────────┐
            │ 三路并行验证 (parallel verification, defense in depth)               │
            │                                  │                                   │
            ▼                                  ▼                                   ▼
 ┌────────────────────┐          ┌──────────────────────┐         ┌─────────────────────────────┐
 │ 路 A  功能对齐      │          │ 路 B  测试执行        │         │ 路 C  安全 & 供应链          │
 │ 逐功能点检索源码    │          │ 合成测试→沙箱真实跑   │         │ Semgrep(SAST)               │
 │ LLM 判定: 实现/部分 │          │ (参考 Qodo Cover-Agent)│         │ Trivy(SCA/CVE) Gitleaks(密钥)│
 │ /缺失 + file:line   │          │ 属性测试/变异测试      │         │ ★ 幻觉依赖检测 (自研)        │
 │ 证据                │          │ → 通过/失败 + 日志     │         │   声明&import → 查 PyPI/npm  │
 └─────────┬──────────┘          └───────────┬──────────┘         │   404→幻觉 · typo-squat       │
           │                                 │                    │   ·新包告警·缓存·可配严重度   │
           │                                 │                    └──────────────┬──────────────┘
           │                                 │                                   │
           └─────────────────────────────────┼───────────────────────────────────┘
                                              ▼
                     ┌─────────────────────────────────────────────────┐
                     │ ③ JUDGE 裁决 (execution > opinion)              │
                     │   聚合三路证据+置信度 → 总判定                   │
                     │   PASS / PASS-WITH-WARNINGS / NEEDS-REVIEW       │
                     └─────────────────────────┬───────────────────────┘
                                               │
                     ┌─────────────────────────▼───────────────────────┐
                     │ ④ REPORT 报告                                   │
                     │   Markdown: 功能检查清单 + 安全红线(按严重度)    │
                     │   + 每条结论的可复核证据链 · JSON(CI 集成)       │
                     └─────────────────────────────────────────────────┘
```

> 注：v0.1 MVP 已落地 ①②③(路 A)④ 与路 C（含幻觉依赖检测）；路 B（沙箱测试执行）为
> 架构设计中的下一阶段。同样内容已单独存为 `ARCHITECTURE-OVERVIEW-cn.md`。

---

## 任务 2 · GitHub 推送结果

**结论：推送成功 ✅**

### Token 诊断

```
curl -sI -H "Authorization: Bearer <token>" https://api.github.com/user
→ HTTP/2 200            (token 有效)
github-authentication-token-expiration: 2026-06-23 05:36:42 UTC
```

> 与上一轮（v0.1）不同：v0.1 总结里记录的 token 返回 **401 Bad credentials**，无法推送；
> 本轮提供的新 token 经 `GET /user` 验证返回 **200**，有效，可正常推送。

### 推送方式

`github.com` 的原生 git over HTTPS 在本环境受限，但 `api.github.com` 经代理可达
（curl 返回 200）。因此采用 **GitHub Git Data API** 一次性提交（比逐文件 Contents API
更干净、原子）：

1. `GET /git/ref/heads/main` → 取当前 commit 与 base tree；
2. 对每个文件 `POST /git/blobs`（base64）→ blob sha；
3. `POST /git/trees`（带 `base_tree`）→ 新 tree；
4. `POST /git/commits`（parents = 旧 commit）→ 新 commit；
5. `PATCH /git/refs/heads/main` → 推进 main。

### 推送内容（仓库 `Rossery/vibe-guard`，分支 `main`）

| 类别 | 文件 |
|---|---|
| 架构文档 | `ARCHITECTURE.md`、`ARCHITECTURE-OVERVIEW-cn.md`（任务 1 产出） |
| 说明/结果 | `README.md`（**已更新为中文版**）、`vibe-guard-v0.1-results-cn.md`、`vibe-guard-v0.1-results.md`、`vibe-guard-v0.2-summary.md`（本文件） |
| 打包 | `pyproject.toml`（v0.2，新增 `vibe-guard-deps` 入口）、`.gitignore` |
| v0.1 源码 | `vibe_guard/`：`cli/models/ingest/align/llm/normalizer/report/security` 等 10 个文件 |
| **v0.2 新模块** | `vibe_guard/hallucheck/`：`models/config/cache/fuzzy/parsers/registry/detector/cli/__init__/__main__` |
| **单元测试** | `vibe_guard/hallucheck/tests/`：`test_fuzzy/parsers/cache/registry/detector` |

**主推送 commit：** `38e4a889cc94cd5b8af285fc1fc869ff7265d412`（33 个文件）
→ https://github.com/Rossery/vibe-guard/commit/38e4a889

本总结文件随后以一个 follow-up commit 推送。已通过
`GET /contents/vibe_guard/hallucheck` 复核远端文件确实存在。

---

## 任务 3 · v0.2 幻觉检测独立模块实现摘要

把幻觉依赖检测从 `vibe_guard/security.py` 中抽取出来，做成一个**独立、零三方依赖、
可单独运行、充分测试**的模块 `vibe_guard/hallucheck/`。

### 目录结构

```
vibe_guard/hallucheck/
├── __init__.py        # 公共 API：HallucinationDetector / DetectorConfig / 模型
├── __main__.py        # python -m vibe_guard.hallucheck
├── models.py          # Ecosystem / Severity / FindingKind / Dependency / Finding / DetectionResult（dataclass，含 JSON 序列化）
├── config.py          # DetectorConfig：开关、阈值、每类严重度可配置
├── cache.py           # NullCache / MemoryCache / JsonFileCache（带 TTL、原子写）
├── fuzzy.py           # Levenshtein + 热门包名表 + typo-squat 匹配
├── parsers.py         # 解析 py/npm 清单 + 源码 import 收集 + 别名映射
├── registry.py        # PyPIClient / NpmClient（HTTP fetcher 可注入、可缓存）
├── detector.py        # 编排：解析 → 查询 → 启发式 → 汇总
├── cli.py             # 独立子命令 vibe-guard-deps（文本 / --json）
└── tests/             # 38 个单元测试，全部离线（registry 用桩）
    ├── test_fuzzy.py · test_parsers.py · test_cache.py
    ├── test_registry.py · test_detector.py
```

代码量：模块 **~1367 行**，测试 **~485 行**；类型注解完整、文档注释齐全。

### v0.1 已有能力（保留并强化）

- **解析真实依赖声明**：`requirements*.txt` / `constraints.txt`、`pyproject.toml`
  （PEP 621 + build-system + poetry）、`setup.py`（`install_requires` 等）、`setup.cfg`、
  `Pipfile`。
- **提取第三方 import 并映射包名**：收集源码 import，排除 stdlib 与本地包，
  通过 import→distribution 别名表（`yaml→pyyaml`、`cv2→opencv-python`、`bs4→beautifulsoup4`…）
  归一化。
- **逐包查 PyPI JSON API**，标记 404 的包为幻觉。
- **区分 declared-but-not-found（声明却不存在）vs imported-but-not-declared
  （导入却未声明）**；dev/test 目录里的未声明 import 降级为 INFO，避免淹没真信号。

### v0.2 新增能力

| 能力 | 说明 | 严重度 |
|---|---|---|
| **包名模糊匹配（typo-squat）** | Levenshtein 编辑距离 ≤ 配置阈值（短名收紧到 1）匹配热门包表：`requets`→`requests`、`beautfulsoup4`→`beautifulsoup4`、`lodahs`→`lodash`。本身就是热门包则不误报。 | `HIGH` |
| **新包告警** | 查 PyPI 各 release 的最早 `upload_time` / npm `time.created`，早于阈值（默认 60 天）即标记——新注册包是 slopsquat/投毒常见载体。 | `MEDIUM` |
| **低下载量告警** | npm `downloads/point/last-week`，低于阈值（默认 500/周）标记冷门可疑。 | `LOW` |
| **npm 支持** | 解析 `package.json`（deps/devDeps/peer/optional）+ JS/TS 源码 `import`/`require`/动态 `import()`，scope 子路径 `@babel/core/x`→`@babel/core`，排除 Node 内置模块，查 npm registry。 | — |
| **缓存机制** | `JsonFileCache`（默认 `~/.cache/vibe-guard/hallucheck/`，带 TTL、原子写）；registry 查询结果按 `生态:包名` 缓存，避免重复 API 调用。 | — |
| **可配置严重度** | `DetectorConfig.severity_overrides` 按 `FindingKind` 覆盖默认严重度。 | — |
| **独立 CLI 子命令** | `vibe-guard-deps <path>`，可单独跑；`--fail-on` 控制退出码用于 CI 门禁。 | — |
| **JSON 输出** | `DetectionResult.to_json()` / `--json`，含 summary 统计 + 逐条 finding，便于 CI 集成。 | — |

### 关键设计：可测试性

- **HTTP 层可注入**：`RegistryClient(fetcher=...)`，测试用桩 fetcher 返回预置 JSON，
  **全部 38 个测试离线运行**（0.1s）。
- **缓存时钟可注入**：`now=lambda: ...`，无需 `sleep` 即可测 TTL 过期。
- **registry 客户端可注入到 detector**：端到端测试用 `FakeClient` 构造各种边界
  （declared-not-found / imported-not-found / typosquat / 新包 / 未声明 / 干净包）。
- **transient 与 confirmed-missing 区分**：404 → `exists=False`（判幻觉）；超时/5xx →
  `None`（未知，不误判）。

### 与主流水线的衔接

`security.py` 的 `run_hallucinated_deps(repo, ...)` 改成一个**薄适配器**：调用新模块的
`HallucinationDetector().detect_path(repo.root)`，把更丰富的 finding 映射回流水线的
`SecurityFinding`（规则号 `VG-DEP-001..006`），路 C 的接线与报告保持不变。

### 验证记录（本轮实跑）

```
# 单元测试
$ pytest vibe_guard/hallucheck/tests/ -q
38 passed in 0.06s

# 独立 CLI（离线启发式）—— 造的假项目
$ python -m vibe_guard.hallucheck /tmp/hx --no-registry
⛔ HIGH   typosquat         'requets' ~ 'requests'
⚠️ MEDIUM undeclared_import 'coollib-undeclared' / 'numpy'

# 独立 CLI（实连 PyPI + npm registry）
$ python -m vibe_guard.hallucheck /tmp/hx --ecosystem pypi,npm
🛑 CRITICAL declared_not_found  npm 'leftpad-fake-npm-zzz'        (404)
🛑 CRITICAL imported_not_found  pypi 'coollib-undeclared'         (404)
🛑 CRITICAL declared_not_found  pypi 'leftpad-totally-fake-...'   (404)
🛑 CRITICAL declared_not_found  pypi 'requets'                    (404, 且 typosquat)
⛔ HIGH     typosquat           'requets' ~ 'requests'
⚠️ MEDIUM   undeclared_import   'numpy' (confirmed on registry)
→ checked=8 cache_hits=0  ；二次运行 cache_hits=6（缓存生效）

# 主流水线集成（Route C）
$ python -m vibe_guard scan /tmp/hx --no-align --no-trivy
hallucinated-dep   ok   findings=6   →   crit/high/med = 4/1/1
```

### 后续（v0.3 方向）

- typo-squat 热门包表自动从 registry top-N 拉取并缓存（当前为精选静态表，高精度优先）；
- PyPI 下载量信号（接入 pypistats / BigQuery）对齐 npm；
- 把 registry 查询并发化（线程池）以缩短大仓扫描时延；
- 维护者信誉 / 包签名（Sigstore）等更强的供应链信号。

---

## 交付物清单（本轮）

| 文件 | 内容 |
|---|---|
| `ARCHITECTURE-OVERVIEW-cn.md` | 任务 1：架构简介 + ASCII 流程图 |
| `README.md` | 已更新为中文版（含 v0.2 用法） |
| `pyproject.toml` | v0.2，新增 `vibe-guard-deps` 入口、`hallucheck` 包、`dev` extra |
| `vibe_guard/hallucheck/**` | 任务 3：幻觉检测独立模块（10 源文件 + 6 测试，38 用例全过） |
| `vibe_guard/security.py` | 改为调用新模块的薄适配器 |
| `vibe-guard-v0.2-summary.md` | 本总结文件 |
| GitHub `Rossery/vibe-guard@main` | 任务 2：commit `38e4a889`（+ 本文件 follow-up） |

_本轮为单轮 headless 运行，全部工作在本轮内完成。_
