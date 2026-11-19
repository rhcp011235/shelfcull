"""
shelfcull - click through your Radarr / Sonarr library, see who watched what in Plex, and mark titles Delete or Keep.

One process, standard library only:
  * a small web page (static/) + JSON API,
  * a background worker that builds the list, marks "Seen it" titles played in Plex and removes what was marked Delete.

Everything is configured with environment variables (see .env.example).
"""
import base64, csv, datetime, hmac, io, json, os, re, ssl, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
KEY_RE = re.compile(r"^[rs]\d{1,3}:\d{1,10}$")


# --------------------------------------------------------------------------------------------------------- config
class ConfigError(Exception):
    pass


def load_env_file(path, env):
    """Read KEY=VALUE lines into env without overriding what is already set."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env.setdefault(k, v)


def _bool(env, name, default):
    v = env.get(name, "").strip().lower()
    if not v:
        return default
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _int(env, name, default, low, high):
    v = env.get(name, "").strip()
    if not v:
        return default
    if not v.isdigit() or not low <= int(v) <= high:
        raise ConfigError(f"{name} must be a number from {low} to {high}")
    return int(v)


def _instances(env, prefix, secret, letter, label):
    """RADARR_URL / RADARR_1_URL / RADARR_2_URL ... -> [{id, name, url, key}]"""
    found = []
    if env.get(f"{prefix}_URL", "").strip():
        found.append((0, f"{prefix}_"))
    for k in env:
        m = re.fullmatch(rf"{prefix}_(\d{{1,3}})_URL", k)
        if m and env[k].strip():
            found.append((int(m.group(1)), f"{prefix}_{m.group(1)}_"))
    found.sort()
    if len({n for n, _p in found}) != len(found):
        raise ConfigError(f"{prefix}: the same number is used twice")
    out = []
    for n, p in found:
        url = env[p + "URL"].strip().rstrip("/")
        if not re.match(r"^https?://[^/\s]+", url):
            raise ConfigError(f"{p}URL must start with http:// or https://")
        key = env.get(p + secret, "").strip()
        if not key:
            raise ConfigError(f"{p}{secret} is missing")
        name = env.get(p + "NAME", "").strip() or (label if len(found) == 1 else f"{label} {n}")
        out.append({"id": f"{letter}{n}", "name": name[:40], "url": url, "key": key, "bin": env.get(p + "RECYCLE_BIN", "").strip()})
    return out


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.radarrs = _instances(env, "RADARR", "API_KEY", "r", "Radarr")
        self.sonarrs = _instances(env, "SONARR", "API_KEY", "s", "Sonarr")
        self.plexes = _instances(env, "PLEX", "TOKEN", "p", "Plex")
        if not self.radarrs and not self.sonarrs:
            raise ConfigError("nothing to show: set RADARR_URL + RADARR_API_KEY and/or SONARR_URL + SONARR_API_KEY")
        self.listen = env.get("LISTEN", "").strip() or "0.0.0.0"
        self.port = _int(env, "PORT", 8765, 1, 65535)
        self.data_dir = env.get("DATA_DIR", "").strip() or "./data"
        self.auth_user = env.get("AUTH_USER", "").strip() or "admin"
        self.auth_password = env.get("AUTH_PASSWORD", "")
        self.dry_run = _bool(env, "DRY_RUN", False)
        self.delete_files = _bool(env, "DELETE_FILES", True)
        self.exclude = _bool(env, "ADD_EXCLUSION", True)
        self.everywhere = _bool(env, "ALL_INSTANCES", True)
        self.plex_scan = _bool(env, "PLEX_SCAN_AFTER_DELETE", True)
        self.plex_empty_trash = _bool(env, "PLEX_EMPTY_TRASH", False)
        self.refresh_minutes = _int(env, "REFRESH_MINUTES", 60, 1, 10080)
        self.default_view = env.get("DEFAULT_VIEW", "").strip().lower() or "all"
        if self.default_view not in ("all", "movies", "shows"):
            raise ConfigError("DEFAULT_VIEW must be all, movies or shows")
        self.timeout = _int(env, "HTTP_TIMEOUT", 120, 5, 3600)
        self.tls = None if _bool(env, "VERIFY_TLS", True) else ssl._create_unverified_context()


# ------------------------------------------------------------------------------------------------------ http client
class ApiError(Exception):
    pass


def call(url, method="GET", headers=None, body=None, timeout=120, tls=None):
    """JSON request. Errors never carry the query string or a header, so no key or token ends up in a log."""
    where = urllib.parse.urlsplit(url)
    where = f"{where.netloc}{where.path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        hint = " (check the API key / token)" if e.code in (401, 403) else ""
        raise ApiError(f"HTTP {e.code} from {where}{hint}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(f"cannot reach {where}: {getattr(e, 'reason', e)}") from None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError(f"{where} did not answer with JSON (is the URL right?)") from None


class Arr:
    """One Radarr (kind 'movie') or Sonarr (kind 'show') instance."""

    def __init__(self, inst, kind, cfg):
        self.id, self.name, self.kind, self.bin = inst["id"], inst["name"], kind, inst.get("bin") or ""
        self.base, self.key, self.cfg = inst["url"] + "/api/v3/", inst["key"], cfg
        self.resource = "movie" if kind == "movie" else "series"
        self.ext = "tmdbId" if kind == "movie" else "tvdbId"

    def req(self, path, method="GET", params=None, body=None):
        q = "?" + urllib.parse.urlencode(params) if params else ""
        return call(self.base + path + q, method, {"X-Api-Key": self.key, "Content-Type": "application/json", "Accept": "application/json"},
                    body, self.cfg.timeout, self.cfg.tls)

    def list(self):
        return self.req(self.resource) or []

    def queue_map(self):
        """item id -> download queue ids."""
        out, page = {}, 1
        while True:
            recs = (self.req("queue", params={"page": page, "pageSize": 1000}) or {}).get("records", [])
            for r in recs:
                out.setdefault(r.get(self.resource + "Id"), []).append(r["id"])
            if len(recs) < 1000:
                return out
            page += 1

    def remove_queue(self, qid):
        self.req(f"queue/{qid}", "DELETE", {"removeFromClient": "true", "blocklist": "false"})

    def excluded(self):
        path = "exclusions" if self.kind == "movie" else "importlistexclusion"
        return {e.get(self.ext) for e in self.req(path) or []}

    def exclude(self, ext_id, title, year):
        if self.kind == "movie":
            self.req("exclusions", "POST", body={"tmdbId": ext_id, "movieTitle": title, "movieYear": year})
        else:
            self.req("importlistexclusion", "POST", body={"tvdbId": ext_id, "title": title})

    def delete(self, item_id):
        flag = "addImportExclusion" if self.kind == "movie" else "addImportListExclusion"
        self.req(f"{self.resource}/{item_id}", "DELETE",
                 {"deleteFiles": str(self.cfg.delete_files).lower(), flag: str(self.cfg.exclude).lower()})


def plex_ids(m):
    """The external ids Plex knows for an item, as 'tmdb:123' / 'imdb:tt123' / 'tvdb:123' (new and legacy agents)."""
    out = set()
    for g in m.get("Guid") or []:
        scheme, _, val = (g.get("id") or "").partition("://")
        if scheme in ("imdb", "tmdb", "tvdb") and val:
            out.add(f"{scheme}:{val}")
    legacy = re.match(r"com\.plexapp\.agents\.(imdb|themoviedb|thetvdb)://([^?/]+)", m.get("guid") or "")
    if legacy:
        out.add({"imdb": "imdb", "themoviedb": "tmdb", "thetvdb": "tvdb"}[legacy.group(1)] + ":" + legacy.group(2))
    return out


class Plex:
    OWNER = 1          # the server owner's account id on every Plex Media Server
    PAGE = 2000

    def __init__(self, inst, cfg):
        self.id, self.name, self.base, self.token, self.cfg = inst["id"], inst["name"], inst["url"], inst["key"], cfg

    def get(self, path, method="GET"):
        r = call(self.base + path, method, {"X-Plex-Token": self.token, "Accept": "application/json"}, None, self.cfg.timeout, self.cfg.tls)
        return (r or {}).get("MediaContainer") or {}

    def sections(self):
        return [d for d in self.get("/library/sections").get("Directory") or [] if d.get("type") in ("movie", "show")]

    def _paged(self, path):
        start, sep = 0, "&" if "?" in path else "?"
        while True:
            rows = self.get(f"{path}{sep}X-Plex-Container-Start={start}&X-Plex-Container-Size={self.PAGE}").get("Metadata") or []
            yield from rows
            if len(rows) < self.PAGE:
                return
            start += len(rows)

    def entries(self):
        """Every movie / show with the owner's watch state, plus the names of other accounts that played it."""
        out, by_key = [], {}
        for s in self.sections():
            show = s["type"] == "show"
            for m in self._paged(f"/library/sections/{s['key']}/all?type={2 if show else 1}&includeGuids=1"):
                e = {"srv": self.id, "kind": "show" if show else "movie", "rk": str(m.get("ratingKey")), "ids": plex_ids(m),
                     "n": (m.get("viewedLeafCount") if show else m.get("viewCount")) or 0,
                     "total": (m.get("leafCount") or 0) if show else 1, "last": m.get("lastViewedAt") or 0, "others": []}
                out.append(e); by_key[e["rk"]] = e
        try:                                   # the play history is a bonus (needs an admin token)
            names = {a.get("id"): a.get("name") for a in self.get("/accounts").get("Account") or []}
            for h in self._paged("/status/sessions/history/all"):
                acc = h.get("accountID")
                if acc in (None, self.OWNER):
                    continue
                rk = str(h.get("grandparentRatingKey") or (h.get("grandparentKey") or "").rsplit("/", 1)[-1] or h.get("ratingKey"))
                e = by_key.get(rk)
                who = names.get(acc) or "another user"
                if e is not None and who not in e["others"]:
                    e["others"].append(who)
        except ApiError:
            pass
        return out

    def mark_played(self, rating_key):
        q = urllib.parse.urlencode({"identifier": "com.plexapp.plugins.library", "key": rating_key})
        self.get(f"/:/scrobble?{q}")

    def scan(self, types):
        keys = [s["key"] for s in self.sections() if s["type"] in types]
        for k in keys:
            self.get(f"/library/sections/{k}/refresh")
        return keys

    def scanning(self, keys):
        return any(d.get("refreshing") for d in self.get("/library/sections").get("Directory") or [] if d.get("key") in keys)

    def empty_trash(self, keys):
        for k in keys:
            self.get(f"/library/sections/{k}/emptyTrash", "PUT")


