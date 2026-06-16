# Vibe Guard — 架构设计文档

> 一个开源的「AI 生成代码端到端验证工具」。
> 验证 vibe coding（用自然语言让 AI 自动生成代码仓库）的产物：**它实现的，是不是你要的？实现的部分，对不对、稳不稳？安不安全？**
>
> 文档版本：v0.1（架构设计草案）  ·  日期：2026-06-16  ·  状态：设计评审

---

## 0. 设计哲学（必须先读）

Vibe Guard 不是又一个 SAST 扫描器，也不是又一个 LLM 代码审查机器人。它是一个**轻量编排层（orchestrator）**，把成熟的开源重工具串成一条「纵深防御」验证流水线，并用执行结果（而非 LLM 的主观判断）作为最终判据。

四条不可动摇的原则：

1. **不重复造轮子（integrate, don't reinvent）**
   所有重活——SAST、SCA、密钥扫描、代码解析、测试执行——全部外挂成熟开源工具（Semgrep / Trivy / Gitleaks / OSV-Scanner / tree-sitter / Qodo Cover-Agent）。我们只写「编排 + prompt + 证据聚合 + 报告」。

2. **execution > opinion（执行优先于观点）**
   借鉴 SWE-bench 的核心信条：**LLM 负责提出「该测什么」，真实执行决定「是否通过」。** 任何「功能已实现」的结论，最高可信度来自「为它合成的测试在隔离沙箱里跑绿了」，而不是 LLM 读了代码觉得对。

3. **轻量编排层（thin orchestration）**
   Vibe Guard 自身代码量应尽可能小。判断一个特性该不该自研的标准：它是不是「编排 / prompt / 证据归一化 / 报告」？是 → 自研；否（是分析/执行/解析引擎）→ 外挂。

4. **纵深防御 + 证据链（defense in depth, everything is evidence）**
   三路验证（功能对齐 / 测试执行 / 安全扫描）互补且互相校验。每一条结论都必须挂上**可复核的证据**（文件:行号、测试日志、扫描器原始命中、registry 查询结果）与**置信度**。没有证据的结论不进报告。

---

## 1. 问题定义

Vibe coding 产物有三类独立风险，对应三个独立的验证问题：

| # | 问题 | 难点 | Vibe Guard 的对策 |
|---|---|---|---|
| Q1 | **它实现的，是不是你要的？**（功能对齐 / spec alignment） | "规格"常只是一段自然语言、对话历史，甚至不存在 | 需求归一化 → 离散功能点 → 逐条核验（路 A + 路 B） |
| Q2 | **实现的部分，对不对、稳不稳？**（正确性 / 鲁棒性） | "演示能跑、生产即崩"，零错误处理 | 合成测试在沙箱执行 + 属性测试 + 变异测试（路 B） |
| Q3 | **它安不安全？**（安全 / 供应链） | 6/8 工具默认输出含严重漏洞；~20% AI 代码引用不存在的包 | SAST + SCA + 密钥 + 幻觉依赖检测（路 C） |

**输入**：一个代码仓库（本地路径或 Git URL）+ 需求描述（PRD / README / 一段自然语言 / 对话记录，**可选**——缺失时从代码反推）。

**输出**：一份带证据、可复核、可量化评分的验证报告（Markdown / HTML / JSON）。

---

## 2. 可量化目标

Vibe Guard 输出 4 个一级指标，每个都有明确定义、计算方式与证据来源。**所有分数都必须可追溯到原始证据**。

### 2.1 功能对齐覆盖率（Functional Alignment Coverage）

> 回答 Q1：声称的功能，逐条核验实现到什么程度。

需求归一化后得到 N 个**离散功能点（requirement items）**。每个功能点被判定为四态之一：

| 状态 | 含义 | 计权 |
|---|---|---|
| `IMPLEMENTED` | 已实现，且有证据（代码定位 + 通过的测试） | 1.0 |
| `PARTIAL` | 部分实现（如缺边界/异常处理、缺子流程） | 0.5 |
| `MISSING` | 找不到对应实现 | 0.0 |
| `UNVERIFIABLE` | 无法验证（如依赖外部服务、需人工） | 不计入分母 |

