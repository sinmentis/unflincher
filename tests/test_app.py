def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_assets_get_no_cache_header(client):
    # Regression test: StaticFiles only sets ETag/Last-Modified by default, which left both
    # browsers and Cloudflare's edge cache (this hostname is proxied) free to keep serving a
    # stale copy indefinitely after a deploy -- a real bug the owner hit in production. no-cache
    # forces revalidation on every request without disabling caching entirely.
    response = client.get("/static/theme.css")
    assert response.headers["cache-control"] == "no-cache"


def test_non_static_routes_are_unaffected_by_the_no_cache_header(client):
    response = client.get("/healthz")
    assert "cache-control" not in {k.lower() for k in response.headers}
