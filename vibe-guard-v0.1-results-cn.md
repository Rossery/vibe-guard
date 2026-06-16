# Vibe Guard v0.1 —— 实现与评估结果

_日期：2026-06-16 · `ARCHITECTURE.md` 中 MVP 的第 1 阶段 · LLM 后端：DeepSeek
（`deepseek-chat`，OpenAI 兼容端点）_

---

## 1. MVP 实现概述

Vibe Guard v0.1 是一个可运行的 CLI 工具，它接收一个 Python 代码仓库，并输出一份
单一的 Markdown **验证报告**，将*功能一致性*检查清单（代码是否实现了
README/需求所声称的功能？）与*安全与依赖红线*结合在一起。

### 流水线（五个阶段全部实现）

| 阶段 | 模块 | 功能说明 |
|---|---|---|
| 1. 采集（Ingest） | `ingest.py` | 遍历仓库，为 Python 构建一张 **tree-sitter** 符号图（包含函数 / 类 / 方法的签名、docstring 和调用引用），并收集 README + 依赖清单文件。 |
| 2. 归一化（Normalize） | `normalizer.py` | LLM 将 README + 可选的用户需求转化为一份 **`RequirementSpec`** —— 一组离散、可测试的功能点列表。 |
| 3. 路线 A —— 功能一致性 | `align.py` | 逐个功能点：检索候选符号（关键词重叠 + 入口点启发式），拉取它们的**真实源码片段**，并让 LLM 判定 `implemented / partial / missing / unclear`（已实现 / 部分实现 / 缺失 / 不明确），并附上**基于片段、落到 `file:line` 的证据**。 |
| 4. 路线 C —— 安全与依赖 | `security.py` | 运行 **Semgrep**（registry 规则包）、**Gitleaks**（密钥）、**Trivy**（CVE，可选）以及一个自研的**幻觉依赖（hallucinated-dependency）**检测器（对声明/导入的包做 PyPI 存在性检查）。 |
| 5. 汇总 / 报告 | `report.py`、`cli.py` | 将所有结果汇总成一份 Markdown 报告，包含一个总判定（`PASS` / `PASS WITH WARNINGS` / `NEEDS REVIEW`）、一张功能检查清单表，以及按严重程度分组的安全发现。 |

### 目录结构

```
vibe-guard-mvp/
├── README.md
├── pyproject.toml                 # 可安装：`vibe-guard` 入口点
├── vibe-guard-v0.1-results.md     # 本文件
├── vibe_guard/                    # 约 1,580 行代码
│   ├── __init__.py
│   ├── __main__.py                # python -m vibe_guard
│   ├── cli.py                     # typer + rich CLI（`scan`、`version`）
│   ├── models.py                  # pydantic v2 模型（图、spec、findings、report）
│   ├── llm.py                     # OpenAI 兼容客户端（DeepSeek）+ JSON 解析
│   ├── ingest.py                  # tree-sitter 符号图
│   ├── normalizer.py              # README/需求 → RequirementSpec
│   ├── align.py                   # 路线 A：检索 + LLM 功能一致性
│   ├── security.py                # 路线 C：semgrep/gitleaks/trivy/幻觉依赖
│   └── report.py                  # Markdown 报告渲染器
├── test_projects/                 # 三个被评估的仓库（已内置）
│   ├── python-slugify/            # 小型
│   ├── itsdangerous/              # 中型
│   └── click/                     # 大型
├── reports_slugify.md             # 各项目完整报告
├── reports_itsdangerous.md
└── reports_click.md
```

### 技术栈

- **解析：** `tree-sitter` 0.25 + `tree-sitter-python` 0.25
- **模型 / CLI：** `pydantic` v2、`typer`、`rich`
- **LLM：** `openai` SDK 指向 DeepSeek（`base_url=https://api.deepseek.com/v1`，`model=deepseek-chat`）
- **安全工具链：** Semgrep 1.166（规则包 `p/python`、`p/security-audit`、`p/secrets`）、Gitleaks 8.18.4、Trivy（可选 —— 见 §4）、自研依赖检测器。

### 幻觉依赖检测器（自研，路线 C）

这是最针对 *AI 生成* 代码的组件。它会：