```
功能对齐覆盖率 = Σ(权重 · 功能点) / (可验证功能点总数)
              = (#IMPLEMENTED·1.0 + #PARTIAL·0.5) / (N − #UNVERIFIABLE)
```

报告同时给出**逐条核对表**：`功能点 | 状态 | 证据(文件:行) | 支撑测试 | 置信度`。

### 2.2 测试执行通过率（Test Execution Pass Rate）

> 回答 Q2：自动合成的测试，在隔离沙箱里真实跑通的比例。这是 execution-based 的硬判据。

```
测试执行通过率 = 通过的测试数 / 成功执行的测试总数
```

伴随两个质量护栏，防止「测试形同虚设」：

- **覆盖率增量（coverage delta）**：合成测试带来的行/分支覆盖率（借 Cover-Agent，只保留「能通过 + 真正提升覆盖率 + 非冗余」的测试）。
- **变异得分（mutation score）**：故意注入变异体，看测试能杀掉多少。`mutation_score = killed / total_mutants`。**高覆盖率 ≠ 能抓 bug，变异得分才衡量「捉虫力」**。

> 注意：分母是「成功执行的测试」。环境搭建失败 / 测试无法运行的情况单独统计为 `INFRA_FAILURE`，不污染通过率，但会拉低整体可信度。

### 2.3 安全漏洞检出率（Security Findings）

> 回答 Q3。这是「检出 + 严重度分级」，不是「检出率/召回率」（我们没有 ground truth）。

四类来源聚合、去重后按 CVSS/严重度分桶：

| 类别 | 工具 | 输出 |
|---|---|---|
| SAST（代码漏洞） | Semgrep（+ Bandit / CodeQL 可选） | `Critical / High / Medium / Low` 计数 + 每条命中的规则/文件:行 |
| SCA（依赖 CVE） | Trivy / OSV-Scanner | 受影响包、CVE、修复版本 |
| 密钥泄露 | Gitleaks（+ TruffleHog 可选验活） | 命中类型、文件:行（值脱敏） |
| 幻觉依赖 / slopsquatting | 自研 registry 校验器 | 不存在/过新/可疑包清单 |

派生一个 **安全分（Security Score, 0–100）**：从 100 起扣分，按严重度加权扣（如 Critical −25、High −10、Medium −3、Low −1，密钥命中 −20，幻觉依赖 −15），下限 0。权重在配置中可调。

### 2.4 报告可信度分数（Report Confidence）

> 元指标：这份报告本身有多可信？

每条结论带 `confidence ∈ [0,1]`，由证据强度决定（执行证据 > 静态证据 > 纯 LLM 判断），并经对抗式复核（多 Judge 投票）调整。整体可信度由以下因素加权：

- 有多大比例的功能点结论由**执行证据**支撑（而非纯 LLM）；
- 沙箱环境搭建成功率（`INFRA_FAILURE` 越多，可信度越低）；
- 对抗式复核中 Judge 的一致性（分歧大 → 降可信度）；
- 工具运行完整度（某扫描器崩了 → 对应维度可信度打折）。

报告头部醒目展示：**「本报告可信度：XX% — 其中 Y% 的功能结论有执行验证支撑」**，避免「LLM 自说自话」被误读为铁证。

---

## 3. Agent 架构设计

### 3.1 Agent Loop（主编排循环）

主 Agent（Orchestrator）是一个**确定性状态机**，不是自由发挥的 ReAct loop——可复现、可审计是验证工具的生命线。LLM 只在「需要语义理解」的节点被调用（归一化、提出测什么、聚合判断），流程控制是硬编码的。

