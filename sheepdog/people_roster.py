"""人物通讯录与助理身份契约（DEMO-149）。"""

from __future__ import annotations

import json, os, re, sqlite3, tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".local/state/sheepdog/sheepdog.sqlite3"
DEFAULT_CONFIG_PATH = Path.home() / ".config/sheepdog/config.toml"
DEFAULT_VAULT_PEOPLE_DIR = Path(
    os.environ.get("SHEEPDOG_VAULT_PEOPLE_DIR") or str(Path.home() / ".config/sheepdog/people")
)

_SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS people_roster (person_key TEXT PRIMARY KEY, open_id TEXT DEFAULT '', feishu_name TEXT DEFAULT '', preferred_name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]', p2p_chat_id TEXT DEFAULT '', role_hint TEXT DEFAULT '', updated_at TEXT NOT NULL);"
_TOVI_SAFE_RE = re.compile(r"(?<![A-Za-z0-9_])(?<!浩宇\()(?<!浩宇（)Tovi(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class PersonaContract:
    name: str = "Mattheuu"; owner: str = "Matthew"; role: str = "Matthew 的 AI 助理与工作分身"

    def to_dict(self) -> dict[str, str]: return asdict(self)

    @classmethod
    def from_config(cls, config_path: Path | str | None = None) -> "PersonaContract":
        candidates: list[Path] = [Path(config_path).expanduser()] if config_path else (([Path(os.environ["SHEEPDOG_CONFIG"]).expanduser()] if os.environ.get("SHEEPDOG_CONFIG") else []) + [DEFAULT_CONFIG_PATH])
        for p in list(candidates):
            if not p.exists(): continue
            try: data = tomllib.loads(p.read_text(encoding="utf-8"))
            except Exception: continue
            sec = data.get("persona")
            if isinstance(sec, dict):
                return cls(name=str(sec.get("name") or cls.name).strip() or cls.name, owner=str(sec.get("owner") or cls.owner).strip() or cls.owner, role=str(sec.get("role") or cls.role).strip() or cls.role)
            rpath = data.get("roster_path")
            if isinstance(rpath, str) and rpath.strip(): candidates.append(Path(rpath).expanduser())
            candidates.append(p.with_name("roster.toml"))
        return cls()


@dataclass(frozen=True)
class PersonRecord:
    person_key: str; open_id: str; feishu_name: str; preferred_name: str
    aliases: tuple[str, ...]; p2p_chat_id: str; role_hint: str; updated_at: str

    def to_dict(self) -> dict[str, object]: return {**asdict(self), "aliases": list(self.aliases)}
    def __getitem__(self, key: str): return getattr(self, key)
    def get(self, key: str, default=None): return getattr(self, key, default)


@dataclass(frozen=True)
class SpeakerProfile:
    speaker_raw: str; person_key: str; canonical_name: str; preferred_name: str
    role_hint: str; is_owner: bool; is_vip: bool; salutation: str; assistant_intro: str


class _SeedCatalog(tuple):
    def __contains__(self, item: object) -> bool: return super().__contains__(item) or any(r.person_key == item for r in self)
    def __getitem__(self, key):
        if isinstance(key, str):
            for r in self:
                if r.person_key == key: return r
            raise KeyError(key)
        return super().__getitem__(key)
    def get(self, key: str, default=None): return next((r for r in self if r.person_key == key), default)
    def keys(self): return [r.person_key for r in self]
    def values(self): return list(self)
    def items(self): return [(r.person_key, r) for r in self]


_SEED_TS = "2026-01-06T04:08:00Z"


def _seed_oid(key: str) -> str:
    return f"ou_test_seed_{key}" if key else ""


def _seed_cid(key: str) -> str:
    return f"oc_test_seed_{key}" if key else ""


BUILTIN_ROSTER_SEEDS = _SeedCatalog((
    PersonRecord("matthew_hong", _seed_oid("matthew_hong"), "Matthew Hong", "Matthew", ("matthew", "matthew hong", "柏一帆", "主人", "owner", "owner(matthew)"), _seed_cid("matthew_hong"), "owner:本人", _SEED_TS),
    PersonRecord("haoyu", "", "Tovi", "浩宇", ("浩宇", "tovi", "haoyu", "hao yu"), "", "colleague:同事", _SEED_TS),
    PersonRecord("gus_han", _seed_oid("gus_han"), "Gus Han", "Gus", ("gus", "gus han", "bygus"), _seed_cid("gus_han"), "colleague:同事", _SEED_TS),
    PersonRecord("florian_yuan", _seed_oid("florian_yuan"), "Florian Yuan", "思远", ("思远", "岳思远", "florian", "florian yuan", "tp_florian", "tp_florian-ops"), _seed_cid("florian_yuan"), "colleague:同事", _SEED_TS),
    PersonRecord("owen_wei", _seed_oid("owen_wei"), "Owen Wei", "阿魏", ("阿魏", "魏总", "owen wei", "a wei", "awei", "owen", "tp_awei", "tp_awei#2"), _seed_cid("owen_wei"), "vip:VIP", _SEED_TS),
    PersonRecord("garrett_rao", _seed_oid("garrett_rao"), "Garrett Rao", "饶总", ("饶总", "老饶", "garrett", "garrett rao"), _seed_cid("garrett_rao"), "vip:vip-b", _SEED_TS),
    PersonRecord("zhaoming_sui", _seed_oid("zhaoming_sui"), "Zhaoming Sui", "昭明", ("昭明", "隋昭明", "zhaoming", "zhaoming sui", "tp_zhaoming"), _seed_cid("zhaoming_sui"), "colleague:同事", _SEED_TS),
    PersonRecord("rui_song", _seed_oid("rui_song"), "Rui Song", "Rui", ("rui", "rui song", "瑞", "瑞哥", "阿瑞", "tp_ruisong"), _seed_cid("rui_song"), "colleague:同事", _SEED_TS),
    PersonRecord("miles_qiu", _seed_oid("miles_qiu"), "Miles Qiu", "Miles", ("miles", "miles qiu", "miles.c"), _seed_cid("miles_qiu"), "colleague:同事", _SEED_TS),
    PersonRecord("darcy_vale", _seed_oid("darcy_vale"), "Darcy Vale", "Darcy", ("darcy", "darcy vale", "tp_darcy", "tp_darcy-vale", "tp_darcy-collab"), _seed_cid("darcy_vale"), "vip:vip-c", _SEED_TS),
    PersonRecord("tao_noel", _seed_oid("tao_noel"), "Tao Noel", "Noel", ("noel", "tao noel"), _seed_cid("tao_noel"), "vip:vip-d", _SEED_TS),
    PersonRecord("pamela", _seed_oid("pamela"), "Pamela", "Pamela", ("pamela", "tp_pamela"), _seed_cid("pamela"), "vip:vip-e", _SEED_TS),
    PersonRecord("seth_wei", "", "Seth Wei", "Seth Wei", ("seth wei", "seth"), "", "colleague:同事", _SEED_TS),
    PersonRecord("caleb_bao", _seed_oid("caleb_bao"), "Caleb Bao", "Caleb", ("caleb bao", "caleb"), _seed_cid("caleb_bao"), "colleague:同事", _SEED_TS),
    PersonRecord("rj_leng", _seed_oid("rj_leng"), "RJ Leng", "RJ", ("rj", "rj leng"), _seed_cid("rj_leng"), "colleague:同事", _SEED_TS),
    PersonRecord("basil_ning", _seed_oid("basil_ning"), "Basil Ning", "Basil", ("basil ning", "basil"), _seed_cid("basil_ning"), "colleague:同事", _SEED_TS),
    PersonRecord("yichuan_xia", _seed_oid("yichuan_xia"), "Yichuan Xia", "Yichuan", ("yichuan xia", "yichuan"), _seed_cid("yichuan_xia"), "colleague:同事", _SEED_TS),
    PersonRecord("quill_vance", _seed_oid("quill_vance"), "Quill Vance", "Quill", ("quill vance", "quill"), _seed_cid("quill_vance"), "colleague:同事", _SEED_TS),
    PersonRecord("hazel_yu", _seed_oid("hazel_yu"), "Hazel Yu", "Hazel", ("hazel yu", "hazel"), _seed_cid("hazel_yu"), "colleague:同事", _SEED_TS),
))


def _effective_roster_seeds(db_path: Path | str | None = None) -> _SeedCatalog:
    """读取内置种子；若目标为本机默认状态库且存在 ~/.config/sheepdog/people_seeds.json，则合并本机真实 open_id/chat_id 覆盖。"""
    is_default_db = db_path is None and not os.environ.get("SHEEPDOG_STATE_DIR")
    if not is_default_db and db_path is not None:
        try:
            is_default_db = Path(db_path).expanduser().resolve() == DEFAULT_DB_PATH.resolve()
        except OSError:
            is_default_db = False
    if not is_default_db:
        return BUILTIN_ROSTER_SEEDS
    cfg_base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    local_file = cfg_base / "sheepdog" / "people_seeds.json"
    if not local_file.is_file():
        return BUILTIN_ROSTER_SEEDS
    try:
        raw_data = json.loads(local_file.read_text(encoding="utf-8"))
        if isinstance(raw_data, list):
            by_key = {str(it.get("person_key") or ""): it for it in raw_data if isinstance(it, dict) and it.get("person_key")}
        elif isinstance(raw_data, dict):
            by_key = {str(k): v for k, v in raw_data.items() if isinstance(v, dict)}
        else:
            return BUILTIN_ROSTER_SEEDS
        merged_records = []
        for s in BUILTIN_ROSTER_SEEDS:
            ov = by_key.get(s.person_key)
            if not ov:
                merged_records.append(s)
                continue
            merged_records.append(PersonRecord(
                s.person_key,
                str(ov.get("open_id") or s.open_id),
                str(ov.get("feishu_name") or s.feishu_name),
                str(ov.get("preferred_name") or s.preferred_name),
                tuple(str(x) for x in (ov.get("aliases") or s.aliases) if str(x).strip()),
                str(ov.get("p2p_chat_id") or s.p2p_chat_id),
                str(ov.get("role_hint") or s.role_hint),
                str(ov.get("updated_at") or s.updated_at),
            ))
        return _SeedCatalog(tuple(merged_records))
    except Exception:
        return BUILTIN_ROSTER_SEEDS


def _now_iso() -> str: return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _other_seed_terms(key: str) -> set[str]:
    return {t.strip().lower() for k2, s2 in BUILTIN_ROSTER_SEEDS.items() if k2 != key for t in (s2.preferred_name, s2.feishu_name, *s2.aliases) if t.strip()}


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path).expanduser() if db_path else (
        Path(os.environ["SHEEPDOG_STATE_DIR"]).expanduser() / "sheepdog.sqlite3" if os.environ.get("SHEEPDOG_STATE_DIR") else DEFAULT_DB_PATH
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA_SQL)
    return conn


