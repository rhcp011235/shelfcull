"""
Try shelfcull without touching a real library: starts fake Radarr (x2), Sonarr and Plex servers filled with invented
titles, then shelfcull itself on http://127.0.0.1:8765/ (PORT overrides).   python tests/demo.py
"""
import base64, os, random, sys, tempfile, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakeArr, FakePlex                                    # noqa: E402
from shelfcull import app as sc                                        # noqa: E402

A = "Silent Broken Last Hidden Crimson Midnight Paper Iron Golden Hollow Electric Distant Burning Quiet Savage Frozen".split()
B = "Harbor Orchard Signal Empire Garden Witness Lantern Highway Promise Kingdom Mirror Season Covenant Meridian Station Parade".split()
GENRES = ["Drama", "Thriller", "Comedy", "Horror", "Sci-Fi", "Crime", "Mystery", "Romance", "Action", "Western"]


def poster(title, hue):
    words = title.split()
    lines = "".join(f'<text x="100" y="{150 + i * 30}" font-size="24" font-family="Georgia,serif" fill="#fff" text-anchor="middle">{w}</text>'
                    for i, w in enumerate(words))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           f'<stop offset="0" stop-color="hsl({hue},55%,38%)"/><stop offset="1" stop-color="hsl({(hue + 70) % 360},60%,14%)"/></linearGradient></defs>'
           f'<rect width="200" height="300" fill="url(#g)"/><circle cx="150" cy="70" r="34" fill="hsla({(hue + 180) % 360},70%,70%,.35)"/>{lines}</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def fill(rnd):
    hd, uhd, tv, plex = FakeArr("movie"), FakeArr("movie"), FakeArr("show"), FakePlex()
    titles = [f"{a} {b}" for a in A for b in B]
    rnd.shuffle(titles)
    for i, t in enumerate(titles[:150]):
        year, tmdb = rnd.randint(1972, 2025), 1000 + i
        extra = dict(images=[{"coverType": "poster", "remoteUrl": poster(t, rnd.randrange(360))}], genres=rnd.sample(GENRES, 2),
                     runtime=rnd.randint(82, 150), overview=f"When the {t.split()[1].lower()} falls quiet, one stranger has a single night to "
                     f"find out what the town has been hiding. An invented film for the shelfcull demo.", certification=rnd.choice(["PG-13", "R", "PG"]),
                     added=f"{rnd.randint(2019, 2025)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}T12:00:00Z")
        arr = uhd if i % 4 == 0 else hd
        m = arr.add(tmdb, t, year, size=rnd.randint(2, 60) * 2**30, **extra)
        m["movieFile"]["dateAdded"] = extra["added"]
        m["movieFile"]["quality"]["quality"]["name"] = "Remux-2160p" if arr is uhd else rnd.choice(["Bluray-1080p", "WEBDL-1080p"])
        m["ratings"] = {"imdb": {"value": round(rnd.uniform(3.8, 8.6), 1), "votes": rnd.randint(300, 400000)}}
        if i % 9 == 0 and arr is uhd:
            hd.add(tmdb, t, year, size=rnd.randint(2, 12) * 2**30, **extra)
        roll = rnd.random()
        p = plex.add_movie(tmdb, t, plays=rnd.randint(1, 3) if roll < 0.3 else 0, last=rnd.randint(1600000000, 1760000000) if roll < 0.3 else 0)
        if 0.3 <= roll < 0.4:
            plex.play_by(7, p)
    for i, t in enumerate(titles[150:185]):
        eps = rnd.randint(6, 80)
        s = tv.add(5000 + i, t, rnd.randint(1995, 2025), size=eps * rnd.randint(1, 4) * 2**30,
                   images=[{"coverType": "poster", "remoteUrl": poster(t, rnd.randrange(360))}], genres=rnd.sample(GENRES, 2), runtime=rnd.choice([22, 44, 58]),
                   overview="An invented series for the shelfcull demo.", status=rnd.choice(["ended", "continuing"]))
        ns = max(1, eps // 10)
        s["statistics"].update(seasonCount=ns, episodeFileCount=eps)
        s["seasons"] = [{"seasonNumber": n + 1, "statistics": {"episodeFileCount": eps // ns + (eps % ns if n == 0 else 0),
                                                              "sizeOnDisk": s["statistics"]["sizeOnDisk"] // ns}} for n in range(ns)]
        plex.add_show(5000 + i, t, episodes=eps, watched=rnd.choice([0, 0, eps // 3, eps]), last=rnd.randint(1600000000, 1760000000))
    return hd, uhd, tv, plex


def start(port=8765, data_dir=None, seed=7, **extra):
    hd, uhd, tv, plex = fill(random.Random(seed))
    env = {"RADARR_1_URL": hd.url, "RADARR_1_API_KEY": "arr-key", "RADARR_1_NAME": "Movies HD", "RADARR_2_URL": uhd.url, "RADARR_2_API_KEY": "arr-key",
           "RADARR_2_NAME": "Movies 4K", "SONARR_URL": tv.url, "SONARR_API_KEY": "arr-key", "SONARR_NAME": "TV", "PLEX_URL": plex.url,
           "PLEX_TOKEN": "plex-token", "LISTEN": "127.0.0.1", "PORT": str(port), "DATA_DIR": data_dir or tempfile.mkdtemp(prefix="shelfcull-demo-")}
    env.update(extra)
    app = sc.App(sc.Config(env))
    app.scan_poll = app.scan_start_wait = 0
    srv = sc.make_server(app)
    threading.Thread(target=app.run_worker, daemon=True).start()
    return app, srv, (hd, uhd, tv, plex)


if __name__ == "__main__":
    app, srv, _fakes = start(int(os.environ.get("PORT", "8765")), DEFAULT_VIEW=os.environ.get("DEMO_DEFAULT_VIEW", "all"))
    print(f"shelfcull demo with invented titles: http://127.0.0.1:{srv.server_address[1]}/   (Ctrl-C stops it)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
