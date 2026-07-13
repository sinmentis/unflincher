def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_assets_get_no_cache_header(client):
    # Regression test: StaticFiles only sets ETag/Last-Modified by default, which left both
    # browsers and Cloudflare's edge cache (this hostname is proxied) free to keep serving a
    # stale copy indefinitely after a deploy -- a real bug the owner hit in production. no-cache
    # forces revalidation on every request without disabling caching entirely. Cover every kind
    # of static asset the redesign ships (ordered stylesheets, shared/page JavaScript, self-hosted
    # fonts, the license, and the favicon) so a future asset move cannot silently lose the header.
    for path in (
        "/static/css/tokens.css",
        "/static/css/base.css",
        "/static/css/shell.css",
        "/static/css/components.css",
        "/static/css/pages.css",
        "/static/app.js",
        "/static/js/timeline.js",
        "/static/js/entry.js",
        "/static/js/report.js",
        "/static/js/chat.js",
        "/static/js/new-entry.js",
        "/static/js/workshop.js",
        "/static/fonts/IBMPlexSansCondensed-Regular.woff2",
        "/static/fonts/OFL.txt",
        "/static/favicon.svg",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_non_static_routes_are_unaffected_by_the_no_cache_header(client):
    response = client.get("/healthz")
    assert "cache-control" not in {k.lower() for k in response.headers}