```
                    ┌──────────────────────────────┐
   输入             │  Phase 0: Ingest & Normalize  │   ← LLM 节点
 (repo + 需求)  ──▶ │  仓库勘探 + 需求归一化          │
                    └───────────────┬──────────────┘
                                    │ 结构化需求清单 (RequirementSpec)
                                    ▼
              ┌──────────── 并行 fan-out（三路）────────────┐
              ▼                     ▼                       ▼
       Phase A: 功能对齐      Phase B: 测试合成执行    Phase C: 安全扫描
       (Alignment Agent)     (Test Agent + 沙箱)      (Security Agents)
        LLM + 代码图谱         LLM 提案 + 真实执行       纯工具，无 LLM
              │                     │                       │
              └──────────┬──────────┴───────────┬───────────┘
                         │ 三路证据 (Evidence[])
                         ▼
                 ┌────────────────────────────┐
                 │  Phase 4: Aggregate & Judge │   ← LLM 节点 + 对抗复核
                 │  证据聚合 + LLM-as-Judge     │
                 └──────────────┬─────────────┘
                                ▼
                 ┌────────────────────────────┐
                 │  Phase 5: Report & Gate     │
                 │  报告渲染 + 安全闸判定        │
                 └────────────────────────────┘
```

主循环伪代码：

```python
def run(repo, requirements, config):
    ctx = Context(repo, config)

    # Phase 0 — 串行，后续都依赖它
    spec = normalize(repo, requirements, ctx)          # LLM
    repo_map = build_repo_map(repo)                     # tree-sitter，纯工具

    # Phase A/B/C — 并行 fan-out，独立失败隔离
    evidence = run_parallel([
        lambda: align_features(spec, repo_map, ctx),    # 路 A
        lambda: synth_and_run_tests(spec, ctx),         # 路 B（最重，最慢）
        lambda: security_scan(repo, ctx),               # 路 C（工具，可秒级并发）
    ], timeout=config.phase_timeout, isolate_failures=True)

    # Phase 4 — 聚合 + 对抗式复核
    verdicts = judge(spec, evidence, ctx)               # LLM-as-Judge + 多投票

    # Phase 5 — 报告 + 安全闸
    report = render(spec, evidence, verdicts, config)
    gate   = evaluate_gate(verdicts, config.gate_policy)
    return report, gate
```

设计要点：
- **确定性优先**：流程控制硬编码；LLM 调用点固定且有 schema 约束输出（强制结构化）。
- **失败隔离**：三路任意一路失败/超时，不拖垮其余两路；缺失的那路在报告里标 `UNAVAILABLE` 并降低该维度可信度。
- **幂等可复现**：同一输入 + 同一 config + 固定随机种子，尽量复现同一报告（LLM 温度调低、记录所有 prompt/response 到 evidence store）。

### 3.2 多 Agent 协作

| Agent | 职责 | 输入 | 输出 | 用 LLM？ |
|---|---|---|---|---|
| **Orchestrator** | 编排全流程、调度、超时/失败处理、聚合调用 | repo + 需求 | 报告 + gate | 否（状态机） |
| **Normalizer Agent** | 把模糊需求/对话/README/代码反推 → 结构化离散功能点（RequirementSpec） | 需求文本 + repo_map | `RequirementSpec`（功能点列表 + 验收标准） | 是 |
| **Alignment Agent**（路 A） | 逐条核对「代码是否实现了功能点 X」，给状态 + 证据（文件:行）；跨文件逻辑审查（PR-Agent 思路） | spec + repo_map + 代码 | `AlignmentEvidence[]` | 是 |
| **Test Agent**（路 B） | 为每个功能点提出测试预言（正常/边界/异常），生成单测/属性测试 | spec + 代码 | 测试代码 + 运行计划 | 是（只提案） |
| **Sandbox Runner**（路 B） | 在隔离环境搭建依赖、跑测试、收集结果/覆盖率/变异得分 | 测试代码 + repo | `TestEvidence[]`（pass/fail/log/coverage） | 否（执行引擎） |
| **Security Agents**（路 C） | 跑 Semgrep/Trivy/Gitleaks/OSV + 幻觉依赖校验，归一化输出 | repo | `SecurityFinding[]` | 否（工具 wrapper） |
| **Judge Agent**（Phase 4） | 聚合三路证据，对每个功能点定状态 + 置信度；对高风险结论做对抗式复核 | 全部 evidence | `Verdict[]` + 可信度 | 是（多 Judge 投票） |
| **Reporter** | 把 verdicts + evidence 渲染成 MD/HTML/JSON；安全闸判定 | verdicts | 报告文件 + 退出码 | 否 |