# ------------------------------------------------------------------------------------------------------------ store
class Store:
    """decisions.json, items.json, status.json and run.flag in the data folder."""

    def __init__(self, data_dir):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, name):
        return os.path.join(self.dir, name)

    def read(self, name, default):
        try:
            with open(self.path(name), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, type(default)) else default
        except (OSError, ValueError):
            return default

    def write(self, name, obj):
        tmp = self.path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        os.replace(tmp, self.path(name))

    def decisions(self):
        with self.lock:
            return self.read("decisions.json", {})

    def update(self, fn):
        with self.lock:
            d = self.decisions(); fn(d); self.write("decisions.json", d)
            return d

    def pending(self, what):
        return sorted(k for k, v in self.decisions().items() if v.get("d") == what and not v.get("done"))

    def queued(self):
        return os.path.exists(self.path("run.flag"))

    def queue(self, on):
        with self.lock:
            if on:
                open(self.path("run.flag"), "w").close()
            elif self.queued():
                os.remove(self.path("run.flag"))


# ------------------------------------------------------------------------------------------------------------- rows
def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def local_date(iso):
    try:
        return datetime.datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return (iso or "")[:10]


def poster_of(m):
    url = next((i.get("remoteUrl") for i in m.get("images") or [] if i.get("coverType") == "poster" and i.get("remoteUrl")), "")
    return re.sub(r"/t/p/[^/]+/", "/t/p/w342/", url)


