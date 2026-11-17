"""Tests against in-memory fake Radarr / Sonarr / Plex servers (tests/fakes.py). Run: python -m unittest discover -s tests -v"""
import base64, json, os, shutil, sys, tempfile, threading, time, unittest, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakeArr, FakePlex                                    # noqa: E402
from shelfcull import app as sc                                        # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.r1, self.r2, self.s1, self.plex = FakeArr("movie"), FakeArr("movie", key="k2"), FakeArr("show"), FakePlex()
        self.fakes = [self.r1, self.r2, self.s1, self.plex]
        self.out = []

    def tearDown(self):
        for f in self.fakes:
            f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        e = {"RADARR_1_URL": self.r1.url, "RADARR_1_API_KEY": "arr-key", "RADARR_1_NAME": "HD",
             "RADARR_2_URL": self.r2.url + "/", "RADARR_2_API_KEY": "k2", "RADARR_2_NAME": "4K",
             "SONARR_URL": self.s1.url, "SONARR_API_KEY": "arr-key",
             "PLEX_URL": self.plex.url, "PLEX_TOKEN": "plex-token", "DATA_DIR": self.tmp}
        e.update(extra)
        return {k: v for k, v in e.items() if v is not None}

    def app(self, **extra):
        a = sc.App(sc.Config(self.env(**extra)), out=self.out.append)
        a.scan_poll = a.scan_start_wait = 0
        return a

    def mark(self, a, key, d, title, year):
        code, err = a.api_set({"k": key, "d": d, "title": title, "year": year})
        self.assertEqual((code, err), (200, None))


class ConfigTests(unittest.TestCase):
    def test_instances_numbered_and_plain(self):
        c = sc.Config({"RADARR_URL": "http://a:7878/", "RADARR_API_KEY": "x", "RADARR_2_URL": "https://b", "RADARR_2_API_KEY": "y",
                       "RADARR_2_NAME": "4K", "SONARR_1_URL": "http://s", "SONARR_1_API_KEY": "z"})
        self.assertEqual([(i["id"], i["name"], i["url"]) for i in c.radarrs], [("r0", "Radarr 0", "http://a:7878"), ("r2", "4K", "https://b")])
        self.assertEqual([(i["id"], i["name"]) for i in c.sonarrs], [("s1", "Sonarr")])
        self.assertEqual(c.plexes, [])

    def test_errors(self):
        for env, word in (({}, "nothing to show"), ({"RADARR_URL": "http://a"}, "RADARR_API_KEY is missing"),
                          ({"RADARR_URL": "a:7878", "RADARR_API_KEY": "x"}, "must start with http"),
                          ({"RADARR_URL": "http://a", "RADARR_API_KEY": "x", "DRY_RUN": "maybe"}, "DRY_RUN must be true or false"),
                          ({"RADARR_URL": "http://a", "RADARR_API_KEY": "x", "PORT": "99999"}, "PORT must be a number"),
                          ({"RADARR_URL": "http://a", "RADARR_API_KEY": "x", "PLEX_URL": "http://p"}, "PLEX_TOKEN is missing")):
            with self.assertRaises(sc.ConfigError) as cm:
                sc.Config(env)
            self.assertIn(word, str(cm.exception))

    def test_env_file(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, ".env")
        with open(p, "w") as f:
            f.write("# comment\nRADARR_URL = \"http://a\"\nexport RADARR_API_KEY='k'\nPORT=1\n\nbroken line\n")
        env = {"PORT": "9000"}
        sc.load_env_file(p, env)
        self.assertEqual(env, {"PORT": "9000", "RADARR_URL": "http://a", "RADARR_API_KEY": "k"})
        shutil.rmtree(d)

    def test_errors_never_carry_secrets(self):
        with self.assertRaises(sc.ApiError) as cm:
            sc.call("http://127.0.0.1:1/api/v3/movie?apikey=SECRET", headers={"X-Api-Key": "SECRET"}, timeout=2)
        self.assertNotIn("SECRET", str(cm.exception))


