# Copyright 2026 Be OnlyOne
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.elit_product_integration.models.elit_catalog_run import (
    ELIT_INGEST_PAGE_SIZE,
)
from odoo.addons.elit_product_integration.models.sync_processor import (
    ELIT_SYNC_MAX_CYCLE_HOURS,
)


@tagged("post_install", "-at_install")
class TestElitCatalogStaging(TransactionCase):
    def setUp(self):
        super().setUp()
        self.processor = self.env["elit.sync.processor"]
        self.Run = self.env["elit.catalog.run"]
        self.Template = self.env["product.template"]
        self.elit_partner = self.env["res.partner"].create(
            {
                "name": "ELIT Catalog Test",
                "supplier_rank": 1,
                "is_company": True,
            }
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "elit.partner_id", str(self.elit_partner.id)
        )

    def _api_product(self, codigo, stock=10, precio=5.0, nombre=None):
        return {
            "codigo_alfa": codigo,
            "codigo_producto": codigo,
            "nombre": nombre or codigo,
            "stock_total": stock,
            "precio": precio,
            "moneda": 2,
        }

    def _create_elit_product(self, codigo, stock_elit=10.0, extra=None):
        vals = {
            "name": codigo,
            "detailed_type": "product",
            "default_code": codigo,
            "elit_product_code": codigo,
            "is_elit_product": True,
            "stock_elit": stock_elit,
            "purchase_ok": True,
            "sale_ok": True,
        }
        if extra:
            vals.update(extra)
        return self.Template.create(vals)

    def _patch_fetch(self, pages_by_offset):
        """pages_by_offset: offset -> JSON dict or None (HTTP error)."""

        def fake_fetch(_self, offset, limit=100):
            if offset in pages_by_offset:
                return pages_by_offset[offset]
            return {"resultado": [], "cotizacion": 1.0}

        return patch.object(
            type(self.processor),
            "_elit_fetch_productos_page",
            fake_fetch,
        )

    def test_ingest_page_complete_uses_page_length_not_total(self):
        self.assertFalse(
            self.processor._elit_ingest_page_complete(ELIT_INGEST_PAGE_SIZE, 100)
        )
        self.assertTrue(self.processor._elit_ingest_page_complete(40, 100))
        self.assertTrue(self.processor._elit_ingest_page_complete(0, 100))

    def test_ingest_two_pages_then_ready(self):
        page0 = [self._api_product("SKU%03d" % i) for i in range(100)]
        page1 = [self._api_product("SKU1%02d" % i) for i in range(40)]
        pages = {
            0: {"resultado": page0, "cotizacion": 1200.0, "paginador": {"total": 100}},
            100: {"resultado": page1, "cotizacion": 1200.0, "paginador": {"total": 100}},
        }
        run = self.Run.create({"state": "ingest", "offset": 0})
        with self._patch_fetch(pages):
            first = run.action_ingest_one_page()
            self.assertFalse(first.get("done"))
            self.assertEqual(run.state, "ingest")
            self.assertEqual(run.offset, 100)
            self.assertEqual(run.line_count, 100)
            second = run.action_ingest_one_page()
        self.assertTrue(second.get("done"))
        self.assertEqual(run.state, "ready")
        self.assertEqual(run.line_count, 140)

    def test_ingest_http_error_does_not_advance_offset(self):
        run = self.Run.create({"state": "ingest", "offset": 0})
        with self._patch_fetch({0: None}):
            result = run.action_ingest_one_page()
        self.assertFalse(result.get("done"))
        self.assertEqual(result.get("errors"), 1)
        self.assertEqual(run.offset, 0)
        self.assertEqual(run.state, "ingest")

    def test_ingest_empty_first_page_failed_does_not_zero_stock(self):
        product = self._create_elit_product("KEEPSTOCK", stock_elit=77.0)
        run = self.Run.create({"state": "ingest", "offset": 0})
        with self._patch_fetch({0: {"resultado": [], "cotizacion": 1.0}}):
            result = run.action_ingest_one_page()
        self.assertTrue(result.get("failed"))
        self.assertEqual(run.state, "failed")
        self.assertAlmostEqual(product.stock_elit, 77.0, places=2)
        self.assertTrue(product.active)

    def test_incomplete_ingest_does_not_zero_stock(self):
        product = self._create_elit_product("STILLTHERE", stock_elit=12.0)
        page0 = [self._api_product("OTHER%03d" % i) for i in range(100)]
        run = self.Run.create({"state": "ingest", "offset": 0})
        with self._patch_fetch(
            {0: {"resultado": page0, "cotizacion": 1.0, "paginador": {"total": 100}}}
        ):
            run.action_ingest_one_page()
        self.assertEqual(run.state, "ingest")
        self.assertAlmostEqual(product.stock_elit, 12.0, places=2)

    def test_apply_updates_existing_and_imports_new(self):
        existing = self._create_elit_product("EXIST01", stock_elit=1.0)
        run = self.Run.create({"state": "ready", "cotizacion": 1.0})
        self.env["elit.catalog.line"].create(
            [
                {
                    "run_id": run.id,
                    "codigo": "EXIST01",
                    "payload": json.dumps(
                        self._api_product("EXIST01", stock=44, precio=9.5)
                    ),
                },
                {
                    "run_id": run.id,
                    "codigo": "NEW01",
                    "payload": json.dumps(
                        self._api_product("NEW01", stock=8, nombre="New SKU")
                    ),
                },
            ]
        )

        def fake_import(_self, products, cotizacion, skip_existing=False, **kwargs):
            for prod in products:
                codigo = prod.get("codigo_alfa")
                self.Template.create(
                    {
                        "name": prod.get("nombre") or codigo,
                        "detailed_type": "product",
                        "default_code": codigo,
                        "elit_product_code": codigo,
                        "is_elit_product": True,
                        "stock_elit": float(prod.get("stock_total") or 0),
                    }
                )
            return {
                "processed": len(products),
                "errors": 0,
                "seen_codes": set(),
            }

        with patch.object(
            type(self.processor),
            "_import_api_products",
            fake_import,
        ):
            result = run.action_apply_one_batch(batch_size=80)
        self.assertTrue(result.get("done"))
        self.assertEqual(run.state, "done")
        self.assertAlmostEqual(existing.stock_elit, 44.0, places=2)
        created = self.Template.search([("elit_product_code", "=", "NEW01")], limit=1)
        self.assertTrue(created)
        self.assertAlmostEqual(created.stock_elit, 8.0, places=2)

    def test_apply_unarchives_existing_elit_product(self):
        """Archived templates must be revived, not raise MissingError."""
        product = self._create_elit_product("ARCH01", stock_elit=3.0)
        product.active = False
        run = self.Run.create({"state": "ready", "cotizacion": 1.0})
        self.env["elit.catalog.line"].create(
            {
                "run_id": run.id,
                "codigo": "ARCH01",
                "payload": json.dumps(self._api_product("ARCH01", stock=9)),
            }
        )
        result = run.action_apply_one_batch(batch_size=80)
        self.assertTrue(result.get("done"))
        product = product.with_context(active_test=False)
        self.assertTrue(product.active)
        self.assertAlmostEqual(product.stock_elit, 9.0, places=2)
        self.assertEqual(run.state, "done")

    def test_complete_snapshot_zeroes_missing_stock_and_recalcs_cost(self):
        kept = self._create_elit_product("KEPT01", stock_elit=15.0)
        extra = {}
        if "gn_item_id" in self.Template._fields:
            extra["gn_item_id"] = 424242
        if "stock_gn" in self.Template._fields:
            extra["stock_gn"] = 20.0
        missing = self._create_elit_product(
            "MISS01",
            stock_elit=88.0,
            extra=extra or None,
        )
        run = self.Run.create({"state": "ready", "cotizacion": 1.0})
        self.env["elit.catalog.line"].create(
            {
                "run_id": run.id,
                "codigo": "KEPT01",
                "payload": json.dumps(self._api_product("KEPT01", stock=15)),
            }
        )
        cost_calls = []

        def fake_cost(_self, products):
            cost_calls.append(products.ids)

        with patch.object(
            type(self.Template),
            "_elit_update_cost_all_companies",
            fake_cost,
        ):
            run.action_apply_one_batch(batch_size=80)
        self.assertEqual(run.state, "done")
        self.assertAlmostEqual(kept.stock_elit, 15.0, places=2)
        self.assertAlmostEqual(missing.stock_elit, 0.0, places=2)
        self.assertTrue(missing.active)
        if "is_gruponucleo_product" in self.Template._fields:
            self.assertTrue(missing.is_gruponucleo_product)
        if "stock_gn" in self.Template._fields:
            self.assertAlmostEqual(missing.stock_gn, 20.0, places=2)
        flattened = [pid for call in cost_calls for pid in call]
        self.assertIn(missing.id, flattened)

    def test_trigger_does_not_reset_in_progress_catalog_run(self):
        run = self.Run.create({"state": "ingest", "offset": 500})
        resumed = self.processor._action_request_elit_catalog_sync()
        self.assertEqual(resumed.id, run.id)
        self.assertEqual(run.offset, 500)
        self.assertEqual(run.state, "ingest")
        self.assertEqual(self.Run.search_count([("state", "=", "ingest")]), 1)

    def test_zombie_run_is_failed_and_new_cycle_starts(self):
        old_start = fields.Datetime.now() - timedelta(
            hours=ELIT_SYNC_MAX_CYCLE_HOURS + 1
        )
        zombie = self.Run.create(
            {
                "state": "ingest",
                "offset": 300,
                "started_at": old_start,
            }
        )
        new_run = self.processor._action_request_elit_catalog_sync()
        self.assertEqual(zombie.state, "failed")
        self.assertNotEqual(new_run.id, zombie.id)
        self.assertEqual(new_run.state, "ingest")
        self.assertEqual(new_run.offset, 0)
