"""Offline tests for the library app's query building: `python -m unittest discover tests`."""

import os
import tempfile
import unittest
from decimal import Decimal

from app.server import PAGE_SIZE, BadRequest, SqliteLibrary, book_filters
from scraper.models import Book, Category, ScrapeRun
from scraper.store import SqliteSink


class BookFilterTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(book_filters({}), {"order": "title.asc", "limit": str(PAGE_SIZE), "offset": "0"})

    def test_all_filters(self):
        f = book_filters({"q": " attic ", "category": "poetry_23", "min_price": "10",
                          "max_price": "30.5", "min_rating": "4", "sort": "price_desc", "page": "3"})
        self.assertEqual(f["title"], "ilike.*attic*")
        self.assertEqual(f["category_slug"], "eq.poetry_23")
        self.assertEqual(f["and"], "(price.gte.10.0,price.lte.30.5)")
        self.assertEqual(f["rating"], "gte.4")
        self.assertEqual(f["order"], "price.desc,title.asc")
        self.assertEqual(f["offset"], str(2 * PAGE_SIZE))

    def test_single_price_bound(self):
        self.assertEqual(book_filters({"max_price": "20"})["price"], "lte.20.0")
        self.assertNotIn("and", book_filters({"max_price": "20"}))

    def test_wildcards_are_literal(self):
        self.assertEqual(book_filters({"q": "a*b%c"})["title"], "ilike.*a b c*")
        self.assertNotIn("title", book_filters({"q": "**"}))

    def test_unknown_sort_falls_back(self):
        self.assertEqual(book_filters({"sort": "upc;drop"})["order"], "title.asc")

    def test_rejects_bad_input(self):
        for params in ({"category": "poetry;drop"}, {"min_price": "abc"}, {"min_price": "-1"},
                       {"min_rating": "6"}, {"page": "0"}, {"max_price": "nan"}):
            with self.subTest(params=params), self.assertRaises(BadRequest):
                book_filters(params)


class SqliteLibraryTests(unittest.TestCase):
    """The offline mode of the app reads the SQLite file the scraper writes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "books.db")
        sink = SqliteSink(path)
        run = ScrapeRun()
        run_id = sink.start_run(run)
        poetry, travel = Category("poetry_23", "Poetry"), Category("travel_2", "Travel")
        books = [
            Book(upc="a1", title="A Light in the Attic", category=poetry, price=Decimal("51.77"), stock=22,
                 rating=3, description="Poems", image_url=None, url="https://books.toscrape.com/a1"),
            Book(upc="b2", title="Its Only the Himalayas", category=travel, price=Decimal("45.17"), stock=19,
                 rating=2, description=None, image_url=None, url="https://books.toscrape.com/b2"),
            Book(upc="c3", title="Olio", category=poetry, price=Decimal("23.88"), stock=19, rating=1,
                 description=None, image_url=None, url="https://books.toscrape.com/c3"),
        ]
        sink.save(run_id, books)
        run.finished_at, run.status = run.started_at, "success"
        sink.finish_run(run_id, run)
        sink.conn.close()
        cls.lib = SqliteLibrary(path)

    @classmethod
    def tearDownClass(cls):
        cls.lib.conn.close()
        cls.tmp.cleanup()

    def test_categories_with_counts(self):
        self.assertEqual(self.lib.categories(), [
            {"slug": "poetry_23", "name": "Poetry", "count": 2},
            {"slug": "travel_2", "name": "Travel", "count": 1}])

    def test_filters_and_sort(self):
        data = self.lib.books({"category": "poetry_23", "sort": "price_asc"})
        self.assertEqual([b["upc"] for b in data["books"]], ["c3", "a1"])
        self.assertEqual(data["total"], 2)
        self.assertEqual(self.lib.books({"q": "attic"})["total"], 1)
        self.assertEqual(self.lib.books({"min_price": "40", "max_price": "50"})["books"][0]["upc"], "b2")
        self.assertEqual(self.lib.books({"min_rating": "3"})["total"], 1)

    def test_book_detail_has_history(self):
        book = self.lib.book("a1")
        self.assertEqual(book["category_name"], "Poetry")
        self.assertEqual(len(book["history"]), 1)
        self.assertAlmostEqual(book["history"][0]["price"], 51.77)
        self.assertIsNone(self.lib.book("zz9"))

    def test_injection_is_inert(self):
        self.assertEqual(self.lib.books({"q": "' OR 1=1 --"})["total"], 0)
        with self.assertRaises(BadRequest):
            self.lib.books({"category": "x' OR '1'='1"})
        with self.assertRaises(BadRequest):
            self.lib.book("a1' OR '1'='1")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            SqliteLibrary(os.path.join(self.tmp.name, "nope.db"))


if __name__ == "__main__":
    unittest.main()


class UnreachableDatabaseTests(unittest.TestCase):
    """A paused or unreachable Supabase must give a JSON error, not a dropped connection."""

    def test_api_answers_503(self):
        import json
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        import requests

        from app.server import make_handler

        class DownLibrary:
            def categories(self):
                raise requests.ConnectionError("paused")

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DownLibrary()))
        server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/categories"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                opener.open(url, timeout=5)
            self.assertEqual(ctx.exception.code, 503)
            self.assertIn("unreachable", json.loads(ctx.exception.read())["error"])
        finally:
            server.shutdown()
            server.server_close()