class BuildTests(Base):
    def test_rows_and_watch_state(self):
        self.r1.add(10, "Alpha", 2001, size=5 * 2**30); self.r1.add(11, "No File", 2002, files=False); self.r1.add(12, "Legacy", 2003)
        self.r2.add(10, "Alpha", 2001, size=40 * 2**30); self.r2.add(20, "Beta", 2010)
        self.s1.add(500, "Gamma Show", 2015, size=30 * 2**30); self.s1.add(501, "Empty Show", 2016, files=False)
        a1 = self.plex.add_movie(10, "Alpha", plays=2, last=1700000000); self.plex.add_movie(10, "Alpha 4K", plays=1, last=1600000000)
        beta = self.plex.add_movie(20, "Beta"); self.plex.add_movie(12, "Legacy", plays=1, last=5, legacy=True)
        show = self.plex.add_show(500, "Gamma Show", episodes=20, watched=5, last=1650000000)
        self.plex.play_by(7, beta); self.plex.play_by(1, beta); self.plex.play_by(7, show, episode_of_show=True); self.plex.play_by(99, a1)
        a = self.app()
        self.assertEqual(a.build(), 5)
        rows = {r["k"]: r for r in a.items}
        self.assertEqual(sorted(rows), ["r1:10", "r1:12", "r2:10", "r2:20", "s0:500"])
        al = rows["r1:10"]
        self.assertEqual((al["n"], al["last"], al["full"], al["also"], al["srcn"], al["q"], al["sz"]), (3, 1700000000, True, ["4K"], "HD", "Bluray-1080p", 5 * 2**30))
        self.assertEqual(al["oth"], ["another user"])
        self.assertEqual((rows["r2:20"]["n"], rows["r2:20"]["oth"], rows["r2:20"]["full"]), (0, ["guest"], False))
        self.assertEqual(rows["r1:12"]["n"], 1, "legacy Plex agent ids are understood")
        g = rows["s0:500"]
        self.assertEqual((g["kind"], g["n"], g["eps"], g["full"], g["oth"], g["q"], g["tvdb"]), ("show", 5, 20, False, ["guest"], "2 seasons, 20 episodes, ended", 500))
        with open(os.path.join(self.tmp, "items.json")) as f:
            self.assertEqual(json.load(f)["built"], a.built)

    def test_no_plex_configured(self):
        self.r1.add(10, "Alpha", 2001)
        a = self.app(PLEX_URL=None, PLEX_TOKEN=None)
        self.assertEqual(a.build(), 1)
        self.assertEqual((a.items[0]["inplex"], a.state()["plex"]), (False, False))
        self.assertEqual(a.api_set({"k": "r1:10", "d": "seen"}), (400, "no Plex server is configured"))

    def test_wrong_key_is_reported_without_it(self):
        self.r1.add(10, "Alpha", 2001)
        a = self.app(RADARR_1_API_KEY="wrong-key")
        with self.assertRaises(sc.ApiError) as cm:
            a.build()
        self.assertIn("HTTP 401", str(cm.exception)); self.assertNotIn("wrong-key", str(cm.exception))