def _merge_aliases(*groups) -> tuple[str, ...]:
    out, seen = [], set()
    for g in groups:
        for item in (g or ()):
            s = str(item).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower()); out.append(s)
    return tuple(out)


def _row_to_record(row: sqlite3.Row) -> PersonRecord:
    try: aliases = tuple(str(x) for x in json.loads(row["aliases_json"] or "[]") if str(x).strip())
    except Exception: aliases = ()
    return PersonRecord(row["person_key"], row["open_id"] or "", row["feishu_name"] or "",
                        row["preferred_name"], aliases, row["p2p_chat_id"] or "",
                        row["role_hint"] or "", row["updated_at"] or _SEED_TS)


def ensure_people_roster_seeded(db_path=None, *, vault_people_dir=None, roster_toml_path=None) -> list[PersonRecord]:
    now = _now_iso()
    seeds = _effective_roster_seeds(db_path)
    with _connect(db_path) as conn:
        existing = {r["person_key"]: _row_to_record(r) for r in conn.execute("SELECT * FROM people_roster").fetchall()}
        by_open_id = {r.open_id: r.person_key for r in existing.values() if r.open_id}
        for seed in seeds:
            key = seed.person_key
            other_seed_terms = _other_seed_terms(key)
            cur = existing.get(key)
            if cur is None:
                conn.execute("INSERT INTO people_roster VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (key, seed.open_id, seed.feishu_name, seed.preferred_name,
                              json.dumps(list(seed.aliases), ensure_ascii=False), seed.p2p_chat_id, seed.role_hint, now))
                existing[key] = seed
                if seed.open_id: by_open_id[seed.open_id] = key
            else:
                clean_aliases = [a for a in cur.aliases if a.strip().lower() not in other_seed_terms]
                merged = _merge_aliases(clean_aliases, seed.aliases)
                oid = "" if (key == "haoyu" and cur.open_id == seeds["gus_han"].open_id) else (cur.open_id or seed.open_id)
                cid = "" if (key == "haoyu" and cur.p2p_chat_id == seeds["gus_han"].p2p_chat_id) else (cur.p2p_chat_id or seed.p2p_chat_id)
                if cur.open_id and not oid: by_open_id.pop(cur.open_id, None)
                if oid: by_open_id[oid] = key
                rh = seed.role_hint if (not cur.role_hint or cur.role_hint in {"colleague", "colleague:p2p", f"colleague:{seed.feishu_name}"}) else cur.role_hint
                conn.execute("UPDATE people_roster SET open_id=?, feishu_name=?, aliases_json=?, p2p_chat_id=?, role_hint=? WHERE person_key=?",
                             (oid, cur.feishu_name or seed.feishu_name, json.dumps(list(merged), ensure_ascii=False), cid, rh, key))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
            q = "SELECT sender_id, sender_name, chat_id FROM messages WHERE chat_type='p2p' AND sender_id LIKE 'ou_%' AND sender_name != '' GROUP BY sender_id"
            try: m_rows = conn.execute(q).fetchall()
            except sqlite3.Error: m_rows = []
            for mr in m_rows:
                oid, sname, cid = mr["sender_id"], mr["sender_name"].strip(), mr["chat_id"] or ""
                if oid in by_open_id: continue
                slug = re.sub(r"[^a-z0-9_]+", "_", sname.lower()).strip("_") or f"p2p_{oid[-8:]}"
                if slug not in existing:
                    conn.execute("INSERT OR IGNORE INTO people_roster VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (slug, oid, sname, sname, json.dumps(list(_merge_aliases([sname.lower(), sname])), ensure_ascii=False), cid, "colleague:p2p", now))
        return [_row_to_record(r) for r in conn.execute("SELECT * FROM people_roster ORDER BY person_key").fetchall()]


def list_people_roster(db_path=None) -> list[PersonRecord]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM people_roster ORDER BY person_key").fetchall()
    return [_row_to_record(r) for r in rows] if rows else ensure_people_roster_seeded(db_path)


def lookup_person(query: str, db_path=None) -> PersonRecord | None:
    q = (query or "").strip()
    if not q: return None
    ql, records = q.lower(), list_people_roster(db_path)
    for rec in records:
        if ql in {rec.person_key.lower(), rec.open_id.lower(), rec.p2p_chat_id.lower(), rec.preferred_name.lower(), rec.feishu_name.lower()}:
            return rec
    for rec in records:
        if ql in {a.lower() for a in rec.aliases}: return rec
    oid_m = re.search(r"ou_[a-z0-9]{8,}", ql)
    if oid_m: return next((r for r in records if r.open_id.lower() == oid_m.group(0)), None)
    if "owner" in ql or "matthew" in ql: return next((r for r in records if r.person_key == "matthew_hong"), None)
    return None


def upsert_person_alias(db_path=None, *, target: str, preferred_name: str = "",
                        add_aliases: list[str] | tuple[str, ...] = (), feishu_name: str = "",
                        open_id: str = "", p2p_chat_id: str = "", role_hint: str = "") -> PersonRecord:
    ensure_people_roster_seeded(db_path)
    cur = lookup_person(target, db_path) or (lookup_person(open_id, db_path) if open_id else None)
    now, t_clean = _now_iso(), target.strip()
    if cur is not None:
        pkey, pref = cur.person_key, preferred_name.strip() or cur.preferred_name
        fname, oid = feishu_name.strip() or cur.feishu_name, open_id.strip() or cur.open_id
        cid, rh = p2p_chat_id.strip() or cur.p2p_chat_id, role_hint.strip() or cur.role_hint
        aliases = _merge_aliases(cur.aliases, [t_clean, pref, fname], add_aliases)
    else:
        pkey = re.sub(r"[^a-z0-9_]+", "_", (feishu_name or t_clean).lower()).strip("_") or f"person_{abs(hash(t_clean)) % 10000}"
        pref, fname = preferred_name.strip() or t_clean, feishu_name.strip() or t_clean
        oid, cid, rh = open_id.strip(), p2p_chat_id.strip(), role_hint.strip() or "colleague"
        aliases = _merge_aliases([t_clean, pref, fname], add_aliases)
    with _connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO people_roster VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (pkey, oid, fname, pref, json.dumps(list(aliases), ensure_ascii=False), cid, rh, now))
    return PersonRecord(pkey, oid, fname, pref, aliases, cid, rh, now)