**通信契约**：Agent 之间**不直接对话**，只通过结构化数据对象（Pydantic/JSON Schema 校验）传递。所有中间产物写入 **Evidence Store**（一个 run 目录，见 §5.2），保证可复核、可断点续跑。这避免了多 Agent 自由对话带来的不可复现与 token 浪费。

**对抗式复核（adversarial verify）**：Judge 对「高风险/高影响」结论（如「功能 X 已实现」「无严重漏洞」）生成 N 个独立 skeptic Judge，每个被要求**尝试反驳**，多数反驳成功则翻转结论。降低单次 LLM 幻觉风险。

### 3.3 任务调度（并行 / 串行 / 超时 / 失败）

```
串行段：Phase 0（Ingest+Normalize）→ 必须先有 RequirementSpec 和 repo_map
  │
  ├─ 并行 fan-out：Phase A / B / C 三路同时跑
  │     ├─ 路 C（安全扫描）最快：多个扫描器本身可并发（Semgrep/Trivy/Gitleaks 互不依赖）
  │     ├─ 路 A（功能对齐）中等：功能点之间可并行核对（按功能点 fan-out）
  │     └─ 路 B（测试执行）最慢：环境搭建是瓶颈；按功能点 pipeline，沙箱并发受资源限制
  │
汇聚 barrier：三路全部结束（或超时/失败）后才进入 Phase 4
  │
串行段：Phase 4（Judge）→ Phase 5（Report+Gate）
```

调度策略：
- **路内并行**：路 A 按功能点 fan-out；路 C 按扫描器 fan-out；路 B 按功能点 pipeline（合成→搭环境→跑→收集），沙箱实例数受 `max_sandboxes` 限制。
- **超时分级**：每个工具/每个 Agent 调用有独立超时；阶段有总超时。超时的子任务标记 `TIMEOUT`，不阻塞汇聚。
- **失败隔离**：单个功能点核对失败 / 单个扫描器崩溃 → 局部降级，结果标记后继续。只有 Phase 0 失败才整体中止（无 spec 无法继续）。
- **资源治理**：沙箱执行是最耗资源的，设并发上限 + 磁盘/内存配额 + 网络隔离（防被测代码外联）。
- **可中断/续跑**：每阶段产物落盘到 Evidence Store，崩溃后可从最近 checkpoint 续跑。

---

## 4. 工具系统

### 4.1 外挂的成熟开源工具（重活全部外包）

| 能力 | 工具 | 角色 | 集成方式 |
|---|---|---|---|
| 代码解析 / 符号图 | **tree-sitter**（多语言） | 建符号表、调用图、import 清单（仿 Greptile 的整库图谱思路） | Python 绑定 `py-tree-sitter` |
| SAST（多语言） | **Semgrep** | 主力静态安全分析（~82% 准确率，规则易扩展，统一 SAST/SCA/Secrets） | CLI + JSON 输出 |
| SAST（Python 补强） | **Bandit** | Python 安全反模式 | CLI（可选） |
| SAST（深度污点，可选） | **CodeQL** | 高价值仓库的深度数据流分析（准确率最高、误报最低） | CLI（重，可选开启） |
| SCA（依赖漏洞） | **Trivy** | 一站式扫依赖/IaC/密钥/镜像 | CLI + JSON |
| SCA（精确 lockfile） | **OSV-Scanner** | 对接 OSV 库，精确匹配 lockfile 版本 | CLI + JSON |
| 密钥扫描 | **Gitleaks** | 硬编码密钥/凭证 | CLI + JSON |
| 密钥验活（可选） | **TruffleHog** | 验证泄露凭证是否仍有效 | CLI（可选） |
| 测试生成（单测） | **Qodo Cover-Agent** | 生成「能过 + 提覆盖率 + 非冗余」的测试 | CLI / 库 |
| 属性测试 | **Hypothesis**(Py) / **fast-check**(JS/TS) | 不变式/属性驱动证伪 | 作为生成测试的目标框架 |
| 变异测试 | **mutmut**(Py) / **StrykerJS**(JS/TS) | 算变异得分，验证测试「捉虫力」 | CLI |
| 跨文件逻辑审查 | **PR-Agent**（Qodo，开源） | 路 A 的跨文件 bug 审查组件（可选） | 库 / 自部署 |
| 沙箱隔离 | **Docker**（+ 语言镜像） | 隔离可复现执行环境（仿 SWE-bench++ environment synthesis） | Docker SDK |