class DeleteTests(Base):
    def world(self, **extra):
        self.r1.add(10, "Alpha", 2001, size=5 * 2**30); self.r1.add(30, "Gamma: Part Two", 2003, size=2**30); self.r1.add(50, "Keeper", 2005)
        self.r2.add(10, "Alpha", 2001, size=40 * 2**30); self.r2.add(20, "Beta", 2002, size=30 * 2**30)
        self.s1.add(500, "Gamma Show", 2015, size=30 * 2**30)
        self.plex.add_movie(10, "Alpha"); self.plex.add_show(500, "Gamma Show")
        a = self.app(**extra); a.build()
        return a

    def test_removes_from_every_instance_and_blocks_everywhere(self):
        a = self.world(PLEX_EMPTY_TRASH="true")
        self.r1.queue = [{"id": 900, "movieId": 1}, {"id": 901, "movieId": 3}]
        self.mark(a, "r1:10", "delete", "Alpha", 2001); self.mark(a, "r2:20", "delete", "Beta", 2002)
        self.mark(a, "r1:50", "keep", "Keeper", 2005); self.mark(a, "s0:500", "delete", "Gamma Show", 2015)
        seen = []
        res = a.process_delete(lambda n, total, freed: seen.append((n, total)))
        self.assertEqual((res["removed"], res["failed"], res["freed"]), (3, [], (5 + 40 + 30 + 30) * 2**30))
        self.assertEqual([d["ext"] for d in self.r1.deleted], [10]); self.assertEqual(sorted(d["ext"] for d in self.r2.deleted), [10, 20])
        self.assertEqual(self.r1.deleted[0]["deleteFiles"], "true"); self.assertEqual(self.r1.deleted[0]["exclude"], "true")
        self.assertIn({"tmdbId": 20, "movieTitle": "Beta", "movieYear": 2002}, self.r1.exclusions, "blocked in the Radarr that never had it")
        self.assertEqual([r["id"] for r in self.r1.queue], [901], "its running download is removed, others stay")
        self.assertEqual([d["ext"] for d in self.s1.deleted], [500]); self.assertEqual(self.s1.deleted[0]["exclude"], "true")
        d = a.store.decisions()
        self.assertTrue(d["r1:10"]["done"] and d["r2:20"]["done"] and d["s0:500"]["done"]); self.assertEqual(d["r1:50"]["d"], "keep")
        self.assertEqual(seen[-1], (3, 3))
        self.assertEqual((sorted(self.plex.scanned), sorted(self.plex.trash_emptied)), (["1", "2"], ["1", "2"]), "movie + show libraries, not music")
        self.assertEqual(a.api_set({"k": "r1:10", "d": "keep"}), (409, "that title was already removed"))

    def test_trash_not_emptied_by_default(self):
        a = self.world(); self.mark(a, "r2:20", "delete", "Beta", 2002); a.process_delete()
        self.assertEqual((self.plex.scanned, self.plex.trash_emptied), (["1"], []))

    def test_title_mismatch_and_gone_and_failure(self):
        a = self.world()
        self.mark(a, "r1:30", "delete", "Something Else", 2003); self.mark(a, "r1:777", "delete", "Vanished", 1999)
        self.mark(a, "r2:20", "delete", "Beta", 2002); self.r2.fail_delete.add(2)
        res = a.process_delete()
        self.assertEqual(res["removed"], 0); self.assertEqual(len(res["failed"]), 2)
        self.assertEqual(self.r1.deleted, []); self.assertEqual(self.r2.deleted, [])
        d = a.store.decisions()
        self.assertEqual(d["r1:777"].get("note"), "already gone"); self.assertNotIn("done", d["r1:30"]); self.assertNotIn("done", d["r2:20"])
        self.assertEqual(self.plex.scanned, [], "nothing removed: Plex is left alone")
        self.r2.fail_delete.clear()
        self.assertEqual(a.process_delete()["removed"], 1, "the failed one is retried on the next run")

    def test_punctuation_in_titles_is_tolerated(self):
        a = self.world(); self.mark(a, "r1:30", "delete", "Gamma - Part Two!", 2003)
        self.assertEqual(a.process_delete()["removed"], 1)

    def test_dry_run_changes_nothing(self):
        a = self.world(DRY_RUN="true"); self.mark(a, "r1:10", "delete", "Alpha", 2001)
        res = a.process_delete()
        self.assertEqual((res["removed"], res["dry"]), (1, True))
        self.assertEqual((self.r1.deleted, self.r2.deleted, self.plex.scanned), ([], [], []))
        self.assertNotIn("done", a.store.decisions()["r1:10"])
        self.assertTrue(any("DRY RUN, would remove: Alpha (2001) from HD + 4K" in line for line in self.out))

    def test_options_single_instance_no_exclusion_keep_files(self):
        a = self.world(ALL_INSTANCES="false", ADD_EXCLUSION="false", DELETE_FILES="false", PLEX_SCAN_AFTER_DELETE="false")
        self.mark(a, "r1:10", "delete", "Alpha", 2001); a.process_delete()
        self.assertEqual(self.r1.deleted, [{"id": 1, "ext": 10, "deleteFiles": "false", "exclude": "false"}])
        self.assertEqual((self.r2.deleted, self.r2.exclusions, self.plex.scanned), ([], [], []))


class SeenTests(Base):
    def test_marks_every_plex_copy_and_drops_the_mark(self):
        self.r1.add(10, "Alpha", 2001); self.r1.add(30, "Not In Plex", 2003); self.s1.add(500, "Gamma Show", 2015)
        m1 = self.plex.add_movie(10, "Alpha"); m2 = self.plex.add_movie(10, "Alpha 4K"); show = self.plex.add_show(500, "Gamma Show", episodes=20, watched=3)
        a = self.app(); a.build()
        self.mark(a, "r1:10", "seen", "Alpha", 2001); self.mark(a, "s0:500", "seen", "Gamma Show", 2015); self.mark(a, "r1:30", "seen", "Not In Plex", 2003)
        self.assertEqual(a.process_seen(), 2)
        self.assertEqual((m1["viewCount"], m2["viewCount"], show["viewedLeafCount"]), (1, 1, 20))
        d = a.store.decisions()
        self.assertNotIn("r1:10", d); self.assertNotIn("s0:500", d)
        self.assertEqual((d["r1:30"]["d"], d["r1:30"]["error"]), ("failed", "Plex has no item with that id"))
        self.assertEqual(a.process_seen(), 0, "a failed mark is not retried forever")
        self.assertEqual((self.r1.deleted, self.s1.deleted), ([], []))
        a.build(); rows = {r["k"]: r for r in a.items}
        self.assertTrue(rows["r1:10"]["full"] and rows["s0:500"]["full"])