_LEARN_PATTERNS = (
    re.compile(r"(?P<a>[A-Za-z0-9_\u4e00-\u9fa5 ]{1,20}?)\s*(?:就是|其实就是)\s*(?P<b>[A-Za-z0-9_\u4e00-\u9fa5 ]{1,20}?)[，,。；;\s]+(?:以后)?(?:我们)?(?:都)?(?:叫他|叫她|称呼他|叫)\s*(?P<pref>[A-Za-z0-9_\u4e00-\u9fa5]{1,16})"),
    re.compile(r"把\s*(?P<target>[A-Za-z0-9_\u4e00-\u9fa5 ]{1,24}?)\s*的(?:花名|昵称|习惯称呼|称呼|名字)(?:记成|改成|设为|叫作|叫)\s*(?P<pref>[A-Za-z0-9_\u4e00-\u9fa5]{1,16})"),
    re.compile(r"(?:以后)?把\s*(?P<target>[A-Za-z0-9_\u4e00-\u9fa5 ]{1,24}?)\s*(?:叫作|叫做|改叫|称呼为|称为|叫|记成)\s*(?P<pref>[A-Za-z0-9_\u4e00-\u9fa5]{1,16})"),
    re.compile(r"我们(?:平时|一般|都)?叫[他她]\s*(?P<pref>[\u4e00-\u9fa5A-Za-z0-9_]{1,16}).*?(?:平时|飞书|一般|通常)?(?:都是|都|是)?叫\s*(?P<target>[A-Za-z0-9_\u4e00-\u9fa5 ]{1,20})"),
)


