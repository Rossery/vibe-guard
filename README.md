# Vibe Guard

> 验证 vibe coding 产物的开源工具：**AI 生成的代码，是不是真的实现了它声称的功能？对不对、稳不稳、安不安全？**

Vibe Guard 是一个**轻量编排层**，把成熟的开源工具（Semgrep / Trivy / Gitleaks / OSV-Scanner / tree-sitter / Qodo Cover-Agent 等）串成一条「纵深防御」验证流水线，对 AI 自动生成的代码仓库做端到端验证。

核心信条：**让 LLM 提出「该验证什么」，让真实执行决定「是否通过」**（execution > opinion）。

## 它验证什么

- **功能对齐**：声称的功能逐条核验 → 已实现 / 部分 / 缺失。
- **测试执行**：为功能点合成测试，在隔离沙箱真实跑通。
- **安全与供应链**：SAST + SCA + 密钥泄露 + 幻觉依赖（slopsquatting）。
- **可信度**：每条结论带证据链与置信度。

## 设计

完整架构见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

## 状态

🚧 设计阶段（v0.1）。当前已完成架构设计，MVP 开发中。

## License

MIT
