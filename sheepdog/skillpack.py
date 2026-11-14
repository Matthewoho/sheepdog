from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .sink.agentapi import transcript_path

CONFIG_DIR = Path.home() / ".config" / "sheepdog"

log = logging.getLogger("sheepdog")

MAX_SKILL_MD_BYTES = 40_000
WARN_SKILL_MD_BYTES = 32_000
MAX_BATCH_PROMPT_BYTES = 40_000
MAX_AMBIENT_BYTES = 8_000
MAX_SINGLE_SIGNAL_BYTES = 8_000
MAX_DIFF_SUMMARY_BYTES = 1_000
SKILL_THROTTLE_SECONDS = 3600
RULE_GATE_EXIT_CODE = 4

SKILL_DESCRIPTION = (
    "收到以 `[sheepdog]` 开头的信号批次时使用：牧羊犬托管会话的安全规则、业务规则和命令用法。"
    "批次里写着规则版本，版本变了先重读。"
)

DEFAULT_SKILL_DIR = CONFIG_DIR / "skills" / "sheepdog-worker"


class SkillPackBudgetError(RuntimeError):
    """Raised when rendered SKILL.md exceeds the 40,000-byte hard ceiling."""


@dataclass(frozen=True)
class SkillPackState:
    version: str
    prev_version: str
    published_at: int
    skill_dir: Path
    skill_md_path: Path
    skill_md_bytes: int
    diff_summary: str
    throttled: bool = False


@dataclass(frozen=True)
class GateCheckResult:
    allowed: bool
    exit_code: int = 0
    reason: str = ""
    strike_count: int = 0
    auto_passed_after_strikes: bool = False
    error_passthrough: bool = False