def extract_and_apply_roster_learning(utterance: str, db_path=None) -> PersonRecord | None:
    text = (utterance or "").strip()
    if not text: return None
    strip_chars = " ，,。！？!?:：；;\"'“”‘’「」『』…"
    for pat in _LEARN_PATTERNS:
        m = pat.search(text)
        if not m: continue
        gd = m.groupdict()
        pref = gd["pref"].strip(strip_chars)
        if "a" in gd and "b" in gd:
            a, b = gd["a"].strip(strip_chars), gd["b"].strip(strip_chars)
            return upsert_person_alias(db_path, target=(b if a == pref else a), preferred_name=pref, add_aliases=[a, b, pref])
        target = gd["target"].strip(strip_chars)
        if target and pref:
            return upsert_person_alias(db_path, target=target, preferred_name=pref, add_aliases=[target, pref])
    return None


def expand_contact_query_terms(text: str, db_path=None) -> tuple[set[str], set[str], set[str]]:
    raw, tl = text or "", (text or "").lower()
    aliases_out, oids_out, cids_out = set(), set(), set()
    for rec in list_people_roster(db_path):
        candidates = {rec.preferred_name, rec.feishu_name, *rec.aliases}
        hit = False
        for term in candidates:
            t = term.strip().lower()
            if not t or t in {"owner", "主人"}: continue
            if t == "tovi":
                hit = bool(_TOVI_SAFE_RE.search(raw) or re.search(r"(?<![a-z0-9_])tovi(?![a-z0-9_])", tl))
            elif t.isascii():
                hit = bool(re.search(rf"(?<![a-z0-9_]){re.escape(t)}(?![a-z0-9_])", tl))
            else:
                hit = t in tl
            if hit: break
        if hit:
            aliases_out.update(x.lower() for x in candidates if x.strip())
            if rec.open_id: oids_out.add(rec.open_id)
            if rec.p2p_chat_id: cids_out.add(rec.p2p_chat_id)
    return aliases_out, oids_out, cids_out