### 4.2 需要自研的部分（只写编排/胶水/聚合）

| 模块 | 为什么自研 | 内容 |
|---|---|---|
| **Orchestrator / Agent Loop** | 这是产品本体 | 状态机、调度、超时/失败、checkpoint |
| **Normalizer**（需求归一化） | 没有现成「自然语言→离散功能点」组件 | prompt + schema 约束输出；从代码反推意图（TestSprite 思路） |
| **Repo Mapper** | 把 tree-sitter 产物组织成 LLM 友好的「仓库地图」 | 符号/调用图/import 清单 → 紧凑上下文 |
| **Hallucinated-Dependency Checker** | vibe coding 特有，无现成工具 | 解析依赖 → 查 PyPI/npm registry → 标记「不存在 / 过新 / 低下载量 / import 了但不在 lockfile」 |
| **Test Synthesizer**（提案层） | 把功能点 → 该测什么（test oracle 提案） | prompt + 测试模板；只提案，执行交给沙箱 |
| **Sandbox Runner** | 统一封装 Docker 执行 + 结果/覆盖率/变异收集 | 环境探测、依赖安装、跑测试、采集 |
| **Evidence Store + 归一化层** | 各工具输出格式各异，需统一证据模型 | 统一 `Finding`/`Evidence` schema + 去重/关联 |
| **Judge / Aggregator** | 证据 → 判定 + 置信度 + 对抗复核 | LLM-as-Judge + 多投票 + 评分公式 |
| **Reporter** | 渲染 + 安全闸 | MD/HTML/JSON 模板、gate 策略 |
| **Tool Adapters** | 每个外挂工具一个 wrapper | 调 CLI、解析输出、归一化、容错 |

**自研 / 外挂的边界判据**：「分析 / 执行 / 解析引擎」→ 外挂；「编排 / prompt / 证据归一化 / 报告」→ 自研。

### 4.3 Agent 可用的内部工具（tool calls）

Normalizer/Alignment/Judge 等 LLM Agent 通过受限的工具集与代码交互（不让 LLM 裸跑 shell）：

- `read_file(path, range)` / `list_dir(path)` —— 只读文件访问（限定在 repo 内）
- `search_code(query)` —— 基于 repo_map 的符号/正则检索
- `get_symbol(name)` —— 取某函数/类的定义 + 调用点
- `query_registry(pkg, ecosystem)` —— 查包是否存在（供幻觉依赖核验）
- `run_in_sandbox(cmd)` —— **仅** Sandbox Runner 可用，受隔离与配额约束

---

## 5. 整体结构设计

### 5.1 目录结构