def ids_of(kind, m):
    out = []
    for scheme, field in (("tmdb", "tmdbId"), ("tvdb", "tvdbId"), ("imdb", "imdbId")):
        v = m.get(field)
        if v and (scheme != "tvdb" or kind == "show"):
            out.append(f"{scheme}:{v}")
    return out


def size_of(kind, m):
    return ((m.get("movieFile") or {}).get("size") if kind == "movie" else (m.get("statistics") or {}).get("sizeOnDisk")) or 0


def has_files(kind, m):
    if kind == "movie":
        return bool(m.get("hasFile") and m.get("movieFile"))
    return ((m.get("statistics") or {}).get("episodeFileCount") or 0) > 0


def watch_of(kind, ids, index):
    seen, hits = set(), []
    for i in ids:
        for e in index.get(i, ()):
            if e["kind"] == kind and (e["srv"], e["rk"]) not in seen:
                seen.add((e["srv"], e["rk"])); hits.append(e)
    w = {"n": 0, "eps": 0, "last": 0, "oth": [], "full": False, "inplex": bool(hits)}
    for e in hits:
        if kind == "movie":
            w["n"] += e["n"]
        elif e["n"] > w["n"] or not w["eps"]:
            w["n"], w["eps"] = max(w["n"], e["n"]), e["total"]
        w["last"] = max(w["last"], e["last"])
        w["full"] = w["full"] or (e["n"] >= e["total"] > 0)
        w["oth"] += [o for o in e["others"] if o not in w["oth"]]
    return w