def canonicalize_names_for_owner(text: str, db_path=None, *, audience_speaker: str = "owner(Matthew)") -> str:
    if not text: return text
    out, records = text, list_people_roster(db_path)
    haoyu = next((r for r in records if r.person_key == "haoyu"), None)
    if haoyu: out = _TOVI_SAFE_RE.sub(haoyu.preferred_name, out)
    for rec in records:
        if rec.person_key == "matthew_hong" or not rec.preferred_name: continue
        seed = BUILTIN_ROSTER_SEEDS.get(rec.person_key)
        if not re.search(r"[\u4e00-\u9fa5]", rec.preferred_name) and seed is not None and rec.preferred_name == seed.preferred_name: continue
        blocked = {"tovi", rec.preferred_name.lower(), *_other_seed_terms(rec.person_key)}
        rep_terms = [rec.feishu_name, *[a for a in rec.aliases if a.isascii() and not a.startswith("tp_") and len(a) >= 3]]
        for term in sorted({t.strip() for t in rep_terms if t and t.strip() and t.strip().lower() not in blocked}, key=len, reverse=True):
            out = re.sub(rf"(?<![A-Za-z0-9_/-]){re.escape(term)}(?![A-Za-z0-9_])", rec.preferred_name, out, flags=re.IGNORECASE)
    return out


