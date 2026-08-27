# Copyright 2026 Be OnlyOne
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import MissingError

_logger = logging.getLogger(__name__)

ELIT_INGEST_PAGE_SIZE = 100
ELIT_APPLY_BATCH_SIZE = 80


class ElitCatalogRun(models.Model):
    _name = "elit.catalog.run"
    _description = "ELIT catalog snapshot run"
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    state = fields.Selection(
        [
            ("ingest", "Ingest"),
            ("ready", "Ready"),
            ("apply", "Apply"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="ingest",
        required=True,
        index=True,
    )
    offset = fields.Integer(default=0)
    cotizacion = fields.Float(default=1.0)
    line_count = fields.Integer(readonly=True)
    line_ids = fields.One2many("elit.catalog.line", "run_id")
    started_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    finished_at = fields.Datetime(readonly=True)
    error_message = fields.Char()

    @api.depends("started_at", "state")
    def _compute_name(self):
        for rec in self:
            when = rec.started_at or fields.Datetime.now()
            rec.name = _("ELIT catalog %s (%s)") % (when, rec.state)

    @api.model
    def _get_active_run(self):
        """Return the in-progress run, if any."""
        return self.search(
            [("state", "in", ("ingest", "ready", "apply"))],
            limit=1,
            order="id desc",
        )

    def _upsert_ingest_lines(self, api_products):
        """Create or update staging lines for one API page. Return inserted count."""
        self.ensure_one()
        Line = self.env["elit.catalog.line"]
        created = 0
        for prod in api_products:
            if not isinstance(prod, dict):
                continue
            codigo = prod.get("codigo_alfa") or prod.get("codigo_producto")
            if not codigo:
                continue
            payload = dict(prod)
            payload["_cotizacion_api"] = self.cotizacion
            vals = {
                "payload": json.dumps(payload, ensure_ascii=False, default=str),
                "state": "pending",
                "error_message": False,
            }
            existing = Line.search(
                [("run_id", "=", self.id), ("codigo", "=", codigo)],
                limit=1,
            )
            if existing:
                existing.write(vals)
            else:
                vals.update({"run_id": self.id, "codigo": codigo})
                Line.create(vals)
                created += 1
        if created:
            self.line_count = (self.line_count or 0) + created
        return created

    def action_ingest_one_page(self):
        """Fetch ONE ELIT API page into staging lines.

        Returns a dict with keys *done*, *page_size*, *errors*. On HTTP error
        *errors* is 1 and offset is not advanced (retry next tick).
        """
        self.ensure_one()
        Processor = self.env["elit.sync.processor"]
        limit = ELIT_INGEST_PAGE_SIZE
        data = Processor._elit_fetch_productos_page(self.offset, limit)
        if data is None:
            return {"done": False, "page_size": 0, "errors": 1}

        api_products = data.get("resultado") or []
        if not isinstance(api_products, list):
            _logger.error("ELIT ingest: 'resultado' is not a list (run %s)", self.id)
            return {"done": False, "page_size": 0, "errors": 1}

        cotizacion = float(data.get("cotizacion") or self.cotizacion or 1.0)
        self.cotizacion = cotizacion
        page_size = len(api_products)

        if self.offset == 0 and page_size <= 0:
            self.write(
                {
                    "state": "failed",
                    "error_message": _(
                        "Empty first API page; snapshot aborted so stock is not zeroed."
                    ),
                    "finished_at": fields.Datetime.now(),
                }
            )
            _logger.warning("ELIT ingest: empty first page, run %s marked failed.", self.id)
            return {"done": True, "page_size": 0, "errors": 1, "failed": True}

        created = self._upsert_ingest_lines(api_products)
        _logger.info(
            "ELIT ingest: run=%s offset=%s page_size=%s new_lines=%s",
            self.id,
            self.offset,
            page_size,
            created,
        )

        if Processor._elit_ingest_page_complete(page_size, limit):
            self.write({"state": "ready", "offset": self.offset})
            return {"done": True, "page_size": page_size, "errors": 0}

        self.offset = self.offset + limit
        return {"done": False, "page_size": page_size, "errors": 0}

    def action_apply_one_batch(self, batch_size=None):
        """Apply one internal batch of pending lines (no API calls)."""
        self.ensure_one()
        if batch_size is None:
            batch_size = ELIT_APPLY_BATCH_SIZE
        if self.state == "ready":
            self.state = "apply"
        if self.state != "apply":
            return {"processed": 0, "errors": 0, "done": self.state == "done"}

        lines = self.env["elit.catalog.line"].search(
            [("run_id", "=", self.id), ("state", "=", "pending")],
            limit=batch_size,
            order="id",
        )
        if not lines:
            self._action_finish_apply()
            return {"processed": 0, "errors": 0, "done": True}

        Processor = self.env["elit.sync.processor"]
        Template = self.env["product.template"]
        cotizacion = float(self.cotizacion or 1.0)
        codes = [line.codigo for line in lines if line.codigo]
        try:
            code_to_product = Processor._elit_map_templates_by_api_codes(codes)
        except MissingError:
            _logger.exception(
                "ELIT apply: bulk map hit a deleted record, mapping per code"
            )
            code_to_product = {}
            for code in codes:
                try:
                    code_to_product.update(
                        Processor._elit_map_templates_by_api_codes([code])
                    )
                except MissingError:
                    _logger.warning(
                        "ELIT apply: skip map for %s (deleted related record)",
                        code,
                    )

        processed = 0
        errors = 0
        products_to_update_cost = Template.browse()
        to_import = []

        for line in lines:
            try:
                prod = json.loads(line.payload or "{}")
            except (TypeError, ValueError) as e:
                line.write(
                    {"state": "error", "error_message": str(e)[:500]}
                )
                errors += 1
                continue
            if not isinstance(prod, dict):
                line.write(
                    {"state": "error", "error_message": "Payload is not an object"}
                )
                errors += 1
                continue
            tmpl = Template._elit_template_for_write(
                code_to_product.get(line.codigo)
            )
            if tmpl:
                try:
                    Template._apply_elit_data_to_product(tmpl, prod, cotizacion)
                    line.write({"state": "done", "product_tmpl_id": tmpl.id})
                    products_to_update_cost |= tmpl
                    processed += 1
                except MissingError:
                    if Template._elit_template_for_write(tmpl):
                        _logger.warning(
                            "ELIT apply: %s kept but a related record was deleted: %s",
                            line.codigo,
                            tmpl.id,
                        )
                        line.write(
                            {
                                "state": "error",
                                "error_message": _(
                                    "A related product record was deleted."
                                ),
                                "product_tmpl_id": tmpl.id,
                            }
                        )
                        errors += 1
                    else:
                        _logger.warning(
                            "ELIT apply: %s template was deleted, recreating",
                            line.codigo,
                        )
                        to_import.append((line, prod))
                except Exception as e:
                    _logger.error(
                        "ELIT apply: error updating %s: %s", line.codigo, e
                    )
                    error_vals = {
                        "state": "error",
                        "error_message": str(e)[:500],
                    }
                    safe_tmpl = Template._elit_template_for_write(tmpl)
                    if safe_tmpl:
                        error_vals["product_tmpl_id"] = safe_tmpl.id
                    line.write(error_vals)
                    errors += 1
            else:
                to_import.append((line, prod))

        if to_import:
            import_stats = Processor._import_api_products(
                [prod for _line, prod in to_import],
                cotizacion,
                skip_existing=False,
                commit_batches=False,
            )
            errors += import_stats.get("errors", 0)
            created_map = dict(import_stats.get("templates_by_code") or {})
            errors_by_code = dict(import_stats.get("errors_by_code") or {})
            try:
                created_map.update(
                    Processor._elit_map_templates_by_api_codes(
                        [line.codigo for line, _prod in to_import]
                    )
                )
            except MissingError:
                _logger.exception(
                    "ELIT apply: map after import hit a deleted record"
                )
            for line, prod in to_import:
                tmpl = Template._elit_template_for_write(created_map.get(line.codigo))
                if not tmpl:
                    alt = prod.get("codigo_producto") or prod.get("codigo_alfa")
                    if alt:
                        tmpl = Template._elit_template_for_write(created_map.get(alt))
                if tmpl:
                    line.write({"state": "done", "product_tmpl_id": tmpl.id})
                    processed += 1
                elif line.state == "pending":
                    detail = errors_by_code.get(line.codigo) or errors_by_code.get(
                        prod.get("codigo_alfa") or ""
                    )
                    line.write(
                        {
                            "state": "error",
                            "error_message": (
                                detail or _("Product was not created.")
                            )[:500],
                        }
                    )
                    errors += 1

        products_to_update_cost = products_to_update_cost.with_context(
            active_test=False
        ).exists()
        for product in products_to_update_cost:
            try:
                Template._elit_update_cost_all_companies(product)
            except MissingError:
                _logger.warning(
                    "ELIT apply: skip cost update for deleted product %s",
                    product.id,
                )

        remaining = self.env["elit.catalog.line"].search_count(
            [("run_id", "=", self.id), ("state", "=", "pending")]
        )
        done = remaining == 0
        if done:
            self._action_finish_apply()
        _logger.info(
            "ELIT apply: run=%s processed=%s errors=%s remaining=%s done=%s",
            self.id,
            processed,
            errors,
            remaining,
            done,
        )
        return {"processed": processed, "errors": errors, "done": done}

    def _action_finish_apply(self):
        """Zero stock for ELIT products absent from this complete snapshot."""
        self.ensure_one()
        self._zero_missing_elit_stock()
        self.write(
            {
                "state": "done",
                "finished_at": fields.Datetime.now(),
            }
        )

    def _zero_missing_elit_stock(self):
        """Set stock_elit=0 on ELIT products not linked to any line of this run.

        Only call after ingest completed (state ready/apply). Does not archive.
        """
        self.ensure_one()
        Template = self.env["product.template"].with_context(active_test=False)
        self.env.cr.execute(
            """
            SELECT DISTINCT product_tmpl_id
              FROM elit_catalog_line
             WHERE run_id = %s
               AND product_tmpl_id IS NOT NULL
            """,
            [self.id],
        )
        raw_ids = [row[0] for row in self.env.cr.fetchall() if row[0]]
        seen_ids = Template.browse(raw_ids).exists().ids
        domain = [("is_elit_product", "=", True)]
        if seen_ids:
            domain.append(("id", "not in", seen_ids))
        missing = self.env["product.template"].search(domain)
        if missing:
            missing.write({"stock_elit": 0.0})
            # Recalc replenishment/standard cost so dual-vendor glue (or
            # main-seller logic) can switch to Grupo Núcleo when it has stock.
            self.env["product.template"]._elit_update_cost_all_companies(missing)
            _logger.info(
                "ELIT apply: set stock_elit=0 for %d products missing from run %s",
                len(missing),
                self.id,
            )
        return len(missing)

    def action_retry_error_lines(self):
        """Reset error lines to pending and reactivate the apply cron."""
        self.ensure_one()
        errors = self.line_ids.filtered(lambda line: line.state == "error")
        if not errors:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("ELIT"),
                    "message": _("There are no error lines to retry."),
                    "type": "warning",
                    "sticky": False,
                },
            }
        errors.write({"state": "pending", "error_message": False})
        if self.state in ("done", "ready", "failed"):
            self.write(
                {
                    "state": "apply",
                    "finished_at": False,
                    "error_message": False,
                }
            )
        self.env["elit.sync.processor"]._elit_ensure_cron_active(
            "elit_product_integration.cron_elit_catalog_apply_batch"
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ELIT"),
                "message": _(
                    "%s error line(s) queued for retry. Apply will run again."
                )
                % len(errors),
                "type": "success",
                "sticky": False,
            },
        }