def row_of(arr, m, index, also):
    kind, r = arr.kind, m.get("ratings") or {}
    w = watch_of(kind, ids_of(kind, m), index)
    row = {"k": f"{arr.id}:{m[arr.ext]}", "src": arr.id, "srcn": arr.name, "kind": kind, "imdb": m.get("imdbId") or "", "t": m.get("title") or "?",
           "y": m.get("year") or 0, "rt": m.get("runtime") or 0, "g": m.get("genres") or [], "ov": m.get("overview") or "",
           "cert": m.get("certification") or "", "lang": (m.get("originalLanguage") or {}).get("name") or "", "sz": size_of(kind, m),
           "p": poster_of(m), "also": also, **w}
    if kind == "movie":
        f = m["movieFile"]
        row.update(tmdb=m["tmdbId"], ot=m.get("originalTitle") if (m.get("originalTitle") or row["t"]) != row["t"] else "",
                   ri=(r.get("imdb") or {}).get("value") or 0, rv=(r.get("imdb") or {}).get("votes") or 0, rx=(r.get("tmdb") or {}).get("value") or 0,
                   st=m.get("studio") or "", q=((f.get("quality") or {}).get("quality") or {}).get("name") or "", ad=local_date(f.get("dateAdded")),
                   yt=m.get("youTubeTrailerId") or "")
    else:
        s = m.get("statistics") or {}
        seasons, eps = s.get("seasonCount") or 0, s.get("episodeFileCount") or 0
        per = [[x.get("seasonNumber"), (x.get("statistics") or {}).get("episodeFileCount") or 0, (x.get("statistics") or {}).get("sizeOnDisk") or 0]
               for x in sorted(m.get("seasons") or [], key=lambda x: x.get("seasonNumber") or 0)]
        row.update(seas=[x for x in per if x[1] > 0], nseas=seasons, neps=eps)
        row.update(tvdb=m["tvdbId"], ot="", ri=0, rv=r.get("votes") or 0, rx=r.get("value") or 0, st=m.get("network") or "",
                   q=f"{seasons} season{'' if seasons == 1 else 's'}, {eps} episode{'' if eps == 1 else 's'}"
                     + (", ended" if m.get("status") == "ended" else ""), ad=local_date(m.get("added")), yt="")
    return row


def stamp_bin(bin_dir, item_path):
    """Radarr / Sonarr empty their recycle bin by each file's own date. A whole title moved there keeps its old dates,
    so it would be purged at the next clean-up instead of after the keep period. Give the files today's date so the
    keep period really counts from the removal. Returns the number of files stamped."""
    name = os.path.basename(os.path.normpath((item_path or "").replace("\\", "/")))
    folder = os.path.join(bin_dir, name) if bin_dir and name not in ("", ".", "..") else ""     # never the bin itself
    n = 0
    if folder and os.path.isdir(folder):
        for root, _dirs, files in os.walk(folder):
            for f in files:
                try:
                    os.utime(os.path.join(root, f), None); n += 1
                except OSError:
                    pass
    return n


