"""7.54 会话看得见消息里的图片：送进会话前先下载到状态目录 media/<message_id>/，正文里换成本机路径。

假的 lark-cli 不比真的宽容（10-17 03:09 对真 lark-cli 1.0.95 核过）：
- 少了 --message-id / --file-key / --type 任何一个：退出码 2，stderr 是 JSON 错误「required flag(s) "…" not set」
- --type 只认 image / file；--message-id 要 om_ 开头；不认识的参数报错
- 成功：不带 --output 时下到当前目录（子进程的 cwd），文件名 <钥匙>.<扩展名>；stdout 是
  {"ok": true, "data": {"saved_path": 绝对路径, "size_bytes": n}}
- 钥匙和消息对不上：退出码 4，「File not in msg.」
全部使用合成数据（*_test_*）。
"""

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sheepdog import cli, media
from sheepdog.engine import BUS_TOPIC_ID
from sheepdog.roster import Roster

from test_roster import Base, msg

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
plan = json.load(open(os.path.join(HERE, "plan.json")))
with open(os.path.join(HERE, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")


def fail(rc, typ, sub, message, **extra):
    err = {"type": typ, "subtype": sub, "message": message}
    err.update(extra)
    sys.stderr.write(json.dumps({"ok": False, "identity": "user", "error": err}, indent=2) + "\n")
    sys.exit(rc)


argv = sys.argv[1:]
if argv[:2] != ["im", "+messages-resources-download"]:
    fail(2, "validation", "invalid_argument", "unknown command %r" % argv[:2])
flags, i = {}, 2
known = {"--message-id", "--file-key", "--type", "--as", "--output"}
while i < len(argv):
    a = argv[i]
    if a not in known:
        fail(2, "validation", "invalid_argument", "unknown flag: %s" % a)
    if i + 1 >= len(argv):
        fail(2, "validation", "invalid_argument", "flag needs an argument: %s" % a)
    flags[a] = argv[i + 1]
    i += 2
for need in ("message-id", "file-key", "type"):
    if "--" + need not in flags:
        fail(2, "validation", "invalid_argument", 'required flag(s) "%s" not set' % need)
if flags["--type"] not in ("image", "file"):
    fail(2, "validation", "invalid_argument", 'invalid value "%s" for --type, allowed: image, file' % flags["--type"],
         param="--type")
if not flags["--message-id"].startswith("om_"):
    fail(2, "validation", "invalid_argument", 'invalid message ID "%s": must start with om_' % flags["--message-id"],
         param="--message-id")
if flags.get("--as", "user") not in ("user", "bot"):
    fail(2, "validation", "invalid_argument", "invalid identity %r" % flags["--as"])
key = flags["--file-key"]
p = plan.get(key)
if p is None or p.get("message_id", flags["--message-id"]) != flags["--message-id"]:
    fail(4, "network", "transport", 'HTTP 400: {"code":234003,"msg":"File not in msg."}', code=400)
if p.get("sleep"):
    time.sleep(p["sleep"])
if p.get("error"):
    fail(p.get("rc", 4), "api", "error", p["error"])
out = os.path.join(os.getcwd(), key + p.get("ext", ".jpg"))
with open(out, "wb") as f:
    f.write(b"\xff\xd8" + b"x" * (p.get("size", 100) - 2))
print(json.dumps({"ok": True, "identity": "user", "data": {"saved_path": out, "size_bytes": p.get("size", 100)},
                  "_notice": {"update": {"current": "1.0.95"}}}, indent=2))
'''


def fake_cli(d: Path, plan: dict) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    script = d / "lark-cli"
    script.write_text(FAKE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def calls(script: Path) -> list[dict]:
    f = script.parent / "calls.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


class FakeIsNotLenientTest(unittest.TestCase):
    """假命令和真命令一样严：少参数、错类型、错消息编号都失败，什么都不写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = fake_cli(Path(self.tmp.name) / "bin", {"img_test_a": {"size": 10}})
        self.cwd = Path(self.tmp.name) / "cwd"
        self.cwd.mkdir()

    def run_fake(self, *args):
        return subprocess.run([str(self.script), "im", "+messages-resources-download", *args], cwd=self.cwd,
                              capture_output=True, text=True)

    def test_missing_or_bad_flags_rejected(self):
        full = {"--message-id": "om_test_m", "--file-key": "img_test_a", "--type": "image"}
        for drop in full:
            args = [x for k, v in full.items() if k != drop for x in (k, v)]
            p = self.run_fake(*args, "--as", "user")
            self.assertEqual(p.returncode, 2, drop)
            self.assertIn("required flag(s)", json.loads(p.stderr)["error"]["message"])
        for bad in (["--type", "picture"], ["--message-id", "xx_1"]):
            args = dict(full)
            args[bad[0]] = bad[1]
            p = self.run_fake(*[x for kv in args.items() for x in kv])
            self.assertEqual(p.returncode, 2, bad)
        self.assertEqual(list(self.cwd.iterdir()), [])
        ok = self.run_fake(*[x for kv in full.items() for x in kv], "--as", "user")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual([p.name for p in self.cwd.iterdir()], ["img_test_a.jpg"])   # 下到当前目录


class ImageRefTest(unittest.TestCase):
    def test_three_forms(self):
        text = ("看这个\n![Image](img_v3_test_a-1)\n[Image: img_v3_test_b]\n截图(img_key:img_v3_test_c)"
                "\n![Image](img_v3_test_a-1)\n附件 file_v3_test_x")
        self.assertEqual(media.image_keys(text), ["img_v3_test_a-1", "img_v3_test_b", "img_v3_test_c"])
        self.assertEqual(media.image_keys("没有图 img_ 也不是钥匙"), [])

    def test_only_feishu_message_ids(self):
        self.assertTrue(media.downloadable("om_test_x1"))
        for bad in ("sd_escalation_list_1", "", "om_../x", "om_x/y"):
            self.assertFalse(media.downloadable(bad), bad)


class MediaConfigTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        from sheepdog.config import ConfigError, MediaConfig
        m = MediaConfig()
        self.assertEqual((m.images, m.max_image_mb, m.max_images_per_batch, m.image_timeout_seconds), (True, 20, 10, 30))
        for bad in ({"images": "yes"}, {"max_image_mb": 0}, {"max_images_per_batch": 0}, {"max_images_per_batch": 1.5},
                    {"image_timeout_seconds": -1}, {"max_images_per_batch": True}):
            with self.assertRaises(ConfigError, msg=bad):
                MediaConfig(**bad).validate()


class ImageDeliveryTest(Base):
    """消息走真正的投递路径（Collector 入账 → Dispatcher 组批 → 渲染）进总线会话。"""

    def setUp(self):
        super().setUp()
        self.script = fake_cli(Path(self.tmp.name) / "bin", {
            "img_v3_test_ok": {"size": 2048, "ext": ".jpg"},
            "img_v3_test_png": {"size": 300, "ext": ".png"},
            "img_v3_test_card": {"size": 300, "ext": ".png"},
            "img_v3_test_big": {"size": 4096},
            "img_v3_test_slow": {"sleep": 5},
            "img_v3_test_gone": {"error": "Attachment unavailable"},
            "img_v3_test_1": {}, "img_v3_test_2": {}, "img_v3_test_3": {},
        })

    def disp_images(self, roster=None):
        d = self.disp(roster if roster is not None else Roster())
        d.image_binary = str(self.script)
        return d

    def deliver(self, content, mid="om_test_img1", msg_type="post", d=None):
        t = self.store.get_topic(BUS_TOPIC_ID)
        if t is not None and t["pending_batch_id"]:
            self.ack(BUS_TOPIC_ID)   # 上一批交了回执，这一条才会送
        self.poll([msg(message_id=mid, chat_type="p2p", chat_id="oc_test_stranger", content=content,
                       msg_type=msg_type)])
        d = d or self.disp_images()
        before = len(self.sink.to("conv_test_bus"))
        d.dispatch_once()
        sent = self.sink.to("conv_test_bus")
        self.assertEqual(len(sent), before + 1)
        self.assertIn(f"message_id: `{mid}`", sent[-1])
        return d, sent[-1]

    def media_file(self, mid, name):
        return (self.cfg.media_dir / mid / name).resolve()

    def test_regression_7_54_image_key_replaced_with_local_path(self):
        """回归 7.54：送进会话的图片是一把会话解不开的飞书钥匙（宿主显示 Preview unavailable）→ 换成本机绝对路径。"""
        _, prompt = self.deliver("![Image](img_v3_test_ok)\n起一个专门的session处理跟进一下")
        path = self.media_file("om_test_img1", "img_v3_test_ok.jpg")
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_size, 2048)
        self.assertTrue(path.is_absolute())
        self.assertIn(f"> [图片已存到本机：{path}，用看图工具打开]\n> 起一个专门的session处理跟进一下", prompt)
        self.assertNotIn("![Image](", prompt)
        self.assertNotIn("没下载到本机", prompt)
        (c,) = calls(self.script)
        self.assertEqual(c["argv"], ["im", "+messages-resources-download", "--message-id", "om_test_img1",
                                     "--file-key", "img_v3_test_ok", "--type", "image", "--as", "user"])
        # 子进程的 cwd 指到 media/<message_id>/ 下的临时目录，下完临时目录删掉；本进程的工作目录没动
        self.assertEqual(Path(c["cwd"]).resolve().parent, (self.cfg.media_dir / "om_test_img1").resolve())
        self.assertEqual(sorted(p.name for p in (self.cfg.media_dir / "om_test_img1").iterdir()), ["img_v3_test_ok.jpg"])

    def test_image_message_and_card_forms(self):
        _, prompt = self.deliver("[Image: img_v3_test_png]", msg_type="image")
        self.assertIn(f"[图片已存到本机：{self.media_file('om_test_img1', 'img_v3_test_png.png')}", prompt)
        self.assertNotIn("[Image: img_", prompt)
        _, prompt = self.deliver("<card title=\"x\">\n用例 1 截图(img_key:img_v3_test_card)", mid="om_test_card1",
                                 msg_type="interactive")
        self.assertIn(f"用例 1 截图[图片已存到本机：{self.media_file('om_test_card1', 'img_v3_test_card.png')}",
                      prompt)

    def test_failure_keeps_original_with_reason_and_command(self):
        """下载失败不挡投递：原文照留，写明原因和会话自己去取的那条完整命令。"""
        d, prompt = self.deliver("一直在查\n![Image](img_v3_test_gone)")
        self.assertIn("> ![Image](img_v3_test_gone)", prompt)
        self.assertIn("Attachment unavailable", prompt)
        self.assertIn("`lark-cli im +messages-resources-download --message-id om_test_img1 --file-key "
                      "img_v3_test_gone --type image --as user`", prompt)
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["state"], "running")   # 照常送达
        self.assertFalse(any((self.cfg.media_dir / "om_test_img1").iterdir()))    # 没留半截文件

    def test_unknown_key_fails_like_real_cli(self):
        _, prompt = self.deliver("![Image](img_v3_test_nope)")
        self.assertIn("File not in msg.", prompt)
        self.assertIn("--file-key img_v3_test_nope --type image --as user`", prompt)

    def test_same_image_not_downloaded_twice(self):
        """按路径缓存：同一张图（重投、再被渲染）只下一次。"""
        d, first = self.deliver("![Image](img_v3_test_ok)")
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.update_topic(BUS_TOPIC_ID, dispatched_at=old)
        d.dispatch_once()   # 回执超时重投，同一条消息再渲染一次
        sent = self.sink.to("conv_test_bus")
        self.assertEqual(len(sent), 2)
        path = self.media_file("om_test_img1", "img_v3_test_ok.jpg")
        self.assertIn(f"[图片已存到本机：{path}", sent[-1])
        self.assertEqual(len(calls(self.script)), 1)
        # 新起一个进程（新的 Dispatcher）也认缓存
        again = self.disp_images()._images([self.row("om_test_img1")])
        self.assertTrue(again["om_test_img1"]["img_v3_test_ok"].cached)
        self.assertEqual(len(calls(self.script)), 1)

    def test_over_size_limit_skipped(self):
        self.cfg.media.max_image_mb = 1 / 1024   # 1KB
        _, prompt = self.deliver("![Image](img_v3_test_big)")
        self.assertIn("超过单张上限", prompt)
        self.assertIn("> ![Image](img_v3_test_big)", prompt)
        self.assertIn("--file-key img_v3_test_big --type image --as user`", prompt)
        self.assertEqual(list((self.cfg.media_dir / "om_test_img1").iterdir()), [])

    def test_per_batch_download_cap(self):
        self.cfg.media.max_images_per_batch = 2
        _, prompt = self.deliver("![Image](img_v3_test_1)\n![Image](img_v3_test_2)\n![Image](img_v3_test_3)")
        self.assertEqual(len(calls(self.script)), 2)
        self.assertIn("这一批要下载的图片超过 2 张，这张没下载", prompt)
        self.assertIn("--file-key img_v3_test_3 --type image --as user`", prompt)
        self.assertIn("> ![Image](img_v3_test_3)", prompt)
        self.assertNotIn("![Image](img_v3_test_1)", prompt)

    def test_timeout(self):
        self.cfg.media.image_timeout_seconds = 0.5
        t0 = time.monotonic()
        _, prompt = self.deliver("![Image](img_v3_test_slow)")
        self.assertLess(time.monotonic() - t0, 4)
        self.assertIn("下载超时（0.5 秒）", prompt)
        self.assertIn("> ![Image](img_v3_test_slow)", prompt)

    def test_not_wired_or_switched_off_gives_command(self):
        """不下载的进程（试运行、没接上）和配置关掉时：不调 lark-cli，正文后面照样有会话自己去取的命令。"""
        _, prompt = self.deliver("![Image](img_v3_test_ok)", d=self.disp(Roster()))
        self.assertIn("这个进程不下载图片", prompt)
        self.assertIn("--file-key img_v3_test_ok --type image --as user`", prompt)
        self.cfg.media.images = False
        _, prompt = self.deliver("![Image](img_v3_test_ok)", mid="om_test_img2")
        self.assertIn("[media] images = false", prompt)
        self.assertEqual(calls(self.script), [])

    def test_owner_priority_batch_also_resolves(self):
        """主人插话批次（7.37 第 4 件）走同一套渲染：图片同样换成本机路径。"""
        from sheepdog.prompts import priority_prompt
        self.poll([msg(message_id="om_test_pri", chat_type="p2p", chat_id="oc_test_stranger",
                       content="![Image](img_v3_test_ok) 这个")])
        d = self.disp_images()
        row = self.row("om_test_pri")
        text = priority_prompt(d.playbook, BUS_TOPIC_ID, "p_test", "b_test", [row], "总线", images=d._images([row]))
        self.assertIn(f"> [图片已存到本机：{self.media_file('om_test_pri', 'img_v3_test_ok.jpg')}，用看图工具打开] 这个",
                      text)
        src = (Path(__file__).resolve().parent.parent / "sheepdog" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("images=self._images(urgent)", src)   # 插话那条路确实把结果传进去

    def test_cli_wiring_run_poll_download_dry_run_does_not(self):
        """常驻进程和 poll 接上 lark-cli；--dry-run 不下载，正文后面写明是试运行。"""
        from argparse import Namespace

        class Src:
            binary = "/opt/test/lark-cli"
        d = self.disp(Roster())
        cli._wire_images(d, Src(), Namespace(dry_run=True))
        self.assertEqual((d.image_binary, d.image_off_reason), ("", "试运行（--dry-run）不下载图片"))
        d = self.disp(Roster())
        cli._wire_images(d, Src(), Namespace(dry_run=False))
        self.assertEqual(d.image_binary, "/opt/test/lark-cli")
        d = self.disp(Roster())
        cli._wire_images(d, object(), Namespace())          # 测试环境的 NullSource 没有 binary：用 PATH 上的 lark-cli
        self.assertEqual(d.image_binary, "lark-cli")
        cli._wire_images(None, Src(), Namespace())          # poll --no-dispatch

    def test_system_rows_untouched(self):
        rows = [{"message_id": "sd_escalation_list_test", "content": "![Image](img_v3_test_ok)"}]
        self.assertEqual(self.disp_images()._images(rows), {})


class MediaPruneTest(Base):
    def test_prune_removes_media_of_pruned_messages(self):
        """旧数据清理跟着 retention_days（常驻进程每小时那次，不另起一套）：消息清出账本、目录超过保留期 →
        media/<message_id>/ 整个删；还在账本的、还没过保留期的留着。"""
        self.poll([msg(message_id="om_test_keep", chat_type="p2p", chat_id="oc_test_stranger")])
        md = self.cfg.media_dir
        for mid in ("om_test_keep", "om_test_gone", "om_test_fresh"):
            (md / mid).mkdir(parents=True)
            (md / mid / "img_v3_test.jpg").write_bytes(b"x")
        old = time.time() - 8 * 86400
        for mid in ("om_test_keep", "om_test_gone"):
            os.utime(md / mid, (old, old))
        out = cli._prune(self.store, self.cfg)
        self.assertEqual(out["media_dirs"], 1)
        self.assertEqual(sorted(p.name for p in md.iterdir()), ["om_test_fresh", "om_test_keep"])

if __name__ == "__main__":
    unittest.main()
