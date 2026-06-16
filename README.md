# Vibe Guard

> 面向 AI 生成代码（"vibe coding" 产物）的端到端验证工具。
> **当前：v0.1 MVP（Python）+ v0.2 幻觉依赖检测独立模块（Python & npm）**

现代 AI 编码 Agent 出代码很快，但有三类反复出现的失败模式：

1. **功能漂移** —— 代码并没有真正实现 README / 需求所声称的功能。
2. **正确性 / 鲁棒性** —— "演示能跑、生产即崩"，几乎没有错误处理。
3. **安全与供应链红线** —— 硬编码密钥、不安全模式，以及 **幻觉依赖**
   （import / requirements 指向 **根本不存在的包** —— "slopsquatting" 投毒诱饵）。

Vibe Guard 扫描一个仓库，产出一份 Markdown **验证报告**，回答：*这份代码做到了它声称的
事吗？能安全上线吗？*

## 架构与流程

完整的「一段话简介 + 数据流流程图」见
[`ARCHITECTURE-OVERVIEW-cn.md`](./ARCHITECTURE-OVERVIEW-cn.md)；
完整架构设计文档见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。

整体是一个**确定性状态机编排层**，5 阶段流水线：
**Ingest（tree-sitter 采集）→ Normalize（LLM 需求归一化）→ 三路并行验证
（路 A 功能对齐 / 路 B 测试执行 / 路 C 安全与供应链）→ Judge（裁决）→ Report（报告）**。
重活全部外挂成熟开源工具（**Semgrep** / **Trivy** / **Gitleaks** / **tree-sitter** /
**Qodo Cover-Agent**），我们只写编排、prompt、证据聚合与报告。

核心哲学 **execution > opinion**：LLM 提出「该测什么」，**真实执行**（测试跑绿、扫描命中、
registry 查询）决定「是否通过」。

## 自研亮点：幻觉依赖检测（`vibe_guard.hallucheck`）

解析项目里**真实的依赖声明**（pyproject.toml / setup.py / requirements.txt /
package.json）与源码 import，逐包查询 **PyPI / npm registry**，标记不存在/可疑的包，
专治 AI 代码特有的 slopsquatting。v0.2 把它抽成了一个**独立、零三方依赖、可单跑**的模块：

- **幻觉包检测**：声明/import 的包在 registry 上 404 → `CRITICAL`
- **typo-squat 模糊匹配**：`requets` vs `requests`、`beautfulsoup4` vs `beautifulsoup4`
- **新包/低星告警**：查首次发布时间与下载量，异常新/冷门的包标记为可疑
- **npm 支持**：JS/TS 项目的 npm registry 检查
- **本地缓存**：registry 查询结果带 TTL 缓存，避免重复 API 调用
- **可配置严重度** + **JSON 输出**（方便 CI 集成）

## 安装

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .            # 核心
pip install semgrep         # 可选，路 C SAST
# gitleaks / trivy: 把二进制放到 PATH 上（可选）
```

## 用法

### 完整扫描

```bash
export DEEPSEEK_API_KEY=sk-...           # OpenAI 兼容端点（默认 DeepSeek）
vibe-guard scan path/to/repo -o report.md
```

常用开关：`--requirements <文件|文本>`、`--no-align`（跳过 LLM / 路 A）、
`--no-pypi`（跳过 registry 存在性检查）、`--no-trivy`、`--model` / `--base-url` / `--api-key`。

### 只跑幻觉依赖检测（独立子命令，适合 CI）

```bash
vibe-guard-deps path/to/repo --json                 # JSON 输出
vibe-guard-deps path/to/repo --ecosystem pypi,npm   # 选择生态
vibe-guard-deps path/to/repo --no-registry          # 纯离线启发式
python -m vibe_guard.hallucheck path/to/repo        # 等价调用
```

`--fail-on critical|high|medium|low` 控制何时返回非零退出码，便于流水线门禁。

## 测试

```bash
pytest vibe_guard/hallucheck/tests/      # 幻觉检测模块的单元测试（38 个用例）
```

## 状态

- **v0.1 MVP**：5 阶段流水线全部跑通（路 A + 路 C），在三个真实开源 Python 项目上评估，
  见 [`vibe-guard-v0.1-results-cn.md`](./vibe-guard-v0.1-results-cn.md)。
- **v0.2**：幻觉依赖检测抽取为独立模块 `vibe_guard/hallucheck/`，新增 typo-squat、
  新包告警、npm 支持、缓存、可配置严重度、独立 CLI 与 JSON 输出，见
  [`vibe-guard-v0.2-summary.md`](./vibe-guard-v0.2-summary.md)。
