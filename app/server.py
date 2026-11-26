"""Library app: browse the scraped books in Supabase.

    python app/server.py            # then open http://localhost:8000

The tables are locked by RLS, so the browser never talks to Supabase directly: this small
server holds the service_role key and exposes a read-only JSON API to the page.

    GET /api/categories                   all categories with their book counts
    GET /api/books?q=&category=&min_price=&max_price=&min_rating=&sort=&page=
    GET /api/books/<upc>                  one book plus its price/stock per scrape run

Offline (no Supabase key needed): python app/server.py --sqlite books.db
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

STATIC = Path(__file__).parent / "static"
PAGE_SIZE = 24
SORTS = {
    "title": "title.asc",
    "price_asc": "price.asc,title.asc",
    "price_desc": "price.desc,title.asc",
    "rating": "rating.desc,title.asc",
    "stock": "stock.desc,title.asc",
}
LIST_COLUMNS = "upc,title,price,stock,rating,image_url,category_slug,category_name"
SLUG_RE = re.compile(r"^[a-z0-9-]+_\d+$")
UPC_RE = re.compile(r"^[A-Za-z0-9]+$")


class BadRequest(ValueError):
    pass


def book_filters(params: dict[str, str]) -> dict[str, str]:
    """Turn the page's query string into PostgREST params for books_with_category.

    Everything is validated or whitelisted here, so user input never shapes the query
    beyond a literal value.
    """
    out: dict[str, str] = {"order": SORTS.get(params.get("sort", ""), SORTS["title"])}

    q = params.get("q", "").strip()
    if q:
        # * and % are wildcards to PostgREST/Postgres; strip them so the search is literal.
        q = re.sub(r"[*%]", " ", q)[:100].strip()
        if q:
            out["title"] = f"ilike.*{q}*"

    category = params.get("category", "")
    if category:
        if not SLUG_RE.match(category):
            raise BadRequest("bad category")
        out["category_slug"] = f"eq.{category}"

    prices = []
    if params.get("min_price"):
        prices.append(f"price.gte.{_number(params['min_price'])}")
    if params.get("max_price"):
        prices.append(f"price.lte.{_number(params['max_price'])}")
    if len(prices) == 2:
        out["and"] = f"({','.join(prices)})"
    elif prices:
        _, op, value = prices[0].split(".", 2)
        out["price"] = f"{op}.{value}"

    if params.get("min_rating"):
        rating = int(_number(params["min_rating"]))
        if not 1 <= rating <= 5:
            raise BadRequest("min_rating must be 1-5")
        out["rating"] = f"gte.{rating}"

    page = int(_number(params.get("page") or "1"))
    if page < 1:
        raise BadRequest("page must be >= 1")
    out["limit"] = str(PAGE_SIZE)
    out["offset"] = str((page - 1) * PAGE_SIZE)
    return out


def _number(value: str) -> float:
    try:
        n = float(value)
    except ValueError:
        raise BadRequest(f"not a number: {value!r}") from None
    if n != n or n < 0 or n > 1e6:
        raise BadRequest(f"out of range: {value!r}")
    return n


class Library:
    """Read-only queries the page needs."""

    def __init__(self, client: SupabaseClient):
        self.client = client

    def categories(self) -> list[dict]:
        rows = self.client.select("categories", "slug,name,books(count)", order="name.asc")
        return [{"slug": r["slug"], "name": r["name"], "count": r["books"][0]["count"]} for r in rows]

    def books(self, params: dict[str, str]) -> dict:
        query = {"select": LIST_COLUMNS, **book_filters(params)}
        resp = self.client.session.get(
            f"{self.client.base}/books_with_category", params=query,
            headers={"Prefer": "count=exact"}, timeout=self.client.timeout,
        )
        rows = self.client._check(resp)
        total = int(resp.headers.get("Content-Range", "*/0").rsplit("/", 1)[1])
        return {"books": rows, "total": total, "page_size": PAGE_SIZE}

    def book(self, upc: str) -> dict | None:
        if not UPC_RE.match(upc):
            raise BadRequest("bad upc")
        rows = self.client.select("books_with_category", upc=f"eq.{upc}")
        if not rows:
            return None
        history = self.client.select(
            "book_snapshots", "run_id,price,stock,scrape_runs(finished_at)",
            upc=f"eq.{upc}", order="run_id.asc",
        )
        book = rows[0]
        book["history"] = [
            {"run_id": h["run_id"], "price": h["price"], "stock": h["stock"],
             "date": (h["scrape_runs"] or {}).get("finished_at")}
            for h in history
        ]
        return book


SQLITE_SORTS = {
    "title": "b.title ASC",
    "price_asc": "CAST(b.price AS REAL) ASC, b.title ASC",
    "price_desc": "CAST(b.price AS REAL) DESC, b.title ASC",
    "rating": "b.rating DESC, b.title ASC",
    "stock": "b.stock DESC, b.title ASC",
}
SQLITE_BOOK_SELECT = (
    "SELECT b.upc, b.title, CAST(b.price AS REAL) AS price, b.stock, b.rating, b.description, "
    "b.image_url, b.url, b.category_slug, c.name AS category_name "
    "FROM books b JOIN categories c ON c.slug = b.category_slug"
)


class SqliteLibrary:
    """Same queries as `Library`, read from the local SQLite file the scraper writes.

    Lets anyone run the app with no Supabase account:
        python scrape.py --sink sqlite --limit 200
        python app/server.py --sqlite books.db
    Every user value is validated the same way as for Supabase and passed as a bound parameter.
    """

    def __init__(self, path: str):
        if not Path(path).exists():
            raise FileNotFoundError(f"{path} not found: run `python scrape.py --sink sqlite` first")
        # Read-only connection: the app never writes. check_same_thread=False because the
        # server answers each request on its own thread; reads on one connection are safe.
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def categories(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT c.slug, c.name, COUNT(b.upc) AS count FROM categories c "
            "LEFT JOIN books b ON b.category_slug = c.slug GROUP BY c.slug ORDER BY c.name"
        ).fetchall()
        return [dict(r) for r in rows]

    def books(self, params: dict[str, str]) -> dict:
        book_filters(params)  # same validation as the Supabase path; raises BadRequest
        where, args = [], []
        q = re.sub(r"[*%_]", " ", params.get("q", "").strip())[:100].strip()
        if q:
            where.append("b.title LIKE ?")
            args.append(f"%{q}%")
        if params.get("category"):
            where.append("b.category_slug = ?")
            args.append(params["category"])
        if params.get("min_price"):
            where.append("CAST(b.price AS REAL) >= ?")
            args.append(_number(params["min_price"]))
        if params.get("max_price"):
            where.append("CAST(b.price AS REAL) <= ?")
            args.append(_number(params["max_price"]))
        if params.get("min_rating"):
            where.append("b.rating >= ?")
            args.append(int(_number(params["min_rating"])))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order = SQLITE_SORTS.get(params.get("sort", ""), SQLITE_SORTS["title"])
        page = int(_number(params.get("page") or "1"))
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM books b{clause}", args).fetchone()[0]
        rows = self.conn.execute(
            f"{SQLITE_BOOK_SELECT}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        columns = LIST_COLUMNS.split(",")
        return {"books": [{k: r[k] for k in columns} for r in rows], "total": total,
                "page_size": PAGE_SIZE}

    def book(self, upc: str) -> dict | None:
        if not UPC_RE.match(upc):
            raise BadRequest("bad upc")
        row = self.conn.execute(f"{SQLITE_BOOK_SELECT} WHERE b.upc = ?", (upc,)).fetchone()
        if not row:
            return None
        history = self.conn.execute(
            "SELECT s.run_id, CAST(s.price AS REAL) AS price, s.stock, r.finished_at AS date "
            "FROM book_snapshots s LEFT JOIN scrape_runs r ON r.id = s.run_id "
            "WHERE s.upc = ? ORDER BY s.run_id", (upc,),
        ).fetchall()
        return {**dict(row), "history": [dict(h) for h in history]}


# Sent with every response. The page itself needs inline script/style and Google Fonts.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' https://books.toscrape.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


def make_handler(library: Library):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/categories":
                    return self._json(library.categories())
                if url.path == "/api/books":
                    return self._json(library.books(params))
                if url.path.startswith("/api/books/"):
                    book = library.book(url.path.rsplit("/", 1)[1])
                    return self._json(book) if book else self._json({"error": "not found"}, 404)
                if url.path in ("/", "/index.html"):
                    return self._file(STATIC / "index.html", "text/html; charset=utf-8")
                self._json({"error": "not found"}, 404)
            except BadRequest as exc:
                self._json({"error": str(exc)}, 400)
            except SupabaseError as exc:
                self.log_error("supabase: %s", exc)
                self._json({"error": "database error"}, 502)
            except requests.RequestException as exc:
                # Supabase unreachable (e.g. the free project is paused): answer instead of dropping the connection.
                self.log_error("supabase unreachable: %s", exc)
                self._json({"error": "database unreachable, try again in a minute"}, 503)

        def _json(self, data, status=200):
            self._send(json.dumps(data).encode(), "application/json", status)

        def _file(self, path: Path, ctype: str):
            self._send(path.read_bytes(), ctype)

        def _send(self, body: bytes, ctype: str, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Serve the library app.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sqlite", metavar="FILE",
                    help="read a local SQLite file (e.g. books.db) instead of Supabase")
    args = ap.parse_args()

    try:
        if args.sqlite:
            library = SqliteLibrary(args.sqlite)
            source = f"SQLite file {args.sqlite}"
        else:
            library = Library(SupabaseClient.from_env())
            source = "Supabase"
    except (SupabaseError, FileNotFoundError) as exc:
        print(exc)
        if not args.sqlite:
            print("No Supabase credentials? Run offline: python app/server.py --sqlite books.db")
        return 1
    server = ThreadingHTTPServer((args.host, args.port), make_handler(library))
    print(f"Library open at http://{args.host}:{args.port} (data: {source})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
