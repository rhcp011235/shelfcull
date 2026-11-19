"""
Small in-memory stand-ins for Radarr, Sonarr and Plex Media Server, speaking just enough of their HTTP APIs for
shelfcull. Used by the tests and by tests/demo.py. All titles are invented.
"""
import json, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code, obj=None):
        raw = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        fake = self.server.fake
        with fake.lock:
            fake.calls.append((self.command, u.path, q))
            code, obj = fake.handle(self.command, u.path, q, body, self.headers)
        self._send(code, obj)

    do_GET = do_POST = do_PUT = do_DELETE = _handle


class Fake:
    def __init__(self):
        self.lock, self.calls = threading.Lock(), []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.daemon_threads = True
        self.srv.fake = self
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class FakeArr(Fake):
    """kind 'movie' = Radarr, 'show' = Sonarr."""

    def __init__(self, kind, key="arr-key"):
        super().__init__()
        self.kind, self.key = kind, key
        self.res = "movie" if kind == "movie" else "series"
        self.ext = "tmdbId" if kind == "movie" else "tvdbId"
        self.excl_path = "exclusions" if kind == "movie" else "importlistexclusion"
        self.items, self.queue, self.exclusions, self.deleted, self.fail_delete = [], [], [], [], set()
        self._next = 1

    def add(self, ext_id, title, year, size=4 * 2**30, files=True, **extra):
        m = {"id": self._next, self.ext: ext_id, "title": title, "year": year, "overview": f"The story of {title}.", "genres": ["Drama"],
             "runtime": 100, "images": [], "added": "2024-01-02T03:04:05Z"}
        if self.kind == "movie":
            m.update(hasFile=files, imdbId=f"tt{ext_id:07d}", ratings={"imdb": {"value": 6.5, "votes": 1200}, "tmdb": {"value": 6.1}})
            if files:
                m["movieFile"] = {"size": size, "dateAdded": "2024-01-02T03:04:05Z", "quality": {"quality": {"name": "Bluray-1080p"}}}
        else:
            m.update(imdbId=f"tt{ext_id:07d}", network="Fake TV", status="ended", ratings={"value": 7.2, "votes": 300},
                     statistics={"seasonCount": 2, "episodeFileCount": 20 if files else 0, "sizeOnDisk": size if files else 0},
                     seasons=[{"seasonNumber": 0, "statistics": {"episodeFileCount": 0, "sizeOnDisk": 0}},
                              {"seasonNumber": 1, "statistics": {"episodeFileCount": 12 if files else 0, "sizeOnDisk": size * 3 // 5 if files else 0}},
                              {"seasonNumber": 2, "statistics": {"episodeFileCount": 8 if files else 0, "sizeOnDisk": size * 2 // 5 if files else 0}}])
        m.update(extra)
        self._next += 1
        self.items.append(m)
        return m

    def handle(self, method, path, q, body, headers):
        if headers.get("X-Api-Key") != self.key:
            return 401, {"message": "Unauthorized"}
        p = path.replace("/api/v3/", "", 1).strip("/").split("/")
        if p == [self.res] and method == "GET":
            return 200, self.items
        if p[0] == self.res and len(p) == 2 and method == "DELETE":
            m = next((x for x in self.items if x["id"] == int(p[1])), None)
            if m is None:
                return 404, {"message": "NotFound"}
            if m["id"] in self.fail_delete:
                return 500, {"message": "boom"}
            self.items.remove(m)
            flag = q.get("addImportExclusion" if self.kind == "movie" else "addImportListExclusion")
            self.deleted.append({"id": m["id"], "ext": m[self.ext], "deleteFiles": q.get("deleteFiles"), "exclude": flag})
            if flag == "true":
                self.exclusions.append({self.ext: m[self.ext]})
            return 200, None
        if p == ["queue"] and method == "GET":
            return 200, {"records": self.queue if q.get("page", "1") == "1" else []}
        if p[0] == "queue" and len(p) == 2 and method == "DELETE":
            self.queue = [r for r in self.queue if r["id"] != int(p[1])]
            return 200, None
        if p == [self.excl_path] and method == "GET":
            return 200, self.exclusions
        if p == [self.excl_path] and method == "POST":
            self.exclusions.append(body)
            return 201, body
        return 404, {"message": "NotFound"}


class FakePlex(Fake):
    def __init__(self, token="plex-token"):
        super().__init__()
        self.token = token
        self.sections = [{"key": "1", "type": "movie", "title": "Movies"}, {"key": "2", "type": "show", "title": "TV"},
                         {"key": "3", "type": "artist", "title": "Music"}]
        self.items = {"1": [], "2": []}
        self.accounts = [{"id": 0, "name": ""}, {"id": 1, "name": "owner"}, {"id": 7, "name": "guest"}]
        self.history, self.scanned, self.trash_emptied, self.refreshing = [], [], [], set()
        self._next = 100

    def add_movie(self, tmdb, title, plays=0, last=0, legacy=False, section="1"):
        m = {"ratingKey": str(self._next), "title": title, "viewCount": plays, "lastViewedAt": last}
        if legacy:
            m["guid"] = f"com.plexapp.agents.themoviedb://{tmdb}?lang=en"
        else:
            m["Guid"] = [{"id": f"tmdb://{tmdb}"}, {"id": f"imdb://tt{tmdb:07d}"}]
        if not plays:
            m.pop("viewCount"); m.pop("lastViewedAt")
        self._next += 1
        self.items[section].append(m)
        return m

    def add_show(self, tvdb, title, episodes=20, watched=0, last=0):
        m = {"ratingKey": str(self._next), "title": title, "leafCount": episodes, "viewedLeafCount": watched, "lastViewedAt": last,
             "Guid": [{"id": f"tvdb://{tvdb}"}]}
        self._next += 1
        self.items["2"].append(m)
        return m

    def play_by(self, account_id, item, episode_of_show=False):
        row = {"accountID": account_id}
        if episode_of_show:
            row.update(ratingKey="9999", grandparentKey=f"/library/metadata/{item['ratingKey']}")
        else:
            row["ratingKey"] = item["ratingKey"]
        self.history.append(row)

    def handle(self, method, path, q, body, headers):
        if headers.get("X-Plex-Token") != self.token:
            return 401, None
        mc = lambda **kw: (200, {"MediaContainer": kw})
        if path == "/library/sections":
            return mc(Directory=[dict(s, refreshing=s["key"] in self.refreshing) for s in self.sections])
        if path == "/accounts":
            return mc(Account=self.accounts)
        if path == "/status/sessions/history/all":
            return mc(Metadata=self._page(self.history, q))
        if path == "/:/scrobble":
            for sec in self.items.values():
                for m in sec:
                    if m["ratingKey"] == q.get("key"):
                        if "leafCount" in m:
                            m["viewedLeafCount"] = m["leafCount"]
                        else:
                            m["viewCount"] = m.get("viewCount", 0) + 1
                        return 200, None
            return 404, None
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["library", "sections"]:
            key, action = parts[2], parts[3]
            if action == "all" and key in self.items:
                return mc(Metadata=self._page(self.items[key], q))
            if action == "refresh":
                self.scanned.append(key); return 200, None
            if action == "emptyTrash" and method == "PUT":
                self.trash_emptied.append(key); return 200, None
        return 404, None

    @staticmethod
    def _page(rows, q):
        start, size = int(q.get("X-Plex-Container-Start", 0)), int(q.get("X-Plex-Container-Size", 10**9))
        return rows[start:start + size]