def resolve_speaker_profile(speaker: str, db_path=None, *, persona: PersonaContract | None = None) -> SpeakerProfile:
    persona = persona or PersonaContract.from_config()
    raw = (speaker or "").strip()
    rec = lookup_person(raw, db_path)
    role_desc = persona.role if persona.role.startswith(persona.owner) else f"{persona.owner} 的 {persona.role}"
    if rec and (rec.person_key == "matthew_hong" or rec.role_hint.startswith("owner")):
        return SpeakerProfile(raw, rec.person_key, rec.feishu_name, rec.preferred_name, rec.role_hint, True, False, rec.preferred_name, f"我是 {persona.name}，{role_desc}")
    if rec:
        pref = rec.preferred_name
        return SpeakerProfile(raw, rec.person_key, rec.feishu_name or pref, pref, rec.role_hint, False, rec.role_hint.startswith("vip"), pref, f"{pref}你好，我是 {persona.name}，{role_desc}")
    clean = re.sub(r"\s*\(.*?\)\s*", "", raw).strip() or "同事"
    return SpeakerProfile(raw, "", clean, clean, "colleague", False, False, clean, f"{clean}你好，我是 {persona.name}，{role_desc}")


def add_roster_arguments(subparsers):
    p = subparsers.add_parser("roster", help="人物通讯录与助理身份契约")
    p.add_argument("--db", default=""); p.add_argument("--json", action="store_true", dest="as_json")
    rsub = p.add_subparsers(dest="roster_action")
    for name in ("list", "lookup", "set", "persona"):
        sp = rsub.add_parser(name); sp.add_argument("--db", default=""); sp.add_argument("--json", action="store_true", dest="as_json")
        if name == "lookup": sp.add_argument("query", nargs="?", default="")
        elif name == "set":
            sp.add_argument("--person", required=True); sp.add_argument("--prefer", default="")
            sp.add_argument("--add-alias", default="", dest="add_alias"); sp.add_argument("--role", default="")
        elif name == "persona": sp.add_argument("--config", default="")
    return p


def run_roster_cli(args, cfg=None) -> int:
    db_path = getattr(args, "db", "") or (getattr(cfg, "db_path", None) if cfg else None)
    as_json = bool(getattr(args, "as_json", False) or getattr(args, "json", False))
    action = getattr(args, "roster_action", None) or "list"
    if action == "persona":
        per = PersonaContract.from_config(getattr(args, "config", "") or None)
        print(json.dumps(per.to_dict(), ensure_ascii=False) if as_json else f"{per.name} ({per.owner}): {per.role}")
        return 0
    if action == "lookup":
        rec = lookup_person(getattr(args, "query", ""), db_path)
        print((json.dumps(rec.to_dict(), ensure_ascii=False) if as_json else f"{rec.preferred_name} ({rec.feishu_name}) [{rec.open_id}]") if rec else (json.dumps(None) if as_json else "未找到联系人"))
        return 0 if rec else 1
    if action == "set":
        aliases = [x.strip() for x in (getattr(args, "add_alias", "") or "").split(",") if x.strip()]
        rec = upsert_person_alias(db_path, target=getattr(args, "person", ""), preferred_name=getattr(args, "prefer", ""), add_aliases=aliases, role_hint=getattr(args, "role", ""))
        print(json.dumps(rec.to_dict(), ensure_ascii=False) if as_json else f"{rec.person_key}: {rec.preferred_name}")
        return 0
    rows = ensure_people_roster_seeded(db_path)
    print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False) if as_json else "\n".join(f"{r.person_key}\t{r.preferred_name}\t{r.feishu_name}" for r in rows))
    return 0