def resolve_skill_dir(
    skill_dir: Path | None = None,
    *,
    playbook_dir: Path | None = None,
    state_dir: Path | None = None,
) -> Path:
    if skill_dir is not None:
        return Path(skill_dir)
    env_override = os.environ.get("SHEEPDOG_SKILL_DIR", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    state_override = os.environ.get("SHEEPDOG_STATE_DIR", "").strip()
    if state_override:
        try:
            st_path = Path(state_override).expanduser().resolve()
            real_state = (Path.home() / ".local" / "state" / "sheepdog").resolve()
            if st_path != real_state and not str(st_path).startswith(str(real_state) + os.sep):
                return st_path / ".agents" / "skills" / "sheepdog-worker"
        except OSError:
            pass
    if playbook_dir is not None:
        try:
            raw_pb = Path(playbook_dir).expanduser()
            if not raw_pb.is_absolute() and not raw_pb.exists():
                raw_pb = CONFIG_DIR.expanduser() / raw_pb
            pb_path = raw_pb.resolve()
            real_cfg = CONFIG_DIR.expanduser().resolve()
            if pb_path != real_cfg and not str(pb_path).startswith(str(real_cfg) + os.sep):
                tmp_prefixes = ("/tmp/", "/var/folders/", "/private/tmp/", "/private/var/folders/", tempfile.gettempdir())
                if str(pb_path).startswith(tmp_prefixes):
                    return pb_path.parent / ".agents" / "skills" / "sheepdog-worker"
                digest = hashlib.sha256(str(pb_path).encode("utf-8")).hexdigest()[:12]
                return Path(tempfile.gettempdir()) / "sheepdog-test-skillpack" / digest / "sheepdog-worker"
        except OSError:
            pass
    if state_dir is not None:
        try:
            st_path = Path(state_dir).expanduser().resolve()
            real_state = (Path.home() / ".local" / "state" / "sheepdog").resolve()
            if st_path != real_state and not str(st_path).startswith(str(real_state) + os.sep):
                return st_path / ".agents" / "skills" / "sheepdog-worker"
        except OSError:
            pass
    if "unittest" in sys.modules and not os.environ.get("SHEEPDOG_CONFIG", "").strip():
        return Path(tempfile.gettempdir()) / f"sheepdog-unittest-skillpack-{os.getpid()}" / "sheepdog-worker"
    try:
        cfg_file = Path(os.environ.get("SHEEPDOG_CONFIG") or (CONFIG_DIR / "config.toml")).expanduser()
        if cfg_file.is_file():
            m = re.search(
                r'^\s*workdir\s*=\s*["\']([^"\']+)["\']',
                cfg_file.read_text(encoding="utf-8", errors="ignore"),
                re.M,
            )
            if m and m.group(1).strip():
                return Path(m.group(1).strip()).expanduser() / ".agents" / "skills" / "sheepdog-worker"
    except OSError:
        pass
    return DEFAULT_SKILL_DIR


def skill_start_marker(version: str, ref_name: str = "") -> str:
    prefix = f"sheepdog-worker:{ref_name}" if ref_name else "sheepdog-worker"
    return f"<!-- {prefix} 规则版本 {version} · 开始 -->"


def skill_end_marker(version: str, ref_name: str = "") -> str:
    prefix = f"sheepdog-worker:{ref_name}" if ref_name else "sheepdog-worker"
    return f"<!-- {prefix} 规则版本 {version} · 结束 -->"


def _read_optional(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _neutralize_session_vars(text: str) -> str:
    if not text:
        return ""
    out = text.replace("{{topic_id}}", "<批次头里的 topic>")
    out = out.replace("{{session_title}}", "本会话")
    out = out.replace("{{roster}}", "（名册见批次里的「名册更新」）")
    out = out.replace("{{vips}}", "（本批 VIP 见批次内 👑 印章）")
    out = out.replace("{{host}}", "codex / claude")
    out = re.sub(r"\{\{([A-Za-z0-9_]+)\}\}", r"<\1>", out)
    return out


def _split_common_sections(common_md: str) -> tuple[str, dict[str, str]]:
    """Split playbook/common.md into preamble + `### N.` sections."""
    lines = common_md.splitlines()
    preamble_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current_header: str | None = None
    for line in lines:
        if line.startswith("### "):
            current_header = line.strip()
            sections[current_header] = [line]
        elif current_header is not None:
            sections[current_header].append(line)
        else:
            preamble_lines.append(line)
    return (
        "\n".join(preamble_lines).strip(),
        {k: "\n".join(v).strip() for k, v in sections.items()},
    )


def _load_skill_config_and_playbook(
    config_dir: Path | None = None,
    playbook_dir: Path | None = None,
) -> tuple[Any, Any, Path, Path]:
    import tomllib
    from .config import Config, _apply_file, default_reply_suffix
    from .playbook import Playbook

    if playbook_dir is not None:
        raw_pb = Path(playbook_dir).expanduser()
        if not raw_pb.is_absolute() and not raw_pb.exists():
            base_cfg = Path(config_dir).expanduser() if config_dir is not None else CONFIG_DIR
            pb_dir = base_cfg / raw_pb
        else:
            pb_dir = raw_pb
    else:
        pb_dir = Path(config_dir) / "playbook" if config_dir is not None else (CONFIG_DIR / "playbook")
    cfg_root = Path(config_dir) if config_dir is not None else pb_dir.parent
    cfg_file = cfg_root / "config.toml"
    cfg = Config()
    cfg.config_path = cfg_file
    if cfg_file.exists():
        try:
            with cfg_file.open("rb") as f:
                data = tomllib.load(f)
            _apply_file(cfg, data)
        except Exception:
            pass

    w = cfg.watch
    base_vars = {
        "reply_prefix": cfg.session.reply_prefix or "",
        "reply_suffix": cfg.session.reply_suffix or default_reply_suffix(),
        "watch_remind_minutes": "/".join(str(m) for m in (w.remind_minutes or [5, 15, 30])),
        "watch_expire_minutes": str(w.expire_minutes or 120),
        "receipt_timeout_minutes": str(cfg.session.receipt_timeout_minutes or 20),
        "cli": cfg.cli_command or "sheepdog",
        "topic_id": "<批次头里的 topic>",
        "session_title": "本会话",
        "roster": "（名册见批次里的「名册更新」）",
        "vips": "（本批 VIP 见批次内 👑 印章）",
        "host": "codex / claude",
        **({"identity_enabled": "1"} if getattr(getattr(cfg, "identity", None), "enabled", False) else {}),
        **({"topic_group_enabled": "1"} if getattr(getattr(cfg, "topic_group", None), "active", False) else {}),
    }
    pb = Playbook(pb_dir, base_vars)
    return cfg, pb, pb_dir, cfg_root


def _compute_raw_manifest(
    config_dir: Path | None = None,
    playbook_dir: Path | None = None,
) -> dict[str, str]:
    from . import prompts as _prompts
    from .playbook import _COMMENT

    cfg, pb, pb_dir, cfg_root = _load_skill_config_and_playbook(
        config_dir=config_dir, playbook_dir=playbook_dir
    )
    prompts_dir = pb_dir / "prompts"

    def _pick(*candidates: Path) -> Path:
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    def _render_path(path: Path) -> str:
        raw = _read_optional(path)
        if not raw:
            return ""
        stripped = _COMMENT.sub("", raw).strip()
        if not stripped:
            return ""
        return _neutralize_session_vars(pb._fill(stripped, {}))

    overlay_raw = cfg.prompt_overlay() if getattr(cfg, "prompt_overlay_path", "") else ""
    if not overlay_raw:
        overlay_path = _pick(
            cfg_root / "prompts" / "overlay.md",
            prompts_dir / "bus_Session.md",
            cfg_root / "prompt_overlay.md",
        )
        overlay_raw = _read_optional(overlay_path)
    bus_session_rendered = (
        _neutralize_session_vars(pb._fill(_COMMENT.sub("", overlay_raw).strip(), {}))
        if overlay_raw and _COMMENT.sub("", overlay_raw).strip()
        else ""
    )

    files = {
        "common.md": _pick(pb_dir / "common.md"),
        "security.md": _pick(pb_dir / "security.md", cfg_root / "security.md"),
        "bus_guide.md": _pick(pb_dir / "bus.md", prompts_dir / "bus_guide.md"),
        "vip_block.md": _pick(pb_dir / "vip.md", prompts_dir / "vip_block.md"),
        "batch_footer.md": _pick(pb_dir / "batch_footer.md", prompts_dir / "batch_footer.md"),
        "security_footer.md": _pick(pb_dir / "security_footer.md", prompts_dir / "security_footer.md"),
        "agent_sender_label.md": _pick(pb_dir / "agent_sender_label.md", prompts_dir / "agent_sender_label.md"),
        "topic_group_start_hint.md": _pick(pb_dir / "topic_group_start_hint.md", prompts_dir / "topic_group_start_hint.md"),
        "cli_host.md": _pick(pb_dir / "cli_host.md", prompts_dir / "cli_host.md"),
    }
    manifest: dict[str, str] = {}
    for key, path in sorted(files.items()):
        manifest[key] = _render_path(path)
    manifest["bus_session.md"] = bus_session_rendered

    w = cfg.watch
    remind = w.remind_minutes or [5, 15, 30]
    expire = w.expire_minutes or 120
    manifest["__interface_commands__"] = _neutralize_session_vars(
        _prompts.interface_section(
            "<批次头里的 topic>",
            bus=False,
            remind=remind,
            expire=expire,
            cli=pb.cli,
            identity=pb.identity,
            topic_group=pb.topic_group,
            receipt_timeout=pb.receipt_timeout,
        )
    )
    manifest["__interface_bus__"] = _neutralize_session_vars(
        _prompts.interface_section(
            "tp_bus",
            bus=True,
            remind=remind,
            expire=expire,
            cli=pb.cli,
            identity=pb.identity,
            topic_group=pb.topic_group,
            receipt_timeout=pb.receipt_timeout,
        )
    )
    return manifest


def _summarize_diff(
    old_hashes: dict[str, str],
    new_hashes: dict[str, str],
    old_version: str,
    new_version: str,
) -> str:
    if not old_hashes or not old_version or old_version == new_version:
        return ""
    changed_secs: list[str] = []
    for k, new_dig in sorted(new_hashes.items()):
        if not k.startswith("sec:"):
            continue
        if old_hashes.get(k) != new_dig:
            title = k[len("sec:") :].lstrip("#").strip()
            changed_secs.append(title)
    changed_files: list[str] = []
    for k, new_dig in sorted(new_hashes.items()):
        if not k.startswith("file:"):
            continue
        if old_hashes.get(k) != new_dig:
            fname = k[len("file:") :]
            if fname.startswith("__"):
                continue
            if fname != "common.md" or not changed_secs:
                changed_files.append(fname)
    parts = changed_secs + changed_files
    if not parts:
        summary = f"相比 {old_version} 更新了规则细则"
    else:
        summary = f"相比 {old_version} 改了：" + "、".join(parts)
    encoded = summary.encode("utf-8")
    if len(encoded) > MAX_DIFF_SUMMARY_BYTES:
        summary = encoded[: MAX_DIFF_SUMMARY_BYTES - 20].decode("utf-8", errors="ignore") + "…"
    return summary


def _render_skill_bundle(
    manifest: dict[str, str],
    interface_doc: str = "",
    bus_interface_doc: str = "",
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Render SKILL.md and references/*.md.
    Version is the first 8 hex chars of sha256 over the rendered SKILL.md body + bus reference + commands reference.
    """
    common_md = _neutralize_session_vars(manifest.get("common.md", ""))
    security_md = _neutralize_session_vars(manifest.get("security.md", ""))
    bus_guide = _neutralize_session_vars(manifest.get("bus_guide.md", ""))
    bus_session = _neutralize_session_vars(manifest.get("bus_session.md", ""))
    vip_block = _neutralize_session_vars(manifest.get("vip_block.md", ""))
    batch_footer = _neutralize_session_vars(manifest.get("batch_footer.md", ""))
    security_footer = _neutralize_session_vars(manifest.get("security_footer.md", ""))
    agent_sender_label = _neutralize_session_vars(manifest.get("agent_sender_label.md", ""))
    topic_group_start = _neutralize_session_vars(manifest.get("topic_group_start_hint.md", ""))
    cli_host_md = _neutralize_session_vars(manifest.get("cli_host.md", ""))
    eff_iface = _neutralize_session_vars(
        interface_doc or manifest.get("__interface_commands__", "")
    )
    eff_bus_iface = _neutralize_session_vars(
        bus_interface_doc or manifest.get("__interface_bus__", "")
    )

    preamble, common_sections = _split_common_sections(common_md)

    section_hashes: dict[str, str] = {}
    for key, text in sorted(manifest.items()):
        if key.startswith("__"):
            continue
        section_hashes[f"file:{key}"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    for sec_title, sec_body in sorted(common_sections.items()):
        section_hashes[f"sec:{sec_title}"] = hashlib.sha256(sec_body.encode("utf-8")).hexdigest()[:12]
    if eff_iface:
        section_hashes["sec:sheepdog 接口说明"] = hashlib.sha256(eff_iface.encode("utf-8")).hexdigest()[:12]
    if eff_bus_iface:
        section_hashes["sec:总线接口说明"] = hashlib.sha256(eff_bus_iface.encode("utf-8")).hexdigest()[:12]

    placeholder = "__SHEEPDOG_RULE_VERSION__"

    commands_parts: list[str] = [
        skill_start_marker(placeholder, "commands"),
        f"# sheepdog 命令参数、事实出处核对与等待回执参考（版本 {placeholder}）",
        "",
    ]
    if eff_iface:
        commands_parts.extend([eff_iface.strip(), ""])

    lambs_parts: list[str] = [
        skill_start_marker(placeholder, "lambs"),
        f"# 子会话（羔羊）与羊群看板说明（版本 {placeholder}）",
        "",
    ]
    topic_group_parts: list[str] = [
        skill_start_marker(placeholder, "topic-group"),
        f"# 话题群（topic_group）开新话题与置顶卡规则（版本 {placeholder}）",
        "",
    ]
    if topic_group_start:
        topic_group_parts.extend([topic_group_start, ""])

    core_section_blocks: list[str] = []
    for header, body in common_sections.items():
        if header.startswith("### 4.") or header.startswith("### 7.") or header.startswith("### 11."):
            commands_parts.extend([body, ""])
            if header.startswith("### 4."):
                core_section_blocks.append(
                    f"{header}\n"
                    "- 发出问题等别人回时用 `--status waiting` 或 `sheepdog watch`；"
                    "禁止在会话内 `sleep`/轮询。完整参数与追问边界见 `references/commands.md`。"
                )
            elif header.startswith("### 7."):
                core_section_blocks.append(
                    f"{header}\n"
                    "- 群里跟自己有关的非闲聊消息可贴 `Get`（`sheepdog react <message_id>`），"
                    "私聊由守护进程自动贴。细则见 `references/commands.md`。"
                )
            elif header.startswith("### 11."):
                core_section_blocks.append(
                    f"{header}\n"
                    "- **硬红线**：任何「已核查/已生效/没有/数量/配置值」必须写出本轮亲手跑过的命令或读过的文件与时间；"
                    "写日期时间前先跑 `date`；严禁把本地文件/脑补当云上事实。完整核对表与正反例见 `references/commands.md`。"
                )
        elif header.startswith("### 12."):
            lambs_parts.extend([body, ""])
            core_section_blocks.append(
                f"{header}\n"
                "- 跨会话查看与留言用 `sheepdog board` / `sheepdog tell` / `sheepdog work`。"
                "完整命令见 `references/lambs.md` 与 `references/commands.md`。"
            )
        else:
            core_section_blocks.append(body)

    commands_parts.append(skill_end_marker(placeholder, "commands"))
    lambs_parts.append(skill_end_marker(placeholder, "lambs"))
    topic_group_parts.append(skill_end_marker(placeholder, "topic-group"))

    bus_parts = [
        skill_start_marker(placeholder, "bus"),
        f"# tp_bus 路由与转交规则（版本 {placeholder}）",
        "",
    ]
    if bus_guide:
        bus_parts.extend([bus_guide, ""])
    if bus_session:
        bus_parts.extend([bus_session, ""])
    if eff_bus_iface:
        bus_parts.extend([eff_bus_iface.strip(), ""])
    bus_parts.append(skill_end_marker(placeholder, "bus"))

    vip_parts = [
        skill_start_marker(placeholder, "vip"),
        f"# VIP 授权与三道底线（版本 {placeholder}）",
        "",
        vip_block or "（当批次带有 👑 VIP 块时，按批次内 VIP 授权与三道底线执行。）",
        "",
        skill_end_marker(placeholder, "vip"),
    ]

    cli_host_parts = [
        skill_start_marker(placeholder, "cli-host"),
        f"# 命令行宿主（Codex / Claude CLI）读法指引（版本 {placeholder}）",
        "",
        cli_host_md or "- 命令行宿主会话按同样路径读取 `SKILL.md` 与 `references/*.md`。",
        "",
        skill_end_marker(placeholder, "cli-host"),
    ]

    skill_md_parts: list[str] = [
        skill_start_marker(placeholder),
        "---",
        "name: sheepdog-worker",
        f'description: "{SKILL_DESCRIPTION}"',
        "---",
        "",
        f"# Sheepdog 托管会话工作规则（版本 `{placeholder}`）",
        "",
        "> **必读门禁**：本窗口还没读过当前版本（或发生过上下文压缩 `CHECKPOINT`）时，必须一次完整读完本文件（同时包含文件头第一行与文件尾最后一行的 `规则版本` 标记）。首次调用 `sheepdog reply / report / ask / receipt` 前，CLI 会核对 `transcript_full.jsonl`（未读返回退出码 `4`，参数不用改，读完重试；仅 `receipt --status waiting / stopped` 例外）。",
        "",
        "## 按需参考目录（Progressive Disclosure）",
        "",
        "| 场景 | 按需读取路径 |",
        "| --- | --- |",
        "| 命令说明全文（`reply / ask / report / receipt / watch / board`）与事实出处核对表 | `references/commands.md` |",
        "| `tp_bus` 总线规则与转交（总线会话必读） | `references/bus.md` |",
        "| VIP 规则全文 | `references/vip.md` |",
        "| 羔羊（子会话）与羊群看板 | `references/lambs.md` |",
        "| 话题群长期跟踪与 `--start` | `references/topic-group.md` |",
        "| 命令行宿主（Codex / Claude） | `references/cli-host.md` |",
        "| 单条超 8KB 信号或被截断批次补读全文 | `sheepdog show <消息编号>` 或 `sheepdog show --batch <批次号>` |",
        "",
    ]

    if security_md:
        skill_md_parts.extend([
            "## 安全与授权红线",
            "",
            security_md,
            "",
        ])

    if preamble:
        skill_md_parts.extend([preamble, ""])

    if core_section_blocks:
        skill_md_parts.extend(["\n\n".join(core_section_blocks), ""])

    footer_notes: list[str] = []
    if agent_sender_label:
        footer_notes.append(agent_sender_label)
    if batch_footer:
        footer_notes.append(batch_footer)
    if security_footer:
        footer_notes.append(security_footer)
    if footer_notes:
        skill_md_parts.extend([
            "## 外发自检与收尾提醒",
            "",
            "\n\n".join(footer_notes),
            "",
        ])

    skill_md_parts.append(skill_end_marker(placeholder))
    template_skill_md = "\n".join(skill_md_parts) + "\n"
    template_bus_md = "\n".join(bus_parts) + "\n"
    template_commands_md = "\n".join(commands_parts) + "\n"

    # Compute sha256[:8] from rendered SKILL.md + bus reference + commands reference
    h = hashlib.sha256()
    h.update(template_skill_md.encode("utf-8"))
    h.update(b"\n--BUS--\n")
    h.update(template_bus_md.encode("utf-8"))
    h.update(b"\n--COMMANDS--\n")
    h.update(template_commands_md.encode("utf-8"))
    version = h.hexdigest()[:8]

    skill_md_final = template_skill_md.replace(placeholder, version)
    skill_md_bytes = len(skill_md_final.encode("utf-8"))
    if skill_md_bytes > MAX_SKILL_MD_BYTES:
        raise SkillPackBudgetError(
            f"Rendered SKILL.md is {skill_md_bytes} bytes, exceeding hard ceiling {MAX_SKILL_MD_BYTES} bytes"
        )
    if skill_md_bytes > WARN_SKILL_MD_BYTES:
        log.warning(
            "skillpack: SKILL.md size %d bytes exceeds warning threshold %d bytes",
            skill_md_bytes,
            WARN_SKILL_MD_BYTES,
        )

    files = {
        "SKILL.md": skill_md_final,
        ".generated": (
            "sheepdog 生成，别手改（Generated by sheepdog skillpack from ~/.config/sheepdog/playbook; do not edit by hand.）\n"
            f"version={version}\n"
        ),
        "references/commands.md": template_commands_md.replace(placeholder, version),
        "references/bus.md": template_bus_md.replace(placeholder, version),
        "references/vip.md": ("\n".join(vip_parts) + "\n").replace(placeholder, version),
        "references/lambs.md": ("\n".join(lambs_parts) + "\n").replace(placeholder, version),
        "references/topic-group.md": ("\n".join(topic_group_parts) + "\n").replace(placeholder, version),
        "references/cli-host.md": ("\n".join(cli_host_parts) + "\n").replace(placeholder, version),
    }
    return version, files, section_hashes


def compute_raw_skill_version(
    config_dir: Path | None = None,
    playbook_dir: Path | None = None,
    interface_doc: str = "",
    bus_interface_doc: str = "",
) -> tuple[str, dict[str, str], dict[str, str]]:
    manifest = _compute_raw_manifest(config_dir=config_dir, playbook_dir=playbook_dir)
    version, _, section_hashes = _render_skill_bundle(
        manifest, interface_doc=interface_doc, bus_interface_doc=bus_interface_doc
    )
    return version, manifest, section_hashes


def _atomic_write_skill_dir(target_dir: Path, files: dict[str, str]) -> None:
    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(
        tempfile.mkdtemp(prefix=f".{target_dir.name}.tmp-", dir=str(parent))
    )
    backup_dir: Path | None = None
    try:
        for rel_path, content in files.items():
            dest = tmp_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        if target_dir.exists():
            backup_dir = parent / f".{target_dir.name}.bak-{os.getpid()}-{time.time_ns()}"
            os.replace(target_dir, backup_dir)
        os.replace(tmp_dir, target_dir)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
            try:
                os.replace(backup_dir, target_dir)
            except OSError:
                pass
        raise


def ensure_skillpack_rendered(
    store: Any = None,
    *,
    config_dir: Path | None = None,
    playbook_dir: Path | None = None,
    skill_dir: Path | None = None,
    state_dir: Path | None = None,
    interface_doc: str = "",
    bus_interface_doc: str = "",
    force: bool = False,
    throttle_seconds: int = SKILL_THROTTLE_SECONDS,
    now_ts: float | None = None,
) -> SkillPackState:
    eff_state_dir = state_dir
    if eff_state_dir is None and store is not None:
        db_p = getattr(store, "db_path", None)
        if db_p and str(db_p) != ":memory:":
            eff_state_dir = Path(db_p).parent
    target_dir = resolve_skill_dir(skill_dir, playbook_dir=playbook_dir, state_dir=eff_state_dir)
    now = int(now_ts if now_ts is not None else time.time())
    manifest = _compute_raw_manifest(config_dir=config_dir, playbook_dir=playbook_dir)
    raw_version, files, section_hashes = _render_skill_bundle(
        manifest, interface_doc=interface_doc, bus_interface_doc=bus_interface_doc
    )

    active_version = ""
    prev_version = ""
    published_at = 0
    last_update_at = 0
    diff_summary = ""
    old_hashes: dict[str, str] = {}
    throttled = False

    if store is not None:
        try:
            active_version = (store.get_meta("skillpack:active_version") or "").strip()
            prev_version = (store.get_meta("skillpack:prev_version") or "").strip()
            published_at_raw = (store.get_meta("skillpack:published_at") or "0").strip()
            published_at = int(published_at_raw) if published_at_raw.isdigit() else 0
            last_update_raw = (store.get_meta("skillpack:last_update_at") or "0").strip()
            last_update_at = int(last_update_raw) if last_update_raw.isdigit() else 0
            diff_summary = (store.get_meta("skillpack:diff_summary") or "").strip()
            old_hashes_raw = store.get_meta("skillpack:section_hashes") or ""
            if old_hashes_raw:
                old_hashes = json.loads(old_hashes_raw)
        except Exception as e:
            log.warning("skillpack: failed reading store meta: %s", e)

    if (
        not force
        and active_version
        and store is not None
        and not manifest.get("common.md", "").strip()
    ):
        try:
            active_manifest_raw = store.get_meta("skillpack:active_manifest") or ""
            if active_manifest_raw:
                stored_manifest = json.loads(active_manifest_raw)
                if isinstance(stored_manifest, dict) and str(stored_manifest.get("common.md") or "").strip():
                    manifest = stored_manifest
                    raw_version, files, section_hashes = _render_skill_bundle(
                        manifest, interface_doc=interface_doc, bus_interface_doc=bus_interface_doc
                    )
        except Exception as e:
            log.warning("skillpack: failed restoring active_manifest on empty common.md: %s", e)

    if not active_version:
        active_version = raw_version
        published_at = now
        if store is not None:
            try:
                store.set_meta("skillpack:active_version", active_version)
                store.set_meta("skillpack:published_at", str(published_at))
                store.set_meta("skillpack:last_update_at", "0")
                store.set_meta(
                    "skillpack:section_hashes",
                    json.dumps(section_hashes, ensure_ascii=False, sort_keys=True),
                )
                store.set_meta(
                    "skillpack:active_manifest",
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                )
            except Exception as e:
                log.warning("skillpack: failed writing initial store meta: %s", e)
    elif raw_version != active_version:
        elapsed = now - last_update_at
        if not force and throttle_seconds > 0 and last_update_at > 0 and elapsed < throttle_seconds:
            throttled = True
            if store is not None:
                try:
                    store.set_meta("skillpack:pending_raw_version", raw_version)
                    active_manifest_raw = store.get_meta("skillpack:active_manifest") or ""
                    if active_manifest_raw:
                        manifest = json.loads(active_manifest_raw)
                        active_version, files, _ = _render_skill_bundle(
                            manifest, interface_doc=interface_doc
                        )
                except Exception:
                    pass
        else:
            diff_summary = _summarize_diff(
                old_hashes, section_hashes, active_version, raw_version
            )
            prev_version = active_version
            active_version = raw_version
            published_at = now
            last_update_at = now
            if store is not None:
                try:
                    store.set_meta("skillpack:prev_version", prev_version)
                    store.set_meta("skillpack:active_version", active_version)
                    store.set_meta("skillpack:published_at", str(published_at))
                    store.set_meta("skillpack:last_update_at", str(last_update_at))
                    store.set_meta("skillpack:diff_summary", diff_summary)
                    store.set_meta("skillpack:pending_raw_version", "")
                    store.set_meta(
                        "skillpack:section_hashes",
                        json.dumps(section_hashes, ensure_ascii=False, sort_keys=True),
                    )
                    store.set_meta(
                        "skillpack:active_manifest",
                        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                    )
                except Exception as e:
                    log.warning("skillpack: failed updating store meta: %s", e)

    skill_md_path = target_dir / "SKILL.md"
    generated_path = target_dir / ".generated"
    need_write = force or not skill_md_path.exists() or not generated_path.exists()
    if not need_write:
        try:
            gen_text = generated_path.read_text(encoding="utf-8")
            if f"version={active_version}" not in gen_text:
                need_write = True
        except OSError:
            need_write = True

    if need_write:
        _atomic_write_skill_dir(target_dir, files)
        skill_md_bytes = len(files["SKILL.md"].encode("utf-8"))
        if store is not None:
            try:
                store.set_meta("skillpack:skill_md_bytes", str(skill_md_bytes))
            except Exception:
                pass
    else:
        try:
            skill_md_bytes = skill_md_path.stat().st_size
        except OSError:
            skill_md_bytes = len(files["SKILL.md"].encode("utf-8"))

    return SkillPackState(
        version=active_version,
        prev_version=prev_version,
        published_at=published_at,
        skill_dir=target_dir,
        skill_md_path=skill_md_path,
        skill_md_bytes=skill_md_bytes,
        diff_summary=diff_summary,
        throttled=throttled,
    )


def build_rule_pointer_line(
    *,
    version: str,
    skill_dir: Path | None = None,
    topic: str = "",
    is_read: bool = False,
    last_read_version: str = "",
    diff_summary: str = "",
    compacted: bool = False,
    compaction_note: str = "",
) -> str:
    """Render the 1-line rule pointer for `batch_prompt` (Spec 4.2)."""
    sdir = resolve_skill_dir(skill_dir)
    skill_md = sdir / "SKILL.md"
    bus_ref = sdir / "references" / "bus.md"
    bus_note = f" 总线还须完整读 {bus_ref}。" if topic == "tp_bus" else ""

    if is_read and not compacted:
        return f"📘 规则 sheepdog-worker 版本 {version}（已读）"

    if compacted:
        extra = f"（{compaction_note.strip()}）" if compaction_note and compaction_note.strip() else ""
        return (
            f"📘 规则版本 {version}（上下文压缩过）{extra}。先完整读 {skill_md} 再处理下面的信号；"
            f"没读完，reply / report / ask 和「已处理」回执会被拒。{bus_note}"
        )

    prev_part = f"（你上次读的是 {last_read_version}）" if last_read_version and last_read_version != version else ""
    diff_part = f" 变更：{diff_summary}" if diff_summary else ""
    return (
        f"📘 规则更新到 {version}{prev_part}。先完整读 {skill_md} 再处理下面的信号；"
        f"没读完，reply / report / ask 和「已处理」回执会被拒。{bus_note}{diff_part}"
    )


def cap_ambient_context(ambient: str, topic: str, max_bytes: int = MAX_AMBIENT_BYTES) -> str:
    if not ambient or not ambient.strip():
        return ""
    raw = ambient.strip()
    if len(raw.encode("utf-8")) <= max_bytes:
        return raw
    tail_note = f"\n…（更早的用 `sheepdog thread` 或 `sheepdog context --topic {topic}` 看）"
    budget = max(200, max_bytes - len(tail_note.encode("utf-8")))
    lines = raw.splitlines()
    header_lines: list[str] = []
    body_lines: list[str] = []
    for idx, line in enumerate(lines):
        if idx < 2 and (line.startswith("【") or line.startswith("#") or line.startswith("🌐")):
            header_lines.append(line)
        else:
            body_lines.append(line)
    header_str = "\n".join(header_lines)
    header_bytes = len((header_str + "\n").encode("utf-8")) if header_str else 0
    kept_reversed: list[str] = []
    used = header_bytes
    for line in reversed(body_lines):
        line_bytes = len((line + "\n").encode("utf-8"))
        if used + line_bytes > budget:
            break
        kept_reversed.append(line)
        used += line_bytes
    kept = list(reversed(kept_reversed))
    if not kept and body_lines:
        last_bytes = body_lines[-1].encode("utf-8")[: max(50, budget - 20)]
        kept = [last_bytes.decode("utf-8", errors="ignore")]
    assembled = (header_str + "\n" if header_str else "") + "\n".join(kept) + tail_note
    return assembled.strip()


def cap_single_signal_block(
    rendered_signal: str,
    message_id: str,
    max_bytes: int = MAX_SINGLE_SIGNAL_BYTES,
) -> str:
    encoded = rendered_signal.encode("utf-8")
    if len(encoded) <= max_bytes:
        return rendered_signal
    lines = rendered_signal.splitlines()
    tail_mid_line = ""
    if lines and lines[-1].startswith("message_id:"):
        tail_mid_line = "\n" + lines[-1]
    suffix = f"\n…（正文超 8KB 已截断，全文：`sheepdog show {message_id}`）{tail_mid_line}"
    budget = max(256, max_bytes - len(suffix.encode("utf-8")))
    truncated = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return truncated + suffix


def assert_batch_chunks_within_budget(
    chunks: list[str],
    expected_message_ids: list[str],
    batch_id: str,
    max_bytes: int = MAX_BATCH_PROMPT_BYTES,
) -> None:
    if not chunks:
        raise ValueError(f"batch {batch_id} produced 0 chunks")
    found_mids: set[str] = set()
    for idx, chunk in enumerate(chunks):
        chunk_bytes = chunk.encode("utf-8")
        if len(chunk_bytes) > max_bytes:
            raise ValueError(
                f"batch {batch_id} chunk {idx + 1}/{len(chunks)} is {len(chunk_bytes)} bytes "
                f"(exceeds {max_bytes} bytes)"
            )
        first_40k = chunk_bytes[:max_bytes].decode("utf-8", errors="ignore")
        for mid in expected_message_ids:
            if mid and mid in first_40k:
                found_mids.add(mid)
        if idx < len(chunks) - 1:
            if f"--batch {batch_id} --json" in chunk:
                raise ValueError(
                    f"batch {batch_id} non-final chunk {idx + 1}/{len(chunks)} must not contain receipt command"
                )
    missing = [mid for mid in expected_message_ids if mid and mid not in found_mids]
    if missing:
        raise ValueError(
            f"batch {batch_id} missing message_ids in first 40KB of chunks: {missing}"
        )


def transcript_full_path(
    conversation_id: str,
    brain_dir: Path | None = None,
) -> Path:
    """Return path to `transcript_full.jsonl` (NEVER `transcript.jsonl`)."""
    if brain_dir is not None:
        return (
            Path(brain_dir)
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript_full.jsonl"
        )
    env_override = os.environ.get("SHEEPDOG_BRAIN_DIR", "").strip()
    if env_override:
        return (
            Path(env_override).expanduser()
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript_full.jsonl"
        )
    return transcript_path(conversation_id).with_name("transcript_full.jsonl")


def _parse_iso_to_epoch(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def check_transcript_skill_read(
    conversation_id: str,
    *,
    version: str,
    topic: str = "",
    published_at: int = 0,
    brain_dir: Path | None = None,
    store: Any = None,
) -> tuple[bool, str]:
    """Verify in `transcript_full.jsonl` (never `transcript.jsonl`) that after
    `max(published_at, latest_CHECKPOINT)`, a GENERIC tool output step contains
    both start and end markers for `version` (and `references/bus.md` markers if `topic == 'tp_bus'`).
    """
    tf_path = transcript_full_path(conversation_id, brain_dir=brain_dir)
    if not tf_path.exists():
        return True, "no_transcript_full"

    start_mk = skill_start_marker(version)
    end_mk = skill_end_marker(version)
    bus_start_mk = skill_start_marker(version, "bus")
    bus_end_mk = skill_end_marker(version, "bus")

    raw_text = tf_path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in raw_text.splitlines() if ln.strip()]

    last_checkpoint_idx = -1
    steps: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        steps.append(obj)
        if obj.get("type") == "CHECKPOINT":
            content = str(obj.get("content") or "")
            if not content or "compaction" in content.lower() or "resuming" in content.lower():
                last_checkpoint_idx = len(steps) - 1

    post_checkpoint_steps = steps[last_checkpoint_idx + 1 :]
    skill_read_ok = False
    bus_read_ok = topic != "tp_bus"
    matched_step_idx = -1

    for idx, step in enumerate(post_checkpoint_steps):
        if step.get("type") not in ("GENERIC", "TOOL_RESPONSE"):
            continue
        if published_at > 0:
            step_epoch = _parse_iso_to_epoch(str(step.get("created_at") or ""))
            if step_epoch > 0 and step_epoch + 1.0 < published_at:
                continue
        content = str(step.get("content") or "")
        if start_mk in content and end_mk in content:
            skill_read_ok = True
            matched_step_idx = int(step.get("step_index") or idx)
        if topic == "tp_bus" and bus_start_mk in content and bus_end_mk in content:
            bus_read_ok = True

    if skill_read_ok and bus_read_ok:
        if store is not None:
            try:
                store.set_meta(
                    f"rules_read:{conversation_id}",
                    json.dumps(
                        {
                            "version": version,
                            "step_index": matched_step_idx,
                            "checkpoint_idx": last_checkpoint_idx,
                            "bus_read": bus_read_ok,
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                pass
        return True, "ok"

    if not skill_read_ok:
        return False, "skill_md_unread"
    return False, "bus_ref_unread"


def enforce_rule_read_gate(
    store: Any,
    *,
    topic: str,
    action: str,
    receipt_status: str = "",
    skill_dir: Path | None = None,
    brain_dir: Path | None = None,
    config_dir: Path | None = None,
    playbook_dir: Path | None = None,
) -> GateCheckResult:
    """Pre-send gate for `reply / report / ask / receipt`.
    Exempts `receipt` with status `waiting` or `stopped` (while recording a metric if unread).
    On non-Antigravity host, missing `transcript_full.jsonl`, or error: passes through and logs.
    On 3 consecutive blocks for the same batch: passes through on 4th attempt and alerts owner.
    """
    is_exempt_receipt = action == "receipt" and receipt_status in ("waiting", "stopped")

    if not topic or store is None:
        return GateCheckResult(allowed=True, exit_code=0, reason="no_topic_or_store")

    try:
        t_row = store.get_topic(topic)
    except Exception as e:
        log.warning("rule_gate: error fetching topic %s: %s", topic, e)
        _inc_meta_counter(store, "observe:gate_error_passthrough")
        return GateCheckResult(
            allowed=True, exit_code=0, reason=f"topic_lookup_error:{e}", error_passthrough=True
        )

    if t_row is None:
        return GateCheckResult(allowed=True, exit_code=0, reason="unmanaged_topic")

    t_keys = set(t_row.keys()) if hasattr(t_row, "keys") else set()
    host = str((t_row["host"] if "host" in t_keys else "antigravity") or "antigravity").strip()
    if host != "antigravity":
        return GateCheckResult(allowed=True, exit_code=0, reason=f"non_antigravity_host:{host}")

    cid = str((t_row["conversation_id"] if "conversation_id" in t_keys else "") or "").strip()
    if not cid:
        return GateCheckResult(allowed=True, exit_code=0, reason="no_conversation_id")

    if brain_dir is None and not (store.get_meta("skillpack:active_version") or "").strip():
        return GateCheckResult(allowed=True, exit_code=0, reason="skillpack_not_initialized")

    tf_path = transcript_full_path(cid, brain_dir=brain_dir)
    if not tf_path.exists():
        # Spec 4.3 & 7.3⑥: 会话记录文件不存在 → 放行并记一笔（未创建会话目录的旧单测假会话不污染 meta 快照）
        if brain_dir is not None or tf_path.parent.is_dir():
            _inc_meta_counter(store, "observe:gate_error_passthrough")
        return GateCheckResult(
            allowed=True,
            exit_code=0,
            reason="transcript_full_missing",
            error_passthrough=True,
        )

    try:
        state = ensure_skillpack_rendered(
            store, config_dir=config_dir, playbook_dir=playbook_dir, skill_dir=skill_dir
        )
    except Exception as e:
        log.warning("rule_gate: error ensuring skillpack for %s: %s", topic, e)
        _inc_meta_counter(store, "observe:gate_error_passthrough")
        return GateCheckResult(
            allowed=True, exit_code=0, reason=f"skillpack_error:{e}", error_passthrough=True
        )

    try:
        ok, detail = check_transcript_skill_read(
            cid,
            version=state.version,
            topic=topic,
            published_at=state.published_at,
            brain_dir=brain_dir,
            store=store,
        )
    except Exception as e:
        log.warning("rule_gate: error reading transcript_full for %s (%s): %s", topic, cid, e)
        _inc_meta_counter(store, "observe:gate_error_passthrough")
        return GateCheckResult(
            allowed=True, exit_code=0, reason=f"transcript_read_error:{e}", error_passthrough=True
        )

    if ok:
        return GateCheckResult(allowed=True, exit_code=0, reason="verified")

    if is_exempt_receipt:
        _inc_meta_counter(store, "observe:rule_unread_exempt_receipt")
        return GateCheckResult(allowed=True, exit_code=0, reason="exempt_status_unread_logged")

    batch_key = _latest_batch_key_for_topic(store, topic)
    strike_meta_key = f"skillpack:block_count:{topic}:{batch_key}:{state.version}"
    strike_count = _inc_meta_counter(store, strike_meta_key)

    if strike_count >= 3:
        _inc_meta_counter(store, "observe:gate_3strike_passthrough")
        alert_msg = (
            f"会话 {topic} 在批次 {batch_key} 连续 3 次未读规则（版本 {state.version}），"
            f"已放行并请检查。"
        )
        log.warning("rule_gate: %s", alert_msg)
        try:
            store.set_meta(f"skillpack:last_3strike_alert:{topic}", alert_msg)
            if hasattr(store, "add_owner_notice"):
                ik = "sd-3strike-" + hashlib.sha256(f"{topic}:{batch_key}:{state.version}".encode("utf-8")).hexdigest()[:32]
                if store.owner_notice_by_key(ik) is None:
                    store.add_owner_notice("规则闸门·连续3次未读放行", topic, alert_msg.encode("utf-8"), ik, "", False)
        except Exception:
            pass
        return GateCheckResult(
            allowed=True,
            exit_code=0,
            reason=alert_msg,
            strike_count=strike_count,
            auto_passed_after_strikes=True,
        )

    _inc_meta_counter(store, "observe:rule_unread_blocked")

    sdir = resolve_skill_dir(skill_dir, playbook_dir=playbook_dir)
    if detail == "bus_ref_unread":
        msg = (
            f"先完整读 {sdir / 'SKILL.md'} 与 {sdir / 'references' / 'bus.md'}"
            f"（规则版本 {state.version}），再重试，参数不用改。"
        )
    else:
        msg = (
            f"先完整读 {sdir / 'SKILL.md'}"
            f"（规则版本 {state.version}），再重试，参数不用改。"
        )
    return GateCheckResult(
        allowed=False,
        exit_code=RULE_GATE_EXIT_CODE,
        reason=msg,
        strike_count=strike_count,
    )


def parse_no_reply_items(
    no_reply_args: list[str] | None = None,
    no_reply_json: Any = None,
) -> dict[str, str]:
    """Parse `--no-reply '<mid>:<why>'` and/or JSON `[{"id": "...", "why": "..."}]`
    into `{message_id: why}`.
    """
    result: dict[str, str] = {}
    if no_reply_args:
        for item in no_reply_args:
            raw = str(item or "").strip()
            if not raw:
                continue
            if ":" in raw:
                mid, why = raw.split(":", 1)
                mid, why = mid.strip(), why.strip()
                if mid and why:
                    result[mid] = why
    if isinstance(no_reply_json, str) and no_reply_json.strip():
        try:
            no_reply_json = json.loads(no_reply_json)
        except Exception:
            no_reply_json = None
    if isinstance(no_reply_json, list):
        for entry in no_reply_json:
            if isinstance(entry, dict):
                mid = str(entry.get("id") or entry.get("message_id") or "").strip()
                why = str(entry.get("why") or entry.get("reason") or "").strip()
                if mid and why:
                    result[mid] = why
    elif isinstance(no_reply_json, dict):
        for k, v in no_reply_json.items():
            mid, why = str(k).strip(), str(v).strip()
            if mid and why:
                result[mid] = why
    return result


def find_matching_send_for_owner_msg(
    store: Any,
    *,
    topic: str,
    owner_msg: Any,
    since_iso: str,
) -> dict[str, Any] | None:
    """Return a matching `sends` record (`ask / report / reply`) sent by `topic`
    at or after `since_iso` that responds to `owner_msg`, or None.
    Matches:
    - `kind in ('ask', 'report')` sent strictly after `since_iso` (or not the escalation quoted by `owner_msg`), OR
    - `kind == 'reply'` where `target_message_id` is `owner_msg['message_id']`
      or shares the same thread / root_id / reply_to chain (excluding an earlier message that `owner_msg` itself quoted).
    """
    if store is None or owner_msg is None:
        return None
    m_keys = set(owner_msg.keys()) if hasattr(owner_msg, "keys") else set()
    mid = str((owner_msg["message_id"] if "message_id" in m_keys else "") or "").strip()
    thread_id = str((owner_msg["thread_id"] if "thread_id" in m_keys else "") or "").strip()
    root_id = str((owner_msg["root_id"] if "root_id" in m_keys else "") or "").strip()
    reply_to = str((owner_msg["reply_to"] if "reply_to" in m_keys else "") or "").strip()
    tags_raw = str((owner_msg["tags_json"] if "tags_json" in m_keys else "") or "")

    since_epoch = _parse_iso_to_epoch(since_iso)
    base_topic = (topic or "").split("#", 1)[0]

    rows = store.conn.execute(
        """SELECT idem_key, kind, topic_id, message_id, root_message_id, target_message_id, text, sent_at
           FROM sends
           WHERE (topic_id = ? OR topic_id = ? OR topic_id LIKE ?)
             AND kind IN ('ask', 'report', 'reply')
           ORDER BY sent_at DESC, rowid DESC""",
        (topic, base_topic, f"{base_topic}#%"),
    ).fetchall()

    o_rowid = 0
    if mid:
        try:
            o_r = store.conn.execute("SELECT rowid FROM messages WHERE message_id = ?", (mid,)).fetchone()
            if o_r is not None:
                o_rowid = int(o_r[0])
        except Exception:
            o_rowid = 0
    quoted_or_ancestor_ids = {x for x in (mid, reply_to, root_id) if x}
    thread_anchors = {x for x in (mid, thread_id, root_id, reply_to) if x}

    for r in rows:
        row = dict(r)
        send_mid = str(row.get("message_id") or "").strip()
        if send_mid and (send_mid in quoted_or_ancestor_ids or f"escalation:{send_mid}" in tags_raw):
            continue
        if send_mid and o_rowid > 0:
            try:
                s_r = store.conn.execute("SELECT rowid FROM messages WHERE message_id = ?", (send_mid,)).fetchone()
                if s_r is not None and int(s_r[0]) <= o_rowid:
                    continue
            except Exception:
                pass
        sent_epoch = _parse_iso_to_epoch(str(row.get("sent_at") or ""))
        if since_epoch > 0 and sent_epoch > 0 and sent_epoch + 0.001 < since_epoch:
            continue
        kind = str(row.get("kind") or "")
        if kind in ("ask", "report"):
            if since_epoch > 0 and sent_epoch <= since_epoch:
                continue
            return row
        if kind == "reply":
            target_mid = str(row.get("target_message_id") or "").strip()
            if target_mid and target_mid in thread_anchors:
                return row
            if target_mid:
                t_msg = store.get_message(target_mid)
                if t_msg is not None:
                    tk = set(t_msg.keys()) if hasattr(t_msg, "keys") else set()
                    t_thread = str((t_msg["thread_id"] if "thread_id" in tk else "") or "").strip()
                    t_root = str((t_msg["root_id"] if "root_id" in tk else "") or "").strip()
                    t_rep = str((t_msg["reply_to"] if "reply_to" in tk else "") or "").strip()
                    if (
                        (thread_id and t_thread == thread_id)
                        or (root_id and t_root == root_id)
                        or (mid and mid in (t_thread, t_root, t_rep))
                    ):
                        return row
    return None


def is_owner_echo_required_message(msg_row: Any) -> bool:
    """Return True if `msg_row` is an owner_reply or owner_instruction (excluding recalled/edited)."""
    if msg_row is None:
        return False
    keys = set(msg_row.keys()) if hasattr(msg_row, "keys") else set()
    reason = str((msg_row["reason"] if "reason" in keys else "") or "").strip()
    if reason in ("recalled", "edited"):
        return False
    tags_raw = str((msg_row["tags_json"] if "tags_json" in keys else "") or "[]")
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else list(tags_raw)
    except Exception:
        tags = []
    if reason in ("owner_reply", "owner_instruction", "owner_instruction_private"):
        return True
    if "owner_instruction" in tags or ("owner_verified" in tags and reason != "owner_context"):
        return True
    return False


def check_owner_echo_gate(
    store: Any,
    *,
    batch_id: str,
    topic: str,
    status: str,
    no_reply_map: dict[str, str] | None = None,
    extra_batch_ids: list[str] | None = None,
) -> GateCheckResult:
    """Verify that before `receipt` (`handled` / `done`) succeeds, every
    `owner_reply` / `owner_instruction` message in `batch_id` (plus any `extra_batch_ids`)
    either has a matching `sends` (`ask / report / reply`) record or an explicit `no_reply` reason.
    """
    if status not in ("handled", "done"):
        return GateCheckResult(allowed=True, exit_code=0, reason="status_exempt")
    if store is None or not batch_id or batch_id == "none":
        return GateCheckResult(allowed=True, exit_code=0, reason="no_batch")

    batch_ids = [batch_id] + [b for b in (extra_batch_ids or []) if b and b != batch_id]
    no_reply = no_reply_map or {}
    missing_mids: list[str] = []

    for bid in batch_ids:
        d_row = store.get_dispatch(bid)
        if d_row is None:
            continue
        try:
            mids = json.loads(d_row["message_ids"] or "[]")
        except Exception:
            mids = []
        sent_at_iso = str(d_row["sent_at"] or "")
        for mid in mids:
            m = store.get_message(mid)
            if not is_owner_echo_required_message(m):
                continue
            if mid in no_reply and no_reply[mid].strip():
                continue
            matched_send = find_matching_send_for_owner_msg(
                store, topic=topic, owner_msg=m, since_iso=sent_at_iso
            )
            if matched_send is None:
                missing_mids.append(mid)

    if not missing_mids:
        return GateCheckResult(allowed=True, exit_code=0, reason="all_owner_messages_echoed")

    _inc_meta_counter(store, "observe:owner_no_echo_blocked")

    joined = ", ".join(missing_mids)
    msg = (
        f"批次 {batch_id} 含有主人的话（{joined}）尚未回复。"
        f"请先用 `sheepdog reply / report / ask` 回复，或在回执 JSON 里写明不用回的原因："
        f'`"no_reply": [{{"id": "{missing_mids[0]}", "why": "<一句话原因>"}}]`（或传 `--no-reply "{missing_mids[0]}:<原因>"`）。'
    )
    return GateCheckResult(
        allowed=False,
        exit_code=RULE_GATE_EXIT_CODE,
        reason=msg,
    )


_TRAILING_TRUNCATED_RE = re.compile(
    r"<truncated\s+(\d+)\s+bytes>\s*(?:\n+NOTE:\s*The output was truncated[^\n]*)?\s*$"
)


def detect_step_tail_truncation(step_content: str) -> int | None:
    """Return truncated byte count if `step_content` ends with Antigravity's
    `<truncated N bytes>` marker, or None otherwise.
    Does not match `<truncated ...>` quoted inside normal message body text.
    """
    if not step_content:
        return None
    m = _TRAILING_TRUNCATED_RE.search(step_content)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def delivery_check_batch(
    store: Any,
    *,
    conversation_id: str,
    batch_id: str,
    brain_dir: Path | None = None,
) -> tuple[bool, int, str]:
    """Inspect `transcript_full.jsonl` (NEVER `transcript.jsonl`) after sending `batch_id`
    to `conversation_id`. Returns `(was_truncated, truncated_bytes, rescue_prompt)`.
    """
    if not conversation_id or not batch_id:
        return False, 0, ""
    tf_path = transcript_full_path(conversation_id, brain_dir=brain_dir)
    if not tf_path.exists():
        return False, 0, ""

    try:
        lines = tf_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, 0, ""

    for line in reversed(lines[-40:]):
        if not line.strip() or batch_id not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") not in ("USER_INPUT", "SYSTEM_MESSAGE"):
            continue
        content = str(obj.get("content") or "")
        if batch_id not in content:
            continue
        trunc_bytes = detect_step_tail_truncation(content)
        if trunc_bytes is not None:
            _inc_meta_counter(store, "observe:truncated_batches")
            rescue = (
                f"⚠️ [sheepdog 投递自检] 批次 {batch_id} 投递时被宿主截断了 {trunc_bytes} 字节。"
                f"请立即执行 `sheepdog show --batch {batch_id}` 补读完整信号后再处理。"
            )
            return True, trunc_bytes, rescue
        return False, 0, ""
    return False, 0, ""


def _inc_meta_counter(store: Any, key: str) -> int:
    if store is None:
        return 1
    try:
        raw = store.get_meta(key) or "0"
        val = (int(raw) if str(raw).isdigit() else 0) + 1
        store.set_meta(key, str(val))
        return val
    except Exception:
        return 1


def _latest_batch_key_for_topic(store: Any, topic: str) -> str:
    if store is None:
        return "none"
    try:
        t_row = store.get_topic(topic)
        if t_row is not None and "pending_batch_id" in t_row.keys() and t_row["pending_batch_id"]:
            return str(t_row["pending_batch_id"])
        row = store.conn.execute(
            "SELECT batch_id FROM dispatches WHERE topic_id = ? ORDER BY sent_at DESC, rowid DESC LIMIT 1",
            (topic,),
        ).fetchone()
        if row and row["batch_id"]:
            return str(row["batch_id"])
    except Exception:
        pass
    return "none"