1. **仅**解析**真实的**依赖声明 —— `requirements*.txt`、
   `pyproject.toml` 中的 `[project].dependencies` / `optional-dependencies` / `build-system.requires`
   （通过 `tomllib`）、`setup.py` 中的 `install_requires`/`extras_require`，
   以及 `setup.cfg`。（v0.1 *第一版* 曾天真地 grep 每一个带引号的字符串，
   从而产生了误报 —— 见 §5；该问题已修复。）
2. 提取顶层第三方导入（排除标准库和本地包），
   并映射常见的 导入名→发行包名 别名（`yaml`→`pyyaml`、`cv2`→`opencv-python`、……）。
3. 对每一项查询 **PyPI JSON API**：
   - 声明的包返回 404 → **CRITICAL（严重）**（`VG-DEP-001`，幻觉/抢注式（slopsquatted）清单条目）；
   - 导入的模块其发行包返回 404 → **CRITICAL（严重）**（`VG-DEP-002`）；
   - 导入但未声明的第三方模块 → **MEDIUM（中）**（`VG-DEP-003`），当仅在测试/文档/示例代码中使用时降级为 **INFO（提示）**。

---

## 2. 评估集

通过 GitHub codeload tarball 端点获取了三个真实的开源 Python 项目，
并按非测试代码行数（LOC）分桶：

