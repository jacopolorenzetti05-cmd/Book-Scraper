"""Offline tests for the library app's query building: `python -m unittest discover tests`."""

import unittest

from app.server import PAGE_SIZE, BadRequest, book_filters


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
