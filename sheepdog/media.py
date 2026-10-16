"""消息里的图片送进会话前先解开（7.54）。

账本里的图片只是一把飞书图片钥匙（post 里是 `![Image](img_…)`，图片消息和合并转发里是 `[Image: img_…]`，卡片里是
`(img_key:img_…)`），要配上消息编号和专门的下载命令才能变成图。会话拿到钥匙解不开：宿主界面显示「Preview unavailable」，
模型也看不见图。所以渲染要送进会话的消息时，先用用户身份下载到状态目录 `media/<message_id>/<钥匙>.<扩展名>`，
正文里那把钥匙换成本机绝对路径；同一张图只下一次（按路径缓存）。

下载失败不挡投递：原文照留，正文后面写明没下载下来的原因和会话自己去取的那条完整命令。单张有大小上限、单批最多下几张、
每张有超时（[media] 配置）。旧数据清理时 media 下对应的目录跟着已有的保留期一起清（Store.prune）。
只做图片，文件附件（file_…）不做。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# 三种写法：post 的 Markdown 图片、图片消息 / 合并转发里的 [Image: …]、卡片里的 (img_key:…)
_KEY = r"img_[A-Za-z0-9_-]+"
IMAGE_REF = re.compile(rf"!\[[^\]\n]*\]\((?P<md>{_KEY})\)|\[Image: (?P<tag>{_KEY})\]|\(img_key:(?P<card>{_KEY})\)")
# 能拿去下载的消息编号：飞书消息（om_），sheepdog 自己生成的系统消息（sd_…）不算
_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]+")
_SAFE_KEY = re.compile(rf"{_KEY}")
# 下载中途的临时目录前缀（在 media/<message_id>/ 下）；缓存只认改好名的正式文件
_PART = ".part-"


def image_keys(content: str) -> list[str]:
    """正文里的图片钥匙，按出现先后、去重。"""
    out: list[str] = []
    for m in IMAGE_REF.finditer(content or ""):
        k = m.group("md") or m.group("tag") or m.group("card")
        if k not in out:
            out.append(k)
    return out


def downloadable(message_id: str) -> bool:
    return bool(_MESSAGE_ID.fullmatch(message_id or ""))


def fetch_command(message_id: str, key: str) -> str:
    """会话自己去取的那条完整命令（和 sheepdog 下载用的一样）。"""
    return f"lark-cli im +messages-resources-download --message-id {message_id} --file-key {key} --type image --as user"


@dataclass(frozen=True)
class ImageResult:
    key: str
    path: str = ""     # 下载成功（或缓存命中）：本机绝对路径
    error: str = ""    # 没下载下来的原因
    cached: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.path)


def cached_path(media_dir: Path, message_id: str, key: str) -> Path | None:
    """缓存命中：media/<message_id>/ 下已有 <钥匙>.<扩展名>（或没扩展名的 <钥匙>）。"""
    d = Path(media_dir) / message_id
    if not d.is_dir():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and (p.name == key or p.name.startswith(key + ".")):
            return p.resolve()
    return None


class ImageFetcher:
    """用 lark-cli（用户身份）把一张图下到 media/<message_id>/。lark-cli 不带 --output 时下到当前目录、文件名是
    <钥匙>.<扩展名>：子进程的 cwd 指到一个临时目录（不改本进程的工作目录），下完核大小、改名放进正式位置。"""

    def __init__(self, media_dir: Path, binary: str = "lark-cli", max_bytes: int = 20 * 1024 * 1024,
                 timeout: float = 30):
        self.media_dir = Path(media_dir)
        self.binary = shutil.which(binary) or binary
        self.max_bytes = max_bytes
        self.timeout = timeout
        # 实际调过几次 lark-cli（测试用来核「同一张不重复下载」）
        self.downloads = 0

    def cached(self, message_id: str, key: str) -> ImageResult | None:
        try:
            p = cached_path(self.media_dir, message_id, key)
        except OSError:
            return None
        return ImageResult(key, path=str(p), cached=True) if p is not None else None

    def fetch(self, message_id: str, key: str) -> ImageResult:
        if not downloadable(message_id) or not _SAFE_KEY.fullmatch(key or ""):
            return ImageResult(key, error=f"消息编号或钥匙不是飞书的格式（{message_id} / {key}）")
        hit = self.cached(message_id, key)
        if hit is not None:
            return hit
        dest = self.media_dir / message_id
        try:
            dest.mkdir(parents=True, exist_ok=True)
            part = Path(tempfile.mkdtemp(prefix=f"{_PART}{key}-", dir=dest))
        except OSError as e:
            return ImageResult(key, error=f"建不了目录 {dest}：{e}")
        try:
            return self._download(message_id, key, dest, part)
        except OSError as e:   # 读临时目录、改名出错：当这张没下载下来，不挡投递
            return ImageResult(key, error=f"存图片出错：{e}")
        finally:
            shutil.rmtree(part, ignore_errors=True)

    def _download(self, message_id: str, key: str, dest: Path, part: Path) -> ImageResult:
        self.downloads += 1
        args = [self.binary, "im", "+messages-resources-download", "--message-id", message_id, "--file-key", key,
                "--type", "image", "--as", "user"]
        try:
            proc = subprocess.run(args, cwd=part, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return ImageResult(key, error=f"下载超时（{self.timeout:g} 秒）")
        except OSError as e:
            return ImageResult(key, error=f"跑不了 lark-cli：{e}")
        if proc.returncode != 0:
            return ImageResult(key, error=f"lark-cli 失败：{_error_text(proc)}")
        files = [p for p in part.iterdir() if p.is_file()]
        if len(files) != 1:
            return ImageResult(key, error=f"lark-cli 说成功了，但临时目录里有 {len(files)} 个文件")
        got = files[0]
        size = got.stat().st_size
        if size > self.max_bytes:
            return ImageResult(key, error=f"图片 {size / 1024 / 1024:.1f}MB，超过单张上限 "
                                          f"{self.max_bytes / 1024 / 1024:g}MB，没留")
        final = dest / (key + got.suffix)
        os.replace(got, final)
        return ImageResult(key, path=str(final.resolve()))


def _error_text(proc: subprocess.CompletedProcess) -> str:
    out = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    try:
        err = (json.loads(out) or {}).get("error") or {}
        msg = err.get("message") or ""
        if msg:
            return f"{err.get('type')}/{err.get('subtype')}: {msg}"[:300]
    except (ValueError, AttributeError):
        pass
    return f"rc={proc.returncode} {' '.join(out.split())[:300]}"


def resolve(rows, fetcher: ImageFetcher | None, max_downloads: int, off_reason: str = "") -> dict[str, dict]:
    """一批要送的消息里的图片：{message_id: {钥匙: ImageResult}}。缓存命中不算下载次数；这一批真去下载的最多
    max_downloads 张，超过的写明没下载。fetcher 为 None（试运行、配置关了）时都不下载，原因写 off_reason。
    sheepdog 自己生成的系统消息（编号不是 om_）不处理。"""
    out: dict[str, dict] = {}
    budget = max_downloads
    for r in rows:
        mid = r["message_id"]
        keys = image_keys(r["content"] or "") if downloadable(mid) else []
        if not keys:
            continue
        res: dict[str, ImageResult] = {}
        for k in keys:
            if fetcher is None:
                res[k] = ImageResult(k, error=off_reason or "这个进程不下载图片")
                continue
            hit = fetcher.cached(mid, k)
            if hit is not None:
                res[k] = hit
            elif budget <= 0:
                res[k] = ImageResult(k, error=f"这一批要下载的图片超过 {max_downloads} 张，这张没下载")
            else:
                budget -= 1
                res[k] = fetcher.fetch(mid, k)
        out[mid] = res
    return out


def local_note(path: str) -> str:
    return f"[图片已存到本机：{path}，用看图工具打开]"


def apply(content: str, message_id: str, results: dict | None) -> tuple[str, list[str]]:
    """正文里下载到了的图片钥匙换成本机路径（那一处换成「图片已存到本机：<路径>，用看图工具打开」）；没下载下来的原样留着，
    另外返回几行说明（原因 + 会话自己去取的命令），放在正文后面。"""
    if not results:
        return content, []

    def sub(m: re.Match) -> str:
        r = results.get(m.group("md") or m.group("tag") or m.group("card"))
        return local_note(r.path) if r is not None and r.ok else m.group(0)

    notes = [f"🖼 图片 `{r.key}` 没下载到本机（{r.error}）。要看就自己取：`{fetch_command(message_id, r.key)}`"
             for r in results.values() if not r.ok]
    return IMAGE_REF.sub(sub, content), notes


def prune(media_dir: Path, keep_ids: set[str], older_than: float) -> int:
    """旧数据清理（跟着 retention_days）：media/<message_id>/ 对应的消息已经不在账本里、目录又超过保留期，就整个删。
    返回删了几个目录。"""
    d = Path(media_dir)
    if not d.is_dir():
        return 0
    n = 0
    for sub in d.iterdir():
        if sub.is_dir() and sub.name not in keep_ids and sub.stat().st_mtime < older_than:
            shutil.rmtree(sub, ignore_errors=True)
            n += 1
    return n