| 分桶 | 项目 | 非测试 LOC | Py 文件 | 符号 | README |
|---|---|---|---|---|---|
| **小型**（<500） | [`un33k/python-slugify`](https://github.com/un33k/python-slugify) | 444 | 7 | 99 | ✅ |
| **中型**（500–5000） | [`pallets/itsdangerous`](https://github.com/pallets/itsdangerous) | 1,231 | 15 | 144 | ✅ |
| **大型**（>5000） | [`pallets/click`](https://github.com/pallets/click) | 12,493 | 63 | 1,838 | ✅ |

每个项目均使用 `vibe-guard scan <repo> --no-trivy -o report.md` 进行扫描。
三次运行的实际耗时均在 **75 秒以内**，且各自仅需 **13–14 次 LLM
调用**（一次 normalize 调用 + 每个功能点一次）。

---

## 3. 各项目验证结果

### 3.1 小型 —— `python-slugify` &nbsp; 判定：✅ PASS

> _"一个 Python 库及 CLI 工具，用于从 Unicode 字符串生成 URL 友好的 slug，
> 并提供丰富的配置选项。"_

- **功能一致性：** **13 / 13 个功能点已实现**（0 个部分实现，
  0 个缺失）。每一项 README 功能 —— Unicode 转写、`allow_unicode`、
  HTML 实体转换、最大长度截断、自定义分隔符、停用词、
  自定义正则/替换、大小写控制、`save_order`，以及 CLI（包括
  `--` 多值分隔符）—— 都在 `slugify/slugify.py` 和测试套件中匹配到了具体的 `file:line` 证据。
- **安全 / 依赖：** 0 个严重，0 个高危，**2 个中危**：
  - Semgrep：`insecure-hash-algorithm-sha1`（SHA-1 用于一个非安全用途的 slug 哈希 —— 就模式而言是真阳性，实际风险很低）。
  - `VG-DEP-003`：`unidecode` 被导入，但仅声明为一个*额外依赖*（`extras_require`），因而被标记为未在基础依赖中固定。

> 这是理想的顺利路径：一个小型、忠实的库干净地通过。

### 3.2 中型 —— `itsdangerous` &nbsp; 判定：✅ PASS

> _"一个用于对数据进行加密签名的库，使数据能够安全地穿越不可信环境，
> 支持序列化、压缩、时间戳和 URL 安全编码。"_

- **功能一致性：** **11 / 12 已实现**，1 个 "缺失"。
  - 唯一被标记为 **缺失** 的项 —— *"Salt 支持"* —— 是一个 **假阴性（false negative）**：
    `itsdangerous` 确实在 `Signer` 上支持 `salt` 参数，但关键词
    检索器浮现了错误的符号，于是 LLM（在它所看到的内容下，这一判断是正确的）
    报告没有证据。见 §5。
- **安全 / 依赖：** 0 个严重，0 个高危，**2 个中危**（外加 3 个 INFO 级
  仅开发用途的导入，例如 `pytest`、`freezegun`）：
  - Semgrep：`exec-detected` —— 使用了 `exec()`（出现在带类型签名的 shim / 测试
    辅助代码中）；这是一个真正达到审计级别的信号。

### 3.3 大型 —— `click` &nbsp; 判定：🟡 PASS WITH WARNINGS（带警告通过）

> _"一个用于创建命令行界面的 Python 包，支持可组合的装饰器、
> 自动帮助页面，以及任意层级的命令嵌套。"_

- **采集（Ingest）平滑扩展：** 63 个文件 / 26,704 LOC / **1,838 个符号**，
  在几秒内完成解析。
- **功能一致性：** 9 个已实现，**1 个部分实现**，**2 个缺失**：
  - *"子命令的惰性加载"* 和 *"对缺失选项的提示"* 被
    标记为 **缺失**，而 *"回调执行顺序"* 被标记为 **部分实现** —— 这
    三项都是 **检索假阴性**。Click 实际上全部实现了它们；
    在 1,838 个符号的规模下，单趟关键词检索（每个功能取前 6 个候选）
    没能浮现出正确的代码。这是 v0.1 最突出的局限（§5），也是
    最清晰的信号 —— 说明检索必须更好地扩展。
- **安全 / 依赖：** **没有任何高于 INFO 的发现**。Semgrep 在
  这个成熟代码库上没有产生任何结果；唯一的依赖说明是 INFO 级的开发/测试
  导入（`pytest`、`pallets_sphinx_themes`、`typing_extensions`）。

### 跨项目汇总

| 项目 | 功能 ✅/🟡/❌/❓ | 安全 🟥/🟧/🟨 | 判定 | LLM 调用 | 实际耗时 |
|---|---|---|---|---|---|
| python-slugify | 13 / 0 / 0 / 0 | 0 / 0 / 2 | ✅ PASS | 14 | ~72 s |
| itsdangerous | 11 / 0 / 1 / 0 | 0 / 0 / 2 | ✅ PASS | 13 | ~51 s |
| click | 9 / 1 / 2 / 0 | 0 / 0 / 0 | 🟡 WARN | 13 | ~58 s |

**验证说明（合成测试夹具）：** 我们扫描了一个刻意构造的、被破坏的夹具
（`_smoke/`），其中包含一个硬编码的 AWS 密钥、一个幻觉包（`leftpadinator`）、
一个不存在的清单条目（`totally-not-a-real-pkg-xyz123`）以及一个缺失的
CLI，以确认真阳性行为：Gitleaks 标记了该密钥
（HIGH），依赖检测器标记了两个伪造的包（CRITICAL），而路线
A 正确地将那个缺失的功能标记为 `missing`。

---

## 4. 遇到的问题及处理方式

| # | 问题 | 解决方案 |
|---|---|---|
| 1 | **GitHub token 无效**（`401 Bad credentials`），REST API 和 git push 均受影响。 | 改为通过公开的 `codeload.github.com` tarball 端点获取测试项目。最终的 push **无法**通过认证 —— 见 §7。 |
| 2 | **通过 smart HTTP 协议的 `git clone` 超时**；只有 tarball 下载可用。 | 将所有仓库获取方式切换为 `codeload` tarball。 |
| 3 | **Trivy 二进制下载持续被截断**（8.5 MB / 段错误），且其漏洞数据库需要一次大规模的网络拉取。 | Trivy 已接入，并在可用时使用；但本次评估运行使用了 `--no-trivy`。在此期间，自研的依赖检测器覆盖了供应链维度。 |
| 4 | **Semgrep 的 `--config auto` 在禁用 metrics 时拒绝运行。** | 改用显式的 registry 规则包（`p/python`、`p/security-audit`、`p/secrets`），它们在不发送 metrics 的情况下也能工作。 |
| 5 | **幻觉依赖检测器有严重的误报** —— 其第一版会 grep `setup.py` / `pyproject.toml` 中的每一个带引号字符串，从而"发现"诸如 `console-scripts`、`utf-8`、`r` 之类的包，以及一个 `import … as` 产生的伪影 `as`。 | 重写清单解析逻辑，**仅**读取真实的依赖字段（`pyproject.toml` 用 `tomllib`；`setup.py` 用限定范围的 `install_requires`/`extras_require` 代码块），并修复了导入解析器以正确处理 `import a, b as c`。重新扫描后 slugify 的伪造 CRITICAL 从 8 个降到了 0 个。 |
| 6 | **`pip install` 被 PEP 668 阻止**（externally-managed 环境）。 | 创建了一个项目级 virtualenv。 |

---

## 5. 发现、局限性与改进设想

**做得好的地方**
- **采集（Ingest）** 可在数秒内扩展到约 1,800 个符号 / 27k LOC，且稳定可靠。
- **归一化器（Normalizer）** 产出干净、原子化、分类良好的功能点（每个项目 12–13
  个），读起来就像一份真实的验收检查清单。
- **路线 A 的证据是落地的**：因为 LLM 只看到真实、已定位的
  片段，它的 `file:line` 引用在每一次抽查中都准确无误 —— 没有
  幻觉出的文件路径。
- **依赖检测器**（修复后）精确，并能捕获那种现成
  SAST 工具会遗漏的、AI 特有的抢注（slopsquatting）失效模式。

**局限性（如实陈述）**
1. **检索是扩展时的瓶颈。** 每个功能取前 6 的关键词检索
   在大型仓库中漏掉了真实实现（click：惰性子命令、
   选项提示），甚至在中型仓库中也是如此（itsdangerous：`salt`）。因此，
   对一个成熟库报告的"缺失" **更可能是一次检索遗漏，
   而非真实的功能缺口**。*改进方向：* 基于嵌入（embeddings）的检索、
   围绕种子符号的调用图扩展，以及在宣布 `missing` 之前增加一个
   "你确定它真的缺失吗？"的二次验证趟次。
2. **单语言。** 仅支持 Python；符号图与导入分析需要
   各语言的 tree-sitter 文法才能泛化。
3. **本次运行未实际演练 Trivy/CVE 覆盖**（二进制 + 数据库拉取
   问题），因此运行时依赖的 CVE 目前是一个盲点。
4. **测试被算作证据。** 路线 A 有时会引用测试文件来证明某个
   功能存在。这通常是合理的，但应当加以标注
   （"由测试验证" vs "在源码中实现"），而不是混为一谈。
5. **没有置信度校准 / 没有交叉核对** —— 路线 A 与
   符号图之间缺乏核对：一个 `missing` 判定尚未与"但存在一个
   同名的公开符号"进行调和。
6. **成本/并发：** 一致性比对是串行的（每个功能一次 LLM 调用）。
   扇出（fan-out）可大幅缩短实际耗时。

---

## 6. 下一步（迈向 v0.2）

1. **改进路线 A 的检索** —— 嵌入（embeddings）+ 调用图邻域
   扩展；增加一个*验证趟次*，在最终定稿前将每个 `missing`/`partial`
   判定重新与同名的完整符号进行核对。这
   直接针对 click/itsdangerous 的假阴性问题。
2. **完成路线 C** —— 打包一个可用的 Trivy（或 `pip-audit`/OSV）路径以获得
   真实的 CVE 覆盖；增加许可证（license）红线。
3. **多语言采集** —— JS/TS 与 Go 文法；泛化导入/依赖
   分析。
4. **证据类型化** —— 区分"在源码中实现"与"被
   测试覆盖"；附上满足每个功能的确切行。
5. **并行化一致性比对**，并增加响应缓存以降低延迟/成本。
6. **CI 模式** —— 触发红线时以非零状态码退出、JSON/SARIF 输出，以及一个 PR 评论
   格式化器，使 Vibe Guard 能够对 AI 生成的 PR 进行门禁把关。
7. **校准基准（Calibration harness）** —— 一个带标注的 (repo, feature, truth) 基准集，
   以便能跨版本追踪路线 A 的精确率/召回率。

---

## 7. 交付 / 推送状态

所有产物都写入了 `~/cc/vibe-guard-mvp/` 目录下（MVP 代码在
`vibe_guard/`，被评估的仓库在 `test_projects/`，各项目报告
`reports_*.md`，以及本份总结）。

所提供的 `GITHUB_TOKEN` 在 REST API（`/user`、`/rate_limit`）和经认证的 git push
两方面均返回了 **`401 Bad credentials`**，因此本次运行**无法**推送
该仓库。如需发布，请使用有效的 token 重新运行：

```bash
cd ~/cc/vibe-guard-mvp
git init && git add -A && git commit -m "Vibe Guard MVP v0.1 + evaluation"
git remote add origin https://<TOKEN>@github.com/Rossery/vibe-guard.git
git push -u origin main          # 或使用 Contents API 配合有效 token
```

本地已准备好一个可直接推送的 git 仓库（含提交），因此
只需有效的凭据即可完成交付。
