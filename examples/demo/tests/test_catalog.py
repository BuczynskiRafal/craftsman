"""Tests for product catalog queries."""

from __future__ import annotations

import unittest

from shop.catalog import Product, list_products

PRODUCTS = [
    Product("SKU-1", "Alpha Widget", 1000),
    Product("SKU-2", "Beta Gadget", 2500),
    Product("SKU-3", "Gamma Widget", 1500),
]


class CatalogTestCase(unittest.TestCase):
    def test_list_products_all(self) -> None:
        results = list_products(PRODUCTS)
        self.assertEqual([p.sku for p in results], ["SKU-1", "SKU-2", "SKU-3"])

    def test_list_products_filters_by_query(self) -> None:
        results = list_products(PRODUCTS, "widget")
        self.assertEqual([p.sku for p in results], ["SKU-1", "SKU-3"])

    def test_list_products_case_insensitive(self) -> None:
        results = list_products(PRODUCTS, "BETA")
        self.assertEqual([p.sku for p in results], ["SKU-2"])


if __name__ == "__main__":
    unittest.main()
