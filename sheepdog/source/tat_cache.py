"""飞书/Lark bot tenant_access_token 跨进程文件锁缓存与瞬态错误判定（规格 7.80，REG-031）。"""
from __future__ import annotations

from collections.abc import Mapping
import fcntl, json, logging, os, time, urllib.request
from pathlib import Path
from typing import Any

from .feishu_ws import load_feishu_app_credentials

log = logging.getLogger("sheepdog")

DEFAULT_CACHE_PATH = Path.home() / ".local" / "state" / "sheepdog" / "tat_cache.json"
REFRESH_AHEAD_SECONDS = 600
_TOKEN_INVALID_CODES = ("99991663", "99991668", "99991661")
_TRANSIENT_MARKERS = ("rate_limit", "429", "99991400", "too many requests", "frequency limit", "tat endpoint",
                      "type=network", "network/", "timeout", "timed out", "tls handshake", "connection reset",
                      "temporary failure", "eof")


def fetch_bot_tat(app_id: str, app_secret: str, domain: str = "https://open.larksuite.com",
                  timeout: float = 5.0) -> tuple[str, int]:
    url = f"{domain.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = str(data.get("tenant_access_token") or "").strip()
    if data.get("code") != 0 or not token:
        raise RuntimeError(f"fetch_bot_tat failed: code={data.get('code')} msg={data.get('msg')}")
    return token, int(data.get("expire") or 7200)


def get_bot_tat(*, cache_path: Path | None = None, force_refresh: bool = False,
                creds_loader=None, fetcher=None) -> tuple[str, str, str] | None:
    try:
        creds = (creds_loader or load_feishu_app_credentials)()
        if not creds or not creds.get("app_id") or not creds.get("app_secret"):
            return None
        app_id, app_secret = str(creds["app_id"]).strip(), str(creds["app_secret"]).strip()
        domain = str(creds.get("domain") or "https://open.larksuite.com").strip().rstrip("/")
        brand = "feishu" if "feishu.cn" in domain else "lark"
        path = Path(cache_path) if cache_path is not None else DEFAULT_CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                if not force_refresh and path.is_file():
                    try:
                        cached = json.loads(path.read_text(encoding="utf-8"))
                        if (isinstance(cached, dict) and cached.get("app_id") == app_id
                                and cached.get("brand") == brand and cached.get("token")
                                and float(cached.get("expire_at") or 0) - time.time() > REFRESH_AHEAD_SECONDS):
                            return str(cached["token"]), app_id, brand
                    except Exception:
                        pass
                token, expire = (fetcher or fetch_bot_tat)(app_id, app_secret, domain)
                now = time.time()
                tmp = path.with_suffix(f".tmp.{os.getpid()}")
                payload = {"app_id": app_id, "brand": brand, "token": token,
                           "expire_at": now + int(expire), "updated_at": now}
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                tmp.chmod(0o600)
                tmp.replace(path)
                return token, app_id, brand
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        log.debug("get_bot_tat failed: %s", e)
        return None


def invalidate_tat_cache(*, cache_path: Path | None = None) -> None:
    try:
        path = Path(cache_path) if cache_path is not None else DEFAULT_CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                path.unlink(missing_ok=True)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        log.debug("invalidate_tat_cache failed: %s", e)


def inject_bot_tat_env(base_env: Mapping[str, str] | None = None, *, cache_path: Path | None = None,
                       force_refresh: bool = False, creds_loader=None, fetcher=None) -> dict[str, str] | None:
    src = os.environ if base_env is None else base_env
    if not force_refresh and src.get("LARKSUITE_CLI_TENANT_ACCESS_TOKEN"):
        return dict(src)
    if (got := get_bot_tat(cache_path=cache_path, force_refresh=force_refresh,
                           creds_loader=creds_loader, fetcher=fetcher)) is None:
        return None
    token, app_id, brand = got
    env = dict(src)
    env["LARKSUITE_CLI_TENANT_ACCESS_TOKEN"] = token
    env.setdefault("LARKSUITE_CLI_APP_ID", app_id)
    env.setdefault("LARKSUITE_CLI_BRAND", brand)
    return env


def is_token_invalid_error(err: Any) -> bool:
    s = (json.dumps(err, ensure_ascii=False) if isinstance(err, dict) else str(err or "")).lower()
    if any(code in s for code in _TOKEN_INVALID_CODES):
        return True
    return "tenant_access_token" in s and ("invalid" in s or "expired" in s)


def is_transient_lark_error(exc: BaseException) -> bool:
    if exc.__class__.__name__ == "LarkTimeout":
        return True
    s = str(exc).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)
