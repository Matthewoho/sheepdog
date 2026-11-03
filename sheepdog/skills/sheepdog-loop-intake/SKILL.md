---
name: sheepdog-loop-intake
description: "对话式创建与升级 Sheepdog 业务循环（Loop）。当用户说『帮我建个 loop』『把这件事做成循环』『一直帮我操心某事』时使用。"
---

# Sheepdog 对话式建 Loop 指南（Loop Intake & Authoring）

当主人希望把某件「需要持续操心、定期巡检、出问题自动修、从反馈里越跑越聪明」的事做成 Loop 时，按以下步骤闭环：

## 1. 先查能不能直接套用内置模板
运行 `sheepdog loop recommend` 或检查 6 大内置模板：
- `cloud-cost-steward`（全云资源利用率巡检与降本闭环）
- `service-release-guardian`（单服务上线盯盘 $\to$ 自动排障修代码 $\to$ 测服验证 $\to$ 拿生产授权发版闭环）
- `prod-release-pilot`（Lark 发版审批单零 Token 探活与引航）
- `vps-node-and-whitelist-steward`（3x-ui 专线节点健康度与测试白名单自动对齐）
- `ssl-cert-lifecycle-steward`（多云 SSL/TLS 证书到期预警与续期核验）
- `infra-migration-daily-steward`（跨云迁移阻塞跟踪与每日进度播报）

## 2. 明确 5 个业务格子（若主人已说清则直接推断，不反复盘问）
1. **① 取料与去重（`load_cmd` & `dedup_key`）**：怎么用最轻的命令判断「现在有没有新活」？按什么唯一键防重复？
2. **② 多阶段跟进状态机（`stages`）**：一件事从发现到彻底解决要经历哪几个阶段（例如 `detected -> fixing -> testing -> waiting_approval -> closed`）？
3. **③ 闲忙动态变频（`interval_minutes` vs. `active_interval_minutes`）**：平时多久探一次？手头有活在跟进时加密到几分钟盯一次？
4. **④ 拍板边界（`autonomy: shadow | confirm | auto`）**：新建默认 `shadow`（影子试跑发预览卡，主人点按钮再升 `confirm` 或 `auto`）。
5. **⑤ 专属记忆与豁免（`MEMORY.md`）**：哪些已知情况属于豁免噪音？

## 3. 一键落盘并触发首次影子试跑
1. 初始化循环目录与 `LOOP.md` / `MEMORY.md`：
   `sheepdog loop init <loop-id> --template <模板名或 custom> --title "<标题>" --topic <负责会话>`
2. 按需编辑 `~/.config/sheepdog/loops/<loop-id>/LOOP.md` 填入具体的 `load_cmd`、阶段和规则。
3. 触发一次 `shadow` 试跑并向主人发送带 **`[🚀 升为拍板模式 (confirm)]` / `[⚡ 升为全自动 (auto)]`** 按钮的预览卡：
   `sheepdog loop run <loop-id> --force`
