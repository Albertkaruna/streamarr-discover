def test_img_rejects_bad_names_and_sizes():
    from fastapi.testclient import TestClient

    from discover import app, config
    c = TestClient(app.app)
    c.cookies.set("discover", config.SESSION)
    for url in ("/img/w342/..%2Fdiscover.db", "/img/w9999/abc.jpg", "/img/w342/abc.exe", "/img/w342/a%20b.jpg"):
        assert c.get(url).status_code == 404, url


def test_img_serves_cached_file_without_network():
    from fastapi.testclient import TestClient

    from discover import app, config
    from discover.features import img
    (img.ROOT / "w92").mkdir(parents=True, exist_ok=True)
    f = img.ROOT / "w92" / "testposter.jpg"
    f.write_bytes(b"\xff\xd8jpeg")
    try:
        c = TestClient(app.app)
        c.cookies.set("discover", config.SESSION)
        r = c.get("/img/w92/testposter.jpg")
        assert r.status_code == 200 and r.content == b"\xff\xd8jpeg" and "immutable" in r.headers["cache-control"]
    finally:
        f.unlink()


def test_upstream_errors_become_friendly_503():
    import httpx
    from fastapi.testclient import TestClient

    from discover import app, config

    @app.app.get("/__test/boom")
    async def boom():
        raise httpx.ConnectError("down")
    c = TestClient(app.app)
    c.cookies.set("discover", config.SESSION)
    r = c.get("/__test/boom")
    assert r.status_code == 503 and "Try again" in r.text
    assert c.get("/__test/boom", headers={"x-requested-with": "fetch"}).json()["ok"] is False