```
vibe-guard/
├── ARCHITECTURE.md            # 本文档
├── README.md
├── pyproject.toml             # 依赖与打包（uv / hatchling）
├── vibe_guard/
│   ├── __init__.py
│   ├── cli.py                 # CLI 入口（typer）
│   ├── config.py              # 配置加载/校验（pydantic-settings）
│   ├── orchestrator/
│   │   ├── loop.py            # 主 Agent Loop（状态机）
│   │   ├── scheduler.py       # 并行/串行/超时/失败隔离
│   │   └── context.py         # 运行上下文 + Evidence Store 句柄
│   ├── ingest/
│   │   ├── repo_loader.py     # 拉取/定位仓库
│   │   ├── repo_mapper.py     # tree-sitter → 仓库地图
│   │   └── normalizer.py      # 需求归一化 → RequirementSpec
│   ├── routes/
│   │   ├── alignment/         # 路 A：功能对齐
│   │   ├── testing/           # 路 B：测试合成 + 沙箱执行
│   │   │   ├── synthesizer.py
│   │   │   ├── sandbox.py     # Docker runner
│   │   │   ├── coverage.py
│   │   │   └── mutation.py
│   │   └── security/          # 路 C：安全扫描
│   │       ├── sast.py        # semgrep/bandit/codeql adapters
│   │       ├── sca.py         # trivy/osv adapters
│   │       ├── secrets.py     # gitleaks/trufflehog
│   │       └── hallucinated_deps.py   # 自研幻觉依赖校验
│   ├── adapters/              # 外部工具 CLI wrappers（统一输出）
│   ├── evidence/
│   │   ├── models.py          # Evidence/Finding/Verdict schema (pydantic)
│   │   └── store.py           # 落盘/读取/去重/关联
│   ├── judge/
│   │   ├── aggregator.py      # 证据聚合
│   │   ├── judge.py           # LLM-as-Judge + 对抗复核
│   │   └── scoring.py         # 4 个一级指标计算
│   ├── report/
│   │   ├── renderer.py        # MD/HTML/JSON
│   │   ├── gate.py            # 安全闸策略
│   │   └── templates/
│   └── llm/
│       ├── client.py          # LLM 客户端封装（默认 Claude）
│       └── prompts/           # 所有 prompt 模板
├── configs/
│   └── default.yaml           # 默认配置（阈值/权重/工具开关）
├── tests/
└── examples/                  # 示例 vibe-coded 仓库 + 期望报告
```

### 5.2 各模块职责（一句话）

- **cli / config**：用户接口与配置加载。
- **orchestrator**：流程控制中枢（状态机 + 调度 + 上下文）。
- **ingest**：仓库与需求的「输入归一化」——产出 `RepoMap` 和 `RequirementSpec`。
- **routes/{alignment,testing,security}**：三路验证，各产出 `Evidence[]`。
- **adapters**：每个外部工具一个统一 wrapper（调用 + 解析 + 容错）。
- **evidence**：统一证据模型 + 持久化（Evidence Store，是可复核的根基）。
- **judge**：证据 → 判定 + 置信度 + 评分 + 对抗复核。
- **report**：渲染报告 + 安全闸退出码。
- **llm**：LLM 客户端 + prompt 集中管理。

### 5.3 数据流

```
代码仓库 + 需求
   │
   ▼  ingest
RepoMap（tree-sitter 符号/调用图/import） + RequirementSpec（离散功能点[]）
   │
   ├──▶ 路 A ──▶ AlignmentEvidence[]   (功能点 → 实现定位 + 跨文件审查)
   ├──▶ 路 B ──▶ TestEvidence[]        (功能点 → 合成测试 → 沙箱执行结果/覆盖/变异)
   └──▶ 路 C ──▶ SecurityFinding[]     (SAST/SCA/密钥/幻觉依赖)
   │
   ▼  evidence.store（全部落盘，统一 schema，去重 + 关联到功能点）
Unified Evidence Graph
   │
   ▼  judge（聚合 + LLM-as-Judge + 对抗复核）
Verdict[]（每功能点状态 + 置信度） + 风险清单 + 4 个一级指标分数
   │
   ▼  report
report.{md,html,json}  +  gate 退出码（0 通过 / 非 0 拦截）
```

**核心约束**：每个 `Verdict` 必须能反向追溯到 `Evidence`，每个 `Evidence` 必须能追溯到原始工具输出/代码定位。证据链断裂的结论不出现在报告里。