class HttpTests(Base):
    def start(self, **extra):
        self.r1.add(10, "Alpha", 2001, size=5 * 2**30); self.r2.add(20, "Beta", 2002); self.plex.add_movie(10, "Alpha")
        cfg = sc.Config(self.env(LISTEN="127.0.0.1", **extra)); cfg.port = 0
        self.a = sc.App(cfg, out=self.out.append); self.a.scan_poll = self.a.scan_start_wait = 0
        self.srv = sc.make_server(self.a)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        threading.Thread(target=self.a.run_worker, daemon=True).start()
        self.addCleanup(lambda: (self.a.stop.set(), self.a.wake.set(), self.srv.shutdown(), self.srv.server_close()))

    def http(self, path, body=None, headers=None, method=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read(); return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def wait(self, cond, what, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for " + what)

    def test_full_flow_over_http(self):
        self.start()
        self.wait(lambda: self.http("/api/items")[1]["built"], "the first list")
        code, items, _ = self.http("/api/items")
        self.assertEqual(sorted(i["k"] for i in items["items"]), ["r1:10", "r2:20"])
        self.assertEqual([i["name"] for i in items["instances"]], ["HD", "4K", "Sonarr"])
        for path, ctype in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css"), ("/icon.svg", "image/svg+xml")):
            code, _raw, hdr = self.http(path)
            self.assertEqual(code, 200); self.assertTrue(hdr["Content-Type"].startswith(ctype)); self.assertIn("default-src 'self'", hdr["Content-Security-Policy"])
        self.assertEqual(self.http("/../etc/passwd")[0], 404); self.assertEqual(self.http("/data/decisions.json")[0], 404)
        self.assertEqual(self.http("/api/run", {})[0], 409)
        for bad in ({"k": "x1:10", "d": "delete"}, {"k": "r1:10", "d": "explode"}, {"k": "r1:10; rm", "d": "keep"}, {"k": 5, "d": "keep"}):
            self.assertEqual(self.http("/api/set", bad)[0], 400)
        self.assertEqual(self.http("/api/set", [1])[0], 400)
        self.assertEqual(self.http("/api/set", {"k": "r1:10", "d": "delete"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.http("/api/set", {"k": "r1:10", "d": "delete"}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.http("/api/state")[1]["decisions"], {})
        code, st, _ = self.http("/api/set", {"k": "r1:10", "d": "delete", "title": "Alpha", "year": 2001}, {"Origin": self.base})
        self.assertEqual((code, st["pending"]), (200, 1))
        code, st, _ = self.http("/api/run", {})
        self.assertEqual(code, 200)
        self.wait(lambda: not self.http("/api/state")[1]["queued"], "the run")
        st = self.http("/api/state")[1]
        self.assertEqual((st["status"]["state"], st["status"]["removed"], st["status"]["rc"], st["pending"]), ("done", 1, 0, 0))
        self.assertEqual([d["ext"] for d in self.r1.deleted], [10])
        self.assertEqual([i["k"] for i in self.http("/api/items")[1]["items"]], ["r2:20"], "the list is rebuilt before the run reports done")

    def test_seen_over_http(self):
        self.start()
        self.wait(lambda: self.http("/api/items")[1]["built"], "the first list")
        self.assertEqual(self.http("/api/set", {"k": "r1:10", "d": "seen", "title": "Alpha", "year": 2001})[0], 200)
        self.wait(lambda: self.plex.items["1"][0].get("viewCount") == 1, "Plex to be marked")
        self.wait(lambda: any(i["full"] for i in self.http("/api/items")[1]["items"]), "the rebuilt list")
        self.assertEqual(self.http("/api/state")[1]["decisions"], {})

    def test_password(self):
        self.start(AUTH_PASSWORD="s3cret pass", AUTH_USER="me")
        basic = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
        self.assertEqual(self.http("/healthz")[0], 200)
        for path in ("/", "/api/state", "/api/items", "/app.js"):
            code, _b, hdr = self.http(path)
            self.assertEqual(code, 401); self.assertIn("Basic", hdr["WWW-Authenticate"])
        self.assertEqual(self.http("/api/set", {"k": "r1:10", "d": "delete"})[0], 401)
        self.assertEqual(self.http("/api/state", headers=basic("me", "wrong"))[0], 401)
        self.assertEqual(self.http("/api/state", headers=basic("admin", "s3cret pass"))[0], 401)
        self.assertEqual(self.http("/api/state", headers={"Authorization": "Basic !!!"})[0], 401)
        self.assertEqual(self.http("/api/state", headers=basic("me", "s3cret pass"))[0], 200)

    def test_unreachable_radarr_is_shown_not_fatal(self):
        self.start(RADARR_2_URL="http://127.0.0.1:1")
        self.wait(lambda: self.http("/api/state")[1]["error"], "the error")
        st = self.http("/api/state")[1]
        self.assertIn("cannot reach 127.0.0.1:1", st["error"]); self.assertNotIn("k2", st["error"])
        self.assertEqual(self.http("/")[0], 200)


if __name__ == "__main__":
    unittest.main()