def imdb_ids(text, rated_only):
    """IMDb ids from an IMDb export (ratings, watchlist or any list): the 'Const' column."""
    rows = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not rows.fieldnames or "Const" not in rows.fieldnames:
        raise ValueError("this is not an IMDb export (no 'Const' column)")
    out = set()
    for r in rows:
        c = (r.get("Const") or "").strip()
        if re.fullmatch(r"tt\d{5,10}", c) and (not rated_only or (r.get("Your Rating") or "").strip()):
            out.add(c)
    return out


# -------------------------------------------------------------------------------------------------------------- app
class App:
    def __init__(self, cfg, out=None):
        self.cfg = cfg
        self.store = Store(cfg.data_dir)
        self.arrs = [Arr(i, "movie", cfg) for i in cfg.radarrs] + [Arr(i, "show", cfg) for i in cfg.sonarrs]
        self.plexes = [Plex(i, cfg) for i in cfg.plexes]
        self.out = out or self._print
        saved = self.store.read("items.json", {})
        self.items, self.built = saved.get("items") or [], saved.get("built") or 0
        if saved.get("v") != __version__:
            self.built = 0                      # a list cached by another version may lack fields: rebuild it right away
        self.error, self.next_build, self.want_refresh = "", 0, False
        self.wake, self.stop = threading.Event(), threading.Event()
        self.scan_poll, self.scan_start_wait, self.scan_max_wait = 5, 60, 900

    @staticmethod
    def _print(msg):
        print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)

    def log(self, msg):
        self.out(msg)
        try:
            with open(self.store.path("shelfcull.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        except OSError:
            pass

    def instances(self):
        return [{"id": a.id, "name": a.name, "kind": a.kind} for a in self.arrs]

    # ---- the list
    def plex_index(self):
        index = {}
        for p in self.plexes:
            for e in p.entries():
                for i in e["ids"]:
                    index.setdefault(i, []).append(e)
        return index

    def build(self):
        index = self.plex_index()
        lists = {a.id: a.list() for a in self.arrs}
        items = []
        for a in self.arrs:
            peers = [b for b in self.arrs if b.kind == a.kind and b.id != a.id]
            held = {b.name: {m.get(b.ext) for m in lists[b.id]} for b in peers}
            for m in lists[a.id]:
                if m.get(a.ext) and has_files(a.kind, m):
                    items.append(row_of(a, m, index, [n for n, ids in held.items() if m[a.ext] in ids]))
        self.items, self.built, self.error = items, int(time.time()), ""
        self.store.write("items.json", {"built": self.built, "v": __version__, "items": items})
        return len(items)

    # ---- Seen it
    def mark_played(self, keys):
        """Mark these list keys played in Plex (every copy on every Plex server). Returns ({key: label} done, {key: reason} failed)."""
        rows, index, by_srv = {r["k"]: r for r in self.items}, self.plex_index(), {p.id: p for p in self.plexes}
        ok, failed = {}, {}
        for k in keys:
            row = rows.get(k)
            label = f"{row['t']} ({row['y']})" if row else k
            try:
                if row is None:
                    raise ApiError("it is no longer in the list")
                hits = {}
                for i in [f"{s}:{row[f]}" for s, f in (("tmdb", "tmdb"), ("tvdb", "tvdb"), ("imdb", "imdb")) if row.get(f)]:
                    for e in index.get(i, ()):
                        if e["kind"] == row["kind"]:
                            hits[(e["srv"], e["rk"])] = e
                if not hits:
                    raise ApiError("Plex has no item with that id")
                for (srv, rk) in hits:
                    by_srv[srv].mark_played(rk)
                ok[k] = label; self.log(f"marked played in Plex: {label}")
            except ApiError as e:
                failed[k] = str(e)[:200]; self.log(f"FAILED to mark played {label}: {e}")
        return ok, failed

    def process_seen(self):
        todo = self.store.pending("seen")
        if not todo:
            return 0
        ok, failed = self.mark_played(todo)
        if failed:                # not retried: the page shows the reason and the title counts as undecided again
            self.store.update(lambda cur: [cur[k].update(d="failed", error=e) for k, e in failed.items() if k in cur])
        if ok:
            self.store.update(lambda cur: [cur.pop(k, None) for k in ok if cur.get(k, {}).get("d") == "seen"])
        return len(ok)

    def process_import(self):
        """Titles queued by an IMDb import: mark them played. Marks (Delete / Keep) are left as they are."""
        with self.store.lock:
            todo = self.store.read("seen-queue.json", [])
            if todo:
                self.store.write("seen-queue.json", [])
        if not todo:
            return 0
        ok, failed = self.mark_played(todo)
        self.log(f"IMDb import: {len(ok)} marked played in Plex, {len(failed)} failed")
        return len(ok)

    def api_imdb(self, body):
        if not self.plexes:
            return 400, "no Plex server is configured", None
        try:
            ids = imdb_ids(str(body.get("csv") or ""), bool(body.get("ratedOnly")))
        except (ValueError, csv.Error) as e:
            return 400, str(e), None
        hit = [r for r in self.items if r.get("imdb") in ids]
        todo = [r["k"] for r in hit if r["inplex"] and not r["full"]]
        if todo:
            with self.store.lock:
                self.store.write("seen-queue.json", sorted(set(self.store.read("seen-queue.json", [])) | set(todo)))
            self.wake.set()
        return 200, None, {"inFile": len(ids), "inLibrary": len(hit), "alreadyPlayed": sum(1 for r in hit if r["full"]),
                           "notInPlex": sum(1 for r in hit if not r["inplex"]), "queued": len(todo)}

    # ---- Delete
    def process_delete(self, progress=None):
        cfg, d = self.cfg, self.store.decisions()
        todo = sorted(k for k, v in d.items() if v.get("d") == "delete" and not v.get("done"))
        res = {"removed": 0, "failed": [], "freed": 0, "dry": cfg.dry_run}
        if not todo:
            return res
        by_id = {a.id: a for a in self.arrs}
        lists = {a.id: {m.get(a.ext): m for m in a.list()} for a in self.arrs}
        queues, blocked, unsaved, done, kinds = {}, {}, {}, 0, set()

        def save():
            nonlocal done
            if unsaved:
                self.store.update(lambda cur: [cur[k].update(v) for k, v in unsaved.items() if k in cur])
                done += len(unsaved); unsaved.clear()
            if progress:
                progress(done, len(todo), res["freed"])

        for k in todo:
            src, ext = k.split(":"); ext = int(ext); v = d[k]
            label = f"{v.get('title')} ({v.get('year')})"
            a = by_id.get(src)
            if a is None:
                res["failed"].append(f"{label}: its Radarr/Sonarr ({src}) is no longer configured"); continue
            group = [b for b in self.arrs if b.kind == a.kind and (b.id == a.id or cfg.everywhere)]
            holders = [b for b in group if ext in lists[b.id]]
            if not holders:
                self.log(f"already gone: {label}")
                if not cfg.dry_run:
                    unsaved[k] = {"done": int(time.time()), "note": "already gone"}
                continue
            ref = lists[src].get(ext) or lists[holders[0].id][ext]
            if norm(ref.get("title")) != norm(v.get("title")) or (ref.get("year") or 0) != (v.get("year") or 0):
                self.log(f"SKIPPED {label}: {a.name} now calls that id '{ref.get('title')} ({ref.get('year')})'")
                res["failed"].append(f"{label}: does not match {a.name} any more"); continue
            size = sum(size_of(b.kind, lists[b.id][ext]) for b in holders)
            where = " + ".join(b.name for b in holders)
            if cfg.dry_run:
                self.log(f"DRY RUN, would remove: {label} from {where}, {size / 2**30:.1f} GB")
                res["removed"] += 1; res["freed"] += size; continue
            try:
                for b in holders:
                    if b.id not in queues:
                        queues[b.id] = b.queue_map()
                    for qid in queues[b.id].get(lists[b.id][ext]["id"], []):
                        b.remove_queue(qid)
                    gone = lists[b.id].pop(ext)
                    b.delete(gone["id"])
                    if b.bin and cfg.delete_files:
                        stamp_bin(b.bin, gone.get("path"))
                if cfg.exclude and cfg.everywhere:
                    for b in group:
                        if b not in holders:
                            if b.id not in blocked:
                                blocked[b.id] = b.excluded()
                            if ext not in blocked[b.id]:
                                b.exclude(ext, ref.get("title") or "", ref.get("year") or 0); blocked[b.id].add(ext)
                res["removed"] += 1; res["freed"] += size; kinds.add(a.kind)
                unsaved[k] = {"done": int(time.time()), "freed": size}
                self.log(f"removed: {label} from {where}, {size / 2**30:.1f} GB")
            except ApiError as e:
                self.log(f"FAILED {label}: {e}"); res["failed"].append(f"{label}: {e}")
            if len(unsaved) >= 25:
                save()
        save()
        if kinds and cfg.plex_scan:
            self.plex_aftercare(kinds)
        return res

    def plex_aftercare(self, kinds):
        """Let Plex notice the removals; optionally empty its trash once the scan has finished."""
        for p in self.plexes:
            try:
                keys = p.scan(kinds)
                if not (self.cfg.plex_empty_trash and keys):
                    continue
                waited = 0
                while waited < self.scan_start_wait and not p.scanning(keys):
                    time.sleep(self.scan_poll); waited += self.scan_poll
                waited = 0
                while waited < self.scan_max_wait and p.scanning(keys):
                    time.sleep(self.scan_poll); waited += self.scan_poll
                p.empty_trash(keys)
            except ApiError as e:
                self.log(f"Plex {p.name}: {e}")

    # ---- worker
    def tick(self):
        acted = False
        if self.plexes and self.store.pending("seen"):
            acted = self.process_seen() > 0
        if self.plexes and self.store.read("seen-queue.json", []):
            acted = self.process_import() > 0 or acted
        if self.store.queued():
            started = int(time.time())
            self.store.write("status.json", {"state": "running", "at": started})
            try:
                res = self.process_delete(lambda n, total, freed: self.store.write(
                    "status.json", {"state": "running", "at": started, "n": n, "total": total, "freed": freed}))
            except Exception as e:                                    # noqa: BLE001 - the run must always end with a status
                res = {"removed": 0, "failed": [str(e)], "freed": 0, "dry": self.cfg.dry_run}; self.log(f"FAILED run: {e}")
            acted = True
            try:
                self.build()
            except Exception as e:                                    # noqa: BLE001
                self.error = str(e); res["failed"].append(f"list not refreshed: {e}")
            self.store.write("status.json", {"state": "done", "at": int(time.time()), "rc": 1 if res["failed"] else 0, **res})
            self.store.queue(False)
            return
        now = time.time()
        stale = now - self.built > self.cfg.refresh_minutes * 60
        if acted or self.want_refresh or (stale and now >= self.next_build):
            self.want_refresh = False
            try:
                n = self.build()
                self.out(f"list built: {n} titles")
            except Exception as e:                                    # noqa: BLE001
                self.error, self.next_build = str(e), now + 60
                self.log(f"could not build the list: {e}")

    def run_worker(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:                                    # noqa: BLE001
                self.log(f"worker error: {e}")
            self.wake.wait(5); self.wake.clear()

    # ---- what the page asks for
    def state(self):
        d = self.store.decisions()
        return {"ok": True, "decisions": d, "pending": sum(1 for v in d.values() if v.get("d") == "delete" and not v.get("done")),
                "queued": self.store.queued(), "status": self.store.read("status.json", {}) or None, "built": self.built,
                "error": self.error, "dryRun": self.cfg.dry_run, "plex": bool(self.plexes),
                "importing": len(self.store.read("seen-queue.json", []))}

    def api_set(self, body):
        k, dec = body.get("k"), body.get("d")
        if not isinstance(k, str) or not KEY_RE.match(k):
            return 400, "bad key"
        if dec not in ("delete", "keep", "seen", "none"):
            return 400, "bad decision"
        if dec == "seen" and not self.plexes:
            return 400, "no Plex server is configured"
        err = []

        def change(d):
            if d.get(k, {}).get("done"):
                err.append("that title was already removed"); return
            if dec == "none":
                d.pop(k, None)
            else:
                try:
                    year = int(body.get("year") or 0)
                except (TypeError, ValueError):
                    year = 0
                d[k] = {"d": dec, "at": int(time.time()), "title": str(body.get("title") or "")[:200], "year": year}
        self.store.update(change)
        if err:
            return 409, err[0]
        if dec == "seen":
            self.wake.set()
        return 200, None

    def api_run(self):
        if not self.store.pending("delete"):
            return 409, "nothing is marked Delete"
        self.store.queue(True); self.wake.set()
        return 200, None


# ------------------------------------------------------------------------------------------------------------- http
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml"}
CSP = "default-src 'self'; img-src 'self' data: https: http:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class Handler(BaseHTTPRequestHandler):
    server_version = "shelfcull"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):          # no access log: nothing about the user ends up in the container log
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        raw = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def fail(self, code, msg):
        self.send(code, {"ok": False, "error": msg})

    def authorised(self):
        cfg = self.server.app.cfg
        if not cfg.auth_password:
            return True
        try:
            scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
            user, _, pw = base64.b64decode(token).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            scheme, user, pw = "", "", ""
        ok = scheme.lower() == "basic" and hmac.compare_digest(user.encode(), cfg.auth_user.encode()) \
            and hmac.compare_digest(pw.encode(), cfg.auth_password.encode())
        if not ok:
            self.send(401, {"ok": False, "error": "sign in"}, extra={"WWW-Authenticate": 'Basic realm="shelfcull", charset="UTF-8"'})
        return ok

    def do_GET(self):
        app, path = self.server.app, urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self.send(200, {"ok": True})
        if not self.authorised():
            return None
        if path == "/api/state":
            return self.send(200, app.state())
        if path == "/api/items":
            return self.send(200, {"built": app.built, "items": app.items, "instances": app.instances(), "version": __version__,
                                   "defaultView": app.cfg.default_view})
        name = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/icon.svg": "icon.svg"}.get(path)
        if not name:
            return self.fail(404, "not found")
        with open(os.path.join(STATIC, name), "rb") as f:
            return self.send(200, f.read(), TYPES[os.path.splitext(name)[1]])

    do_HEAD = do_GET

    def do_POST(self):
        app, path = self.server.app, urllib.parse.urlsplit(self.path).path
        if not self.authorised():
            return None
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or ""):
            return self.fail(403, "cross-site request refused")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.fail(415, "send JSON")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= (8_000_000 if path == "/api/imdb" else 10000):
                return self.fail(413, "request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.fail(400, "JSON object expected")
        if path == "/api/set":
            code, err = app.api_set(body)
        elif path == "/api/run":
            code, err = app.api_run()
        elif path == "/api/cancel":
            app.store.queue(False); code, err = 200, None
        elif path == "/api/imdb":
            code, err, result = app.api_imdb(body)
            if not err:
                return self.send(200, dict(app.state(), imported=result))
        elif path == "/api/refresh":
            app.want_refresh = True; app.wake.set(); code, err = 200, None
        else:
            return self.fail(404, "not found")
        return self.fail(code, err) if err else self.send(200, app.state())


def make_server(app):
    srv = ThreadingHTTPServer((app.cfg.listen, app.cfg.port), Handler)
    srv.daemon_threads = True
    srv.app = app
    return srv


def main():
    load_env_file(os.environ.get("ENV_FILE") or ".env", os.environ)
    try:
        cfg = Config()
    except ConfigError as e:
        print(f"shelfcull: configuration error: {e}", file=sys.stderr)
        return 2
    app = App(cfg)
    names = ", ".join(a.name for a in app.arrs) + (" | Plex: " + ", ".join(p.name for p in app.plexes) if app.plexes else " | no Plex")
    app.out(f"shelfcull {__version__} on {cfg.listen}:{cfg.port} - {names}" + (" - DRY RUN, nothing will be deleted" if cfg.dry_run else ""))
    if not cfg.auth_password:
        app.out("no AUTH_PASSWORD set: anyone who can reach this port can delete from your library")
    threading.Thread(target=app.run_worker, daemon=True).start()
    srv = make_server(app)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    app.stop.set()
    return 0