### 5.4 配置系统

`configs/default.yaml`，支持仓库内 `.vibe-guard.yaml` 覆盖 + CLI flag 覆盖（优先级：CLI > 项目配置 > 默认）。

```yaml
llm:
  provider: anthropic
  model: claude-opus-4-8        # 归一化/对齐/判定用强模型；可为不同 Agent 分别配置
  judge_model: claude-sonnet-4-6
  temperature: 0.0              # 追求可复现
  max_workers: 4

routes:
  alignment: { enabled: true }
  testing:
    enabled: true
    sandbox: docker
    max_sandboxes: 4
    run_mutation: true
    run_property_tests: true
  security:
    sast:   { semgrep: true, bandit: auto, codeql: false }
    sca:    { trivy: true, osv: true }
    secrets:{ gitleaks: true, trufflehog: false }
    hallucinated_deps: true

scoring:
  security_weights: { critical: 25, high: 10, medium: 3, low: 1, secret: 20, hallucinated_dep: 15 }

gate:                            # 安全闸（VibeGuard 思路）：未达标退出码非 0
  fail_on:
    security_critical: ">=1"
    secrets: ">=1"
    alignment_coverage: "<0.6"

timeouts:
  phase_seconds: 1800
  tool_seconds: 300

report:
  formats: [markdown, html, json]
  redact_secrets: true
```

---

## 6. 技术栈选型

| 维度 | 选型 | 理由 |
|---|---|---|
| **主语言** | Python 3.11+ | 生态最贴近目标工具（Semgrep/Bandit/Hypothesis/mutmut/Cover-Agent 都是 Python 友好），AST/tree-sitter 绑定成熟，LLM SDK 一流 |
| **依赖管理** | uv + pyproject.toml | 快、可锁定、可复现 |
| **CLI 框架** | Typer | 类型友好、自动帮助、子命令清晰 |
| **数据建模/校验** | Pydantic v2 | 统一证据/配置 schema，强制 LLM 结构化输出 |
| **LLM 客户端** | Anthropic SDK（默认 Claude Opus 4.8 / Sonnet 4.6） | 默认用最新最强模型；client 层做 provider 抽象，便于接其他模型 |
| **代码解析** | tree-sitter（py-tree-sitter） | 多语言、增量、稳定 |
| **沙箱** | Docker（SDK） | 隔离、可复现、SWE-bench 同款思路；网络隔离防外联 |
| **并发** | asyncio（IO/工具调用） + 进程池（CPU/沙箱） | 工具调用是 IO 密集，沙箱是资源密集 |
| **报告渲染** | Jinja2（MD/HTML）+ 直出 JSON | 模板化、易定制 |
| **测试** | pytest | 标准 |

### 6.1 CLI 设计

```bash
# 最常用：验证一个仓库（需求从 README/代码反推）
vibe-guard scan ./my-app

# 指定需求文件
vibe-guard scan ./my-app --requirements ./PRD.md

# 验证远程仓库 + 只跑安全路 + 输出 HTML
vibe-guard scan https://github.com/user/repo --only security --format html

# 作为 CI gate（命中阈值则退出码非 0）
vibe-guard scan ./my-app --gate --format json -o report.json

# 子命令
vibe-guard scan        # 全流程
vibe-guard normalize   # 只做需求归一化，产出功能点清单（便于人工确认）
vibe-guard report      # 从已有 evidence store 重新渲染报告
vibe-guard doctor      # 检查外部工具（semgrep/trivy/docker...）是否就绪
```

### 6.2 输出格式

- **Markdown**：人读首选，带逐条核对表、风险清单、证据链、修复建议。
- **HTML**：可分享的富报告（折叠证据、严重度配色、可信度仪表盘）。
- **JSON**：机器可读，供 CI / 二次处理 / 趋势追踪；是 evidence store 的导出视图。

报告结构（统一）：
1. **执行摘要**：4 个一级指标 + 整体可信度 + gate 结论。
2. **功能对齐核对表**：逐条状态 + 证据 + 置信度。
3. **测试执行结果**：通过率 / 覆盖率 / 变异得分 + 失败用例日志。
4. **安全与供应链**：SAST/SCA/密钥/幻觉依赖，按严重度排序 + 修复建议。
5. **证据附录**：原始工具输出、prompt/response、可复核命令。

---

## 7. MVP 路线图

借鉴调研的落地优先级：先把「能立刻产出价值」的薄切片跑通，再逐层加深「执行验证」。

### 阶段 1 — MVP（薄端到端切片）

**目标**：`vibe-guard scan ./repo` 能产出一份「功能点核对表 + 安全/依赖红线清单」的 Markdown 报告。

**做**：
- Ingest：repo loader + tree-sitter repo mapper（先支持 **Python**）。
- Normalizer：自然语言/README → 离散功能点（LLM + schema）。
- 路 A（轻量）：LLM 逐条功能对齐（纯静态读码判断，标证据文件:行）。
- 路 C：直接集成 **Semgrep + Trivy + Gitleaks + 自研幻觉依赖校验**（这层最成熟，性价比最高）。
- 聚合：简单 Judge（单次，无对抗）+ 4 指标基础版。
- Reporter：Markdown + JSON。
- CLI：`scan` / `normalize` / `doctor`。

**不做**：沙箱执行、属性/变异测试、对抗复核、HTML、CodeQL、多语言。

**价值**：立刻能回答「声称的功能有没有对应实现 + 有没有明显安全/供应链红线」。

### 阶段 2 — 加入执行验证（execution > opinion 落地）

- 路 B：Test Synthesizer + **Docker 沙箱** + Qodo Cover-Agent 集成；功能对齐从「LLM 判断」升级为「合成测试跑绿」。
- 测试执行通过率 + 覆盖率增量进入评分。
- 功能对齐结论开始引用执行证据，可信度显著提升。

### 阶段 3 — 加深测试质量

- **属性测试**（Hypothesis）暴露浅测试盖不到的缺陷。
- **变异测试**（mutmut）算变异得分，识别「形同虚设的测试」。
- 路 A 接入跨文件逻辑审查（PR-Agent 思路）。

### 阶段 4 — 提升可信度与可拦截

- **对抗式 Judge**（多投票）+ 报告可信度分数成熟化。
- **安全闸**（VibeGuard 思路）：`--gate` 可作 CI 拦截。
- HTML 报告 + 可信度仪表盘。
- 可选接入 **CodeQL** 深度污点分析。

### 阶段 5 — 扩展（可选/重型）

- 多语言扩展（JS/TS：fast-check + StrykerJS；Java 等）。
- 形式化验证 / Vericoding 接入高价值核心逻辑。
- MCP server 化，挂进 Cursor / Claude Code 等 AI IDE，形成「生成→验证→回喂」闭环。

---

## 8. 参考与致谢（设计来源）

本架构站在以下工作的肩膀上：

- **评测哲学**：SWE-bench / SWE-bench++（execution-based）、HumanEval。
- **需求对齐**：PRDBench / PRDJudge（需求→离散指标→Agent 逐条核验 + 专用 Judge）、TestSprite（从代码反推意图、云沙箱闭环）。
- **测试质量**：Qodo Cover-Agent（assured test gen）、Property-Based Testing（FSE 2025）、Mutation Testing。
- **安全**：Semgrep / CodeQL / Bandit / Trivy / OSV-Scanner / Gitleaks / TruffleHog；slopsquatting / 幻觉依赖研究；VibeGuard 安全门框架。
- **代码审查**：PR-Agent（开源）、Greptile（整库图谱思路）。
- **验证理念**：FCL 反馈式自动验证（细粒度可操作反馈 > 粗粒度打分）、OpenAI Scaling Code Verification（输出监控 + 纵深防御）。

> 一句话信条：**让 LLM 提出「该验证什么」，让真实执行决定「是否通过」。**
