# models/sync_processor.py
import requests
import base64
import logging
import json
from datetime import timedelta

from odoo import _, models, api, fields
from odoo.exceptions import MissingError, UserError, ValidationError
from odoo.osv import expression

_logger = logging.getLogger(__name__)

ELIT_PRICE_STOCK_OFFSET_KEY = "elit.update_stock_offset"
ELIT_PRICE_STOCK_REQUESTED_DATE_KEY = "elit.price_stock_sync_requested_date"
ELIT_PRICE_STOCK_DEACTIVATE_PENDING_KEY = "elit.price_stock_sync_deactivate_pending"
CRON_ELIT_PRICE_STOCK_BATCH_XML_ID = "elit_product_integration.cron_elit_price_stock_batch"

ELIT_NEW_PRODUCTS_OFFSET_KEY = "elit.new_products_sync_offset"
ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY = "elit.new_products_sync_requested_date"
ELIT_NEW_PRODUCTS_DEACTIVATE_PENDING_KEY = "elit.new_products_sync_deactivate_pending"
CRON_ELIT_NEW_PRODUCTS_BATCH_XML_ID = "elit_product_integration.cron_elit_new_products_batch"

ELIT_CATALOG_INGEST_DEACTIVATE_PENDING_KEY = "elit.catalog_ingest_deactivate_pending"
ELIT_CATALOG_APPLY_DEACTIVATE_PENDING_KEY = "elit.catalog_apply_deactivate_pending"
CRON_ELIT_CATALOG_INGEST_XML_ID = "elit_product_integration.cron_elit_catalog_ingest_batch"
CRON_ELIT_CATALOG_APPLY_XML_ID = "elit_product_integration.cron_elit_catalog_apply_batch"
ELIT_CATALOG_LAST_DONE_KEY = "elit.catalog_last_done"
ELIT_CATALOG_STALE_HOURS_KEY = "elit.catalog_stale_hours"
ELIT_CATALOG_STALE_HOURS_DEFAULT = 14  # 6h trigger + margin

# ELIT API pagination is 0-based (see paginador.offset in official docs).
ELIT_API_OFFSET_START = 0

# Sync watchdog: last completed cycle timestamps + staleness thresholds (hours).
# Thresholds can be overridden via ICP keys *_stale_hours.
ELIT_NEW_PRODUCTS_LAST_DONE_KEY = "elit.new_products_last_done"
ELIT_PRICE_STOCK_LAST_DONE_KEY = "elit.price_stock_last_done"
ELIT_NEW_PRODUCTS_STALE_HOURS_KEY = "elit.new_products_stale_hours"
ELIT_PRICE_STOCK_STALE_HOURS_KEY = "elit.price_stock_stale_hours"
ELIT_NEW_PRODUCTS_STALE_HOURS_DEFAULT = 30  # daily trigger + margin
ELIT_PRICE_STOCK_STALE_HOURS_DEFAULT = 14  # 6h trigger + margin
ELIT_STALE_NOTIFIED_KEY = "elit.sync_stale_notified"

# Safety: reset a sync cycle stuck in progress for more than this many hours.
ELIT_SYNC_MAX_CYCLE_HOURS = 48


class ElitSyncProcessor(models.AbstractModel):
    _name = "elit.sync.processor"
    _description = "ELIT Product Synchronization Processor"

    @api.model
    def _elit_pagination_done(self, data, offset, limit, page_size):
        """Return True when the current API page is the last one.

        Prefers ``paginador.total`` from the official API response when present;
        falls back to a short page (``page_size < limit``) or an empty page.

        ``total == limit`` on a full page is ambiguous (catalog size vs items
        in this response). In that case keep paging until a short/empty page.
        """
        if page_size <= 0:
            return True
        if page_size < limit:
            return True
        paginador = data.get("paginador") if isinstance(data, dict) else None
        if isinstance(paginador, dict) and paginador.get("total") is not None:
            try:
                total = int(paginador.get("total"))
            except (TypeError, ValueError):
                total = None
            if total is not None and total >= 0:
                if total <= limit:
                    return False
                return (offset + page_size) >= total
        return False

    @api.model
    def _elit_read_api_offset(self, ICP, key, default=None):
        """Read a 0-based API offset from ICP (never negative)."""
        if default is None:
            default = ELIT_API_OFFSET_START
        raw = ICP.get_param(key, str(default))
        try:
            offset = int(raw or default)
        except (TypeError, ValueError):
            offset = default
        return max(0, offset)

    @api.model
    def _elit_list_query_params(self, limit, offset, extra=None):
        """Build query params for POST /productos.

        ELIT rejects ``offset=0`` with HTTP 400. Official examples omit offset
        on the first page; we do the same and only send offset when > 0.
        """
        params = {"limit": int(limit)}
        offset = max(0, int(offset or 0))
        if offset > 0:
            params["offset"] = offset
        if extra:
            params.update(extra)
        return params

    @api.model
    def _elit_ingest_page_complete(self, page_size, limit=100):
        """Return True when an ingest API page is the last one.

        Uses only page length (empty or short page). ``paginador.total`` is
        not trusted: ELIT often returns total equal to the page size.
        """
        return page_size <= 0 or page_size < int(limit)

    @api.model
    def _elit_fetch_productos_page(self, offset, limit=100):
        """POST one /productos page. Return the JSON dict or None on error."""
        ICP = self.env["ir.config_parameter"].sudo()
        get_param = ICP.get_param
        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        api_url = get_param("elit.api_url", "https://clientes.elit.com.ar").rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")
        if "api.elit.com.ar" in api_url:
            api_url = "https://clientes.elit.com.ar"
        if not user_id_str or not token:
            _logger.warning("ELIT fetch page: missing credentials.")
            return None
        try:
            response = requests.post(
                f"{api_url}{endpoint}",
                params=self._elit_list_query_params(limit, offset),
                json={"user_id": int(user_id_str), "token": token},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            _logger.error(
                "ELIT fetch page error (offset %d): %s",
                offset,
                self._elit_http_error_detail(e),
            )
            return None
        if not isinstance(data, dict):
            _logger.error(
                "ELIT fetch page: unexpected payload type %s (offset=%s)",
                type(data).__name__,
                offset,
            )
            return None
        shape_error = self._elit_validate_api_payload(data)
        if shape_error:
            _logger.error("ELIT fetch page: %s", shape_error)
            return None
        return data

    @api.model
    def _elit_normalize_vendor_code(self, codigo):
        """Return an alphanumeric uppercase code (hyphens/spaces stripped)."""
        if not codigo:
            return ""
        return "".join(ch for ch in str(codigo).upper() if ch.isalnum())

    @api.model
    def _elit_code_matches(self, stored, api_codigo):
        """Return True if a stored Odoo code refers to the same ELIT SKU.

        Matches exact codes, hyphen variants (``910-005795`` vs ``910005795``)
        and internal prefixes (``LOGMOU910005795`` vs ``910-005795``).
        """
        stored_n = self._elit_normalize_vendor_code(stored)
        api_n = self._elit_normalize_vendor_code(api_codigo)
        if not stored_n or not api_n:
            return False
        if stored_n == api_n:
            return True
        if len(api_n) < 6 or not stored_n.endswith(api_n):
            return False
        prefix = stored_n[: -len(api_n)]
        return bool(prefix) and not prefix[-1].isdigit()

    @api.model
    def _elit_map_templates_by_api_codes(self, api_codes):
        """Return ``{api_codigo: product.template}`` for codes on one API page.

        Looks up ``elit_product_code``, ``default_code`` and ELIT
        ``supplierinfo.product_code``, including hyphen-stripped and prefixed
        internal SKUs used by some resellers.
        """
        Template = self.env["product.template"].with_context(active_test=False)
        codes = [c for c in api_codes if c]
        if not codes:
            return {}
        processor = self
        norms = {c: processor._elit_normalize_vendor_code(c) for c in codes}
        exact_alts = list({c for c in codes} | {n for n in norms.values() if n})
        templates = Template.search(
            [
                "|",
                "|",
                ("elit_product_code", "in", exact_alts),
                ("default_code", "in", exact_alts),
                ("seller_ids.product_code", "in", exact_alts),
            ]
        )
        mapped = {}
        used_ids = set()

        def _assign(codigo, tmpl):
            if not tmpl or codigo in mapped or tmpl.id in used_ids:
                return
            mapped[codigo] = tmpl
            used_ids.add(tmpl.id)

        def _sorted_candidates(recordset):
            recordset = recordset.with_context(
                active_test=False, prefetch_fields=False
            )
            try:
                recordset = recordset.exists()
            except MissingError:
                alive_ids = []
                for rec in recordset.browse(recordset.ids):
                    try:
                        if rec.exists():
                            alive_ids.append(rec.id)
                    except MissingError:
                        continue
                recordset = recordset.browse(alive_ids)
            return recordset.sorted(
                key=lambda t: (not t.active, not t.is_elit_product, t.id)
            )

        by_elit = {}
        by_default = {}
        by_seller = {}
        for tmpl in _sorted_candidates(templates):
            try:
                tmpl = tmpl.with_context(prefetch_fields=False)
                if tmpl.elit_product_code and tmpl.elit_product_code not in by_elit:
                    by_elit[tmpl.elit_product_code] = tmpl
                    elit_n = processor._elit_normalize_vendor_code(tmpl.elit_product_code)
                    if elit_n and elit_n not in by_elit:
                        by_elit[elit_n] = tmpl
                if tmpl.default_code and tmpl.default_code not in by_default:
                    by_default[tmpl.default_code] = tmpl
                    def_n = processor._elit_normalize_vendor_code(tmpl.default_code)
                    if def_n and def_n not in by_default:
                        by_default[def_n] = tmpl
                for seller in tmpl.seller_ids:
                    if seller.product_code and seller.product_code not in by_seller:
                        by_seller[seller.product_code] = tmpl
                        sell_n = processor._elit_normalize_vendor_code(
                            seller.product_code
                        )
                        if sell_n and sell_n not in by_seller:
                            by_seller[sell_n] = tmpl
            except MissingError:
                _logger.warning(
                    "ELIT map: skip template %s (related record was deleted)",
                    tmpl.id if tmpl else "?",
                )
                continue

        for codigo in codes:
            for pool in (by_elit, by_default, by_seller):
                tmpl = pool.get(codigo) or pool.get(norms[codigo])
                if tmpl:
                    _assign(codigo, tmpl)
                    break

        unmatched = [c for c in codes if c not in mapped]
        suffix_norms = [norms[c] for c in unmatched if len(norms[c]) >= 6]
        if suffix_norms:
            like_domains = []
            for norm in suffix_norms:
                like_domains.append(
                    [
                        "|",
                        "|",
                        ("elit_product_code", "=like", "%" + norm),
                        ("default_code", "=like", "%" + norm),
                        ("seller_ids.product_code", "=like", "%" + norm),
                    ]
                )
            extra = Template.search(expression.OR(like_domains))
            for codigo in unmatched:
                for tmpl in _sorted_candidates(extra):
                    if tmpl.id in used_ids:
                        continue
                    try:
                        tmpl = tmpl.with_context(prefetch_fields=False)
                        seller_codes = tmpl.seller_ids.mapped("product_code")
                        if (
                            processor._elit_code_matches(tmpl.elit_product_code, codigo)
                            or processor._elit_code_matches(tmpl.default_code, codigo)
                            or any(
                                processor._elit_code_matches(code, codigo)
                                for code in seller_codes
                            )
                        ):
                            _assign(codigo, tmpl)
                            break
                    except MissingError:
                        _logger.warning(
                            "ELIT map: skip template %s while matching %s",
                            tmpl.id if tmpl else "?",
                            codigo,
                        )
                        continue
        return mapped

    @api.model
    def _elit_ensure_cron_active(self, xml_id):
        """Activate a cron by xml id when it exists and is inactive."""
        try:
            cron = self.env.ref(xml_id, raise_if_not_found=False)
        except Exception:
            cron = False
        if cron and not cron.active:
            cron.sudo().write({"active": True})

    @api.model
    def _elit_request_sync_cycle(self, requested_key, offset_key, cron_xml_id, log_label):
        """Start a sync cycle, or resume if one is already in progress.

        Never reset the offset while a cycle is active. The 6h/24h trigger
        would otherwise restart the catalog walk before the last pages run.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        if self._elit_sync_cycle_active(ICP, requested_key, offset_key):
            _logger.info(
                "ELIT %s: cycle already in progress at offset %s, not resetting.",
                log_label,
                ICP.get_param(offset_key),
            )
            self._elit_ensure_cron_active(cron_xml_id)
            return
        now_str = fields.Datetime.to_string(fields.Datetime.now())
        ICP.set_param(requested_key, now_str)
        ICP.set_param(offset_key, str(ELIT_API_OFFSET_START))
        self._elit_ensure_cron_active(cron_xml_id)
        _logger.info(
            "ELIT %s: trigger set cycle start=%s, batch cron activated.",
            log_label,
            now_str,
        )

    @api.model
    def _elit_http_error_detail(self, exc):
        """Extract short response body from a requests HTTPError, if any."""
        resp = getattr(exc, "response", None)
        if resp is None:
            return str(exc)
        body = (resp.text or "")[:500]
        return "%s | body=%s" % (exc, body)

    @api.model
    def sync_products(self, sync_type="incremental", date_from=None, offset_start=None):
        """Synchronize products from ELIT API.

        :param sync_type: 'full' or 'incremental'
        :param date_from: Optional start date for incremental sync
        :param offset_start: Optional starting offset to resume interrupted sync
            (0-based, as required by the ELIT API)
        """
        get_param = self.env["ir.config_parameter"].sudo().get_param
        set_param = self.env["ir.config_parameter"].sudo().set_param

        offset = ELIT_API_OFFSET_START if offset_start is None else max(0, int(offset_start))
        set_param(f"elit.{sync_type}_offset", str(offset))

        if sync_type == "incremental" and not date_from:
            last_sync_str = get_param("elit.last_incremental_sync")
            date_from = (
                fields.Datetime.from_string(last_sync_str) if last_sync_str else None
            )

        _logger.info("Starting %s ELIT sync from offset %s", sync_type, offset)

        limit = 100
        total_processed = 0
        seen_codes = set()

        while True:
            result = self._run_sync_batch(sync_type, offset, limit)
            if result is None:
                _logger.error(
                    "API error during %s sync at offset %d, aborting.",
                    sync_type, offset,
                )
                break
            updated, batch_seen, api_count, done = result
            total_processed += updated
            seen_codes |= batch_seen

            if done:
                _logger.info("Last %s batch: %s products – finalizing", sync_type, updated)
                break

            offset += limit
            set_param(f"elit.{sync_type}_offset", str(offset))
            _logger.info(
                "%s batch completed: %s products – continuing with offset %s",
                sync_type.capitalize(),
                updated,
                offset,
            )

        # Cleanup
        set_param(f"elit.{sync_type}_offset", str(ELIT_API_OFFSET_START))
        if sync_type == "incremental":
            set_param("elit.last_incremental_sync", fields.Datetime.now())
        elif sync_type == "full":
            set_param("elit.last_full_sync", fields.Datetime.now())
            self._archive_deleted_products(seen_codes=seen_codes)

        _logger.info(
            f"{sync_type.capitalize()} sync ELIT completed: {total_processed} products processed"
        )

        return total_processed

    @api.model
    def _archive_deleted_products(self, seen_codes=None):
        """Archive ELIT products not in API (elit_product_code not in seen_codes). No API calls."""
        if not seen_codes:
            return
        not_seen = self.env["product.template"].search([
            ("is_elit_product", "=", True),
            ("elit_product_code", "not in", list(seen_codes)),
        ])
        if not_seen:
            not_seen.write({"active": False, "stock_elit": 0})
            _logger.info(
                "Archived %d ELIT products not found in API",
                len(not_seen),
            )

    @api.model
    def _run_sync_batch(
        self, sync_type, offset, limit, skip_existing=False, existing_codes=None
    ):
        """Core batch logic – reusable for full/incremental."""
        get_param = self.env["ir.config_parameter"].sudo().get_param

        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        api_url = get_param("elit.api_url", "https://clientes.elit.com.ar").rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")
        if "api.elit.com.ar" in api_url:
            api_url = "https://clientes.elit.com.ar"
            _logger.warning("URL corrected automatically to clientes.elit.com.ar")

        if not user_id_str or not token:
            raise UserError(
                "Missing ELIT credentials in Settings > Technical > System Parameters"
            )

        user_id = int(user_id_str)
        headers = {"Content-Type": "application/json"}
        payload = {"user_id": user_id, "token": token}

        usd = self.env.ref("base.USD")
        ars = self.env["res.currency"].search([("name", "=", "ARS")], limit=1)
        routes = self.env.ref("purchase_stock.route_warehouse0_buy") + self.env.ref(
            "stock.route_warehouse0_mto"
        )

        partner_id = int(get_param("elit.partner_id") or 0) or False
        if partner_id:
            partner = self.env["res.partner"].browse(partner_id)
            if not partner.exists():
                partner = self.env["res.partner"].search(
                    [("name", "ilike", "ELIT")], limit=1
                )
        else:
            partner = self.env["res.partner"].search(
                [("name", "ilike", "ELIT")], limit=1
            )
        if not partner:
            partner = self.env["res.partner"].create(
                {"name": "ELIT", "supplier_rank": 1, "is_company": True}
            )
            _logger.info("ELIT partner created automatically")

        # Per-batch caches to avoid N+1 lookups
        supplierinfo_map = {
            si.product_code: si
            for si in self.env["product.supplierinfo"].search(
                [("partner_id", "=", partner.id)]
            )
            if si.product_code
        }
        parent_categ_id = int(get_param("elit.public_categ_parent_id") or 0) or False
        root_categ = (
            self.env["product.public.category"].browse(parent_categ_id)
            if parent_categ_id
            else None
        )
        if not root_categ or not root_categ.exists():
            root_categ = self._get_or_create_public_categ("Computación")
        ctx = {
            "tax_cache": {},
            "categ_cache": {},
            "public_categ_cache": {},
            "supplierinfo_map": supplierinfo_map,
            "root_categ": root_categ,
        }

        params = self._elit_list_query_params(limit, offset)
        if sync_type == "incremental":
            last_sync_str = get_param("elit.last_incremental_sync")
            last_sync = (
                fields.Datetime.from_string(last_sync_str) if last_sync_str else None
            )
            if last_sync:
                params["actualizacion"] = last_sync.strftime("%Y-%m-%d %H:%M")

        full_url = f"{api_url}{endpoint}"
        _logger.info(
            "ELIT request → %s",
            full_url + "?" + "&".join(f"{k}={v}" for k, v in params.items()),
        )

        try:
            response = requests.post(
                full_url, params=params, json=payload, headers=headers, timeout=30
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            _logger.error(
                "Error in _run_sync_batch (%s, offset=%s): %s",
                sync_type, offset, self._elit_http_error_detail(e),
            )
            return None

        if not isinstance(data, dict):
            _logger.error(
                "ELIT _run_sync_batch: unexpected payload type %s (offset=%s)",
                type(data).__name__,
                offset,
            )
            return None

        cotizacion = float(data.get("cotizacion") or 1.0)
        products = data.get("resultado", [])
        if products is None:
            products = []
        if not isinstance(products, list):
            _logger.error(
                "ELIT _run_sync_batch: 'resultado' is not a list (offset=%s)",
                offset,
            )
            return None

        _logger.info("Page received: %s products", len(products))

        if not products:
            return 0, set(), 0, True

        # Resolve existing codes for THIS page only when caller did not pass a set
        page_existing = existing_codes
        if skip_existing and page_existing is None:
            page_codes = []
            for prod in products:
                added = False
                for key in ("codigo_alfa", "codigo_producto"):
                    code = prod.get(key)
                    if not code:
                        continue
                    added = True
                    if code not in page_codes:
                        page_codes.append(code)
                if not added and prod.get("id") is not None:
                    page_codes.append(str(prod.get("id")))
            mapped = self._elit_map_templates_by_api_codes(page_codes)
            page_existing = set(mapped.keys())

        stats = self._import_api_products(
            products,
            cotizacion,
            skip_existing=skip_existing,
            existing_codes=page_existing,
            partner=partner,
            usd=usd,
            ars=ars,
            routes=routes,
            ctx=ctx,
        )
        done = self._elit_pagination_done(data, offset, limit, len(products))
        return stats["processed"], stats["seen_codes"], len(products), done


    @api.model
    def _prepare_batch_context(self):
        """Build partner, currencies, routes and per-batch caches for imports.

        :return: tuple (partner, usd, ars, routes, ctx) or None if credentials missing
        """
        get_param = self.env["ir.config_parameter"].sudo().get_param
        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        if not user_id_str or not token:
            return None

        usd = self.env.ref("base.USD")
        ars = self.env["res.currency"].search([("name", "=", "ARS")], limit=1)
        routes = self.env.ref("purchase_stock.route_warehouse0_buy") + self.env.ref(
            "stock.route_warehouse0_mto"
        )

        partner_id = int(get_param("elit.partner_id") or 0) or False
        if partner_id:
            partner = self.env["res.partner"].browse(partner_id)
            if not partner.exists():
                partner = self.env["res.partner"].search(
                    [("name", "ilike", "ELIT")], limit=1
                )
        else:
            partner = self.env["res.partner"].search(
                [("name", "ilike", "ELIT")], limit=1
            )
        if not partner:
            partner = self.env["res.partner"].create(
                {"name": "ELIT", "supplier_rank": 1, "is_company": True}
            )
            _logger.info("ELIT partner created automatically")

        supplierinfo_map = {
            si.product_code: si
            for si in self.env["product.supplierinfo"].search(
                [("partner_id", "=", partner.id)]
            )
            if si.product_code
        }
        parent_categ_id = int(get_param("elit.public_categ_parent_id") or 0) or False
        root_categ = (
            self.env["product.public.category"].browse(parent_categ_id)
            if parent_categ_id
            else None
        )
        if not root_categ or not root_categ.exists():
            root_categ = self._get_or_create_public_categ("Computación")
        ctx = {
            "tax_cache": {},
            "categ_cache": {},
            "public_categ_cache": {},
            "supplierinfo_map": supplierinfo_map,
            "root_categ": root_categ,
        }
        return partner, usd, ars, routes, ctx

    @api.model
    def _import_api_products(
        self,
        products,
        cotizacion,
        skip_existing=False,
        existing_codes=None,
        partner=None,
        usd=None,
        ars=None,
        routes=None,
        ctx=None,
        commit_batches=True,
    ):
        """Import/update product.template records from an already-fetched API page.

        :param commit_batches: if False, do not commit/rollback the cursor
            (catalog apply runs inside a larger transaction).
        :return: dict with processed, errors, seen_codes
        """
        if partner is None or usd is None or routes is None or ctx is None:
            prepared = self._prepare_batch_context()
            if not prepared:
                _logger.warning("ELIT _import_api_products: missing credentials.")
                return {"processed": 0, "errors": 0, "seen_codes": set()}
            partner, usd, ars, routes, ctx = prepared

        first_product_logged = False
        processed = 0
        errors = 0
        seen_codes = set()
        products_to_update_cost = self.env["product.template"]
        commit_interval = 20

        for prod in products:
            codigo = (
                prod.get("codigo_alfa")
                or prod.get("codigo_producto")
                or str(prod.get("id", ""))
            )
            if not codigo:
                continue
            if skip_existing and existing_codes is not None and codigo in existing_codes:
                continue
            if not first_product_logged:
                _logger.info(
                    "=== EXAMPLE FULL ELIT PRODUCT ===\n%s",
                    json.dumps(prod, indent=2, ensure_ascii=False),
                )
                first_product_logged = True
            try:
                with self.env.cr.savepoint():
                    tmpl = self._process_single_product(
                        prod, partner, usd, ars, routes, cotizacion, ctx
                    )
                if tmpl:
                    products_to_update_cost |= tmpl.with_context(
                        active_test=False
                    ).exists()
                processed += 1
                seen_codes.add(codigo)
                if commit_batches and processed % commit_interval == 0:
                    if products_to_update_cost:
                        self.env["product.template"]._elit_update_cost_all_companies(
                            products_to_update_cost
                        )
                        products_to_update_cost = self.env["product.template"]
                    self.env.cr.commit()
                    _logger.debug("Committed %s products so far", processed)
            except Exception as e:
                errors += 1
                _logger.error(
                    "Error processing product %s: %s",
                    codigo,
                    str(e),
                    exc_info=True,
                )
                if commit_batches:
                    self.env.cr.rollback()
                continue

        products_to_update_cost = products_to_update_cost.with_context(
            active_test=False
        ).exists()
        if products_to_update_cost:
            self.env["product.template"]._elit_update_cost_all_companies(
                products_to_update_cost
            )
        if commit_batches and processed > 0 and processed % commit_interval != 0:
            self.env.cr.commit()

        _logger.info(
            "ELIT import page: %s products, %s errors",
            processed,
            errors,
        )
        return {
            "processed": processed,
            "errors": errors,
            "seen_codes": seen_codes,
        }

    @api.model
    def _elit_tax_ids_for_rate(self, iva_rate, type_tax_use):
        """Return account.tax ids for the rate, one per target company.

        Multi-company: searches taxes in the configured company (Settings) or in
        all companies when none is configured, and returns one match per company.
        """
        amount = round(float(iva_rate), 2)
        company_ids = self.env["product.template"]._elit_get_target_companies().ids
        # sudo() on account.tax: search across companies in cron context;
        # safe as it only reads standard tax records by amount/type.
        taxes = self.env["account.tax"].sudo().search(
            [
                ("type_tax_use", "=", type_tax_use),
                ("amount_type", "=", "percent"),
                ("amount", "=", amount),
                ("company_id", "in", company_ids),
            ]
        )
        tax_ids = []
        seen_companies = set()
        for tax in taxes:
            if tax.company_id.id in seen_companies:
                continue
            seen_companies.add(tax.company_id.id)
            tax_ids.append(tax.id)
        return tax_ids

    def _get_or_create_public_categ(self, name, parent_id=False):
        """Create or return an ecommerce public category."""
        domain = [("name", "=", name)]
        if parent_id:
            domain += [("parent_id", "=", parent_id)]
        else:
            domain += [("parent_id", "=", False)]
        categ = self.env["product.public.category"].search(domain, limit=1)
        if not categ:
            categ = self.env["product.public.category"].create(
                {"name": name, "parent_id": parent_id}
            )
            _logger.info("Created ecommerce category: %s", name)
        return categ

    def _process_single_product(
        self, prod, partner, usd, ars, routes, cotizacion, ctx=None
    ):
        """Process a single product from ELIT API data. Returns product.template for batch cost update."""
        if ctx is None:
            ctx = {
                "tax_cache": {},
                "categ_cache": {},
                "public_categ_cache": {},
                "supplierinfo_map": {},
                "root_categ": self._get_or_create_public_categ("Computación"),
            }

        codigo = (
            prod.get("codigo_alfa") or prod.get("codigo_producto") or str(prod["id"])
        )
        precio_costo = float(prod.get("precio") or 0.0)
        impuesto_interno = float(prod.get("impuesto_interno") or 0.0)

        if impuesto_interno > 0:
            precio_costo_with_tax = precio_costo * (1 + impuesto_interno / 100)
            _logger.info(
                "Applying internal tax %s%% to product %s: adjusted price from %s to %s",
                impuesto_interno,
                codigo,
                precio_costo,
                precio_costo_with_tax,
            )
        else:
            precio_costo_with_tax = precio_costo

        moneda = prod.get("moneda", 1)

        # Internal categories (cached)
        categ_key = (prod.get("categoria") or "", prod.get("sub_categoria") or "")
        if categ_key not in ctx["categ_cache"]:
            categ = self.env["product.category"]
            if prod.get("categoria"):
                parent = self.env["product.category"].search(
                    [("name", "=", prod["categoria"])], limit=1
                )
                if not parent:
                    parent = self.env["product.category"].create(
                        {"name": prod["categoria"]}
                    )
                if prod.get("sub_categoria"):
                    categ = self.env["product.category"].search(
                        [
                            ("name", "=", prod["sub_categoria"]),
                            ("parent_id", "=", parent.id),
                        ],
                        limit=1,
                    )
                    if not categ:
                        categ = self.env["product.category"].create(
                            {
                                "name": prod["sub_categoria"],
                                "parent_id": parent.id,
                            }
                        )
                else:
                    categ = parent
            ctx["categ_cache"][categ_key] = categ
        categ = ctx["categ_cache"][categ_key]

        # Barcode
        ean_raw = prod.get("ean")
        barcode = False
        if ean_raw is not None:
            ean_str = str(ean_raw).strip()
            if ean_str and ean_str != "0" and len(ean_str) >= 8:
                barcode = ean_str

        # Dimensions → volume (+ Zippin size fields when available)
        dim_vals = self.env["product.template"]._elit_dimension_write_vals(prod)
        warranty_text = (prod.get("garantia") or "").strip() or "Sin garantía"
        description = prod.get("descripcion") or False

        # Taxes (cached): one tax per target company (multi-company support;
        # the m2m holds taxes of several companies, each company sees its own)
        iva_rate = float(prod.get("iva") or 21.0)
        if iva_rate not in ctx["tax_cache"]:
            sale_tax_ids = self._elit_tax_ids_for_rate(iva_rate, "sale")
            purchase_tax_ids = self._elit_tax_ids_for_rate(iva_rate, "purchase")
            ctx["tax_cache"][iva_rate] = (sale_tax_ids, purchase_tax_ids)
        sale_tax_ids, purchase_tax_ids = ctx["tax_cache"][iva_rate]
        taxes_ids = [(6, 0, sale_tax_ids)] if sale_tax_ids else []
        supplier_taxes_ids = [(6, 0, purchase_tax_ids)] if purchase_tax_ids else []

        # Internal tax (already handled above with adjustment to precio_costo)
        if impuesto_interno > 0:
            _logger.warning(
                "Product %s has internal tax %s%% – applied to cost",
                codigo,
                impuesto_interno,
            )

        # Image: store URL only; cron sync_elit_images_batch downloads in batch
        image_url_elit = None
        if prod.get("imagenes") and prod["imagenes"]:
            image_url_elit = prod["imagenes"][0] if isinstance(prod["imagenes"][0], str) else None

        # Include page-level cotización in the dump: it is needed to audit prices
        raw_payload = dict(prod)
        raw_payload["_cotizacion_api"] = cotizacion
        vals = {
            "name": prod.get("nombre") or f"ELIT Product {codigo}",
            "detailed_type": "product",
            "elit_product_code": codigo,
            "elit_raw_data": self.env["product.template"]._elit_dump_raw_data(raw_payload),
            "barcode": barcode,
            "categ_id": categ.id
            or self.env.ref(
                "product.product_category_all", raise_if_not_found=False
            ).id,
            "weight": float(prod.get("peso") or 0.0),
            "elit_warranty_months": warranty_text,
            "description_sale": description,
            "elit_brand": prod.get("marca"),
            "is_gamer": bool(prod.get("gamer")),
            "stock_elit": float(prod.get("stock_total") or 0.0),
            "is_elit_product": True,
            "route_ids": [(6, 0, routes.ids)],
            "list_price": float(prod.get("pvp_ars") or 0.0)
            or (float(prod.get("pvp_usd") or 0.0) * cotizacion),
            "taxes_id": taxes_ids,
            "supplier_taxes_id": supplier_taxes_ids,
            "elit_image_url": image_url_elit,
            "allow_out_of_stock_order": True,
        }
        vals.update(dim_vals)

        # Public categories (cached)
        root_categ = ctx.get("root_categ")
        if not root_categ:
            root_categ = self._get_or_create_public_categ("Computación")
        if prod.get("categoria"):
            key_main = ("public", prod["categoria"], root_categ.id)
            if key_main not in ctx["public_categ_cache"]:
                ctx["public_categ_cache"][key_main] = self._get_or_create_public_categ(
                    prod["categoria"], root_categ.id
                )
            main_public_categ = ctx["public_categ_cache"][key_main]
        else:
            main_public_categ = root_categ
        sub_public_categ = None
        if prod.get("sub_categoria"):
            key_sub = ("public", prod["sub_categoria"], main_public_categ.id)
            if key_sub not in ctx["public_categ_cache"]:
                ctx["public_categ_cache"][key_sub] = self._get_or_create_public_categ(
                    prod["sub_categoria"], main_public_categ.id
                )
            sub_public_categ = ctx["public_categ_cache"][key_sub]
        public_categ_ids = [main_public_categ.id]
        if sub_public_categ:
            public_categ_ids.append(sub_public_categ.id)
        vals["public_categ_ids"] = [(6, 0, public_categ_ids)]

        supplierinfo = ctx.get("supplierinfo_map", {}).get(codigo)
        if not supplierinfo:
            supplierinfo = self.env["product.supplierinfo"].search(
                [("partner_id", "=", partner.id), ("product_code", "=", codigo)],
                limit=1,
            )

        # company_id explicit (configured or False=shared) for multi-company visibility
        elit_company_id = self.env["product.template"]._elit_get_company_id()
        result_tmpl = None
        Template = self.env["product.template"]
        try:
            if supplierinfo:
                tmpl = Template._elit_template_from_supplierinfo(supplierinfo)
                if tmpl:
                    if not tmpl.active:
                        vals = dict(vals, active=True)
                    tmpl.write(vals)
                    supplierinfo.write(
                        {
                            "price": precio_costo_with_tax,
                            "currency_id": usd.id
                            if moneda == 2
                            else (ars.id or self.env.company.currency_id.id),
                            "company_id": elit_company_id,
                        }
                    )
                    result_tmpl = tmpl
                else:
                    tmpl = self.env["product.template"].create(vals)
                    supplierinfo.write(
                        {
                            "product_tmpl_id": tmpl.id,
                            "price": precio_costo_with_tax,
                            "currency_id": usd.id
                            if moneda == 2
                            else (ars.id or self.env.company.currency_id.id),
                            "company_id": elit_company_id,
                        }
                    )
                    result_tmpl = tmpl
            else:
                tmpl = self.env["product.template"].create(vals)
                self.env["product.supplierinfo"].create(
                    {
                        "partner_id": partner.id,
                        "product_tmpl_id": tmpl.id,
                        "product_code": codigo,
                        "product_name": prod.get("nombre"),
                        "price": precio_costo_with_tax,
                        "currency_id": usd.id
                        if moneda == 2
                        else (ars.id or self.env.company.currency_id.id),
                        "delay": 3,
                        "company_id": elit_company_id,
                    }
                )
                result_tmpl = tmpl
        except ValidationError as e:
            if (
                "Códigos de barras ya asignados" in str(e)
                or "barcode" in str(e).lower()
            ):
                existing_product = self.env["product.product"].search(
                    [("barcode", "=", barcode)], limit=1
                )
                if existing_product:
                    existing_product.product_tmpl_id.write(vals)
                    si = self.env["product.supplierinfo"].search(
                        [
                            ("partner_id", "=", partner.id),
                            (
                                "product_tmpl_id",
                                "=",
                                existing_product.product_tmpl_id.id,
                            ),
                        ],
                        limit=1,
                    )
                    if si:
                        si.write(
                            {
                                "price": precio_costo_with_tax,  # Adjusted
                                "product_code": codigo,
                                "company_id": elit_company_id,
                            }
                        )
                    else:
                        self.env["product.supplierinfo"].create(
                            {
                                "partner_id": partner.id,
                                "product_tmpl_id": existing_product.product_tmpl_id.id,
                                "product_code": codigo,
                                "product_name": prod.get("nombre"),
                                "price": precio_costo_with_tax,  # Adjusted
                                "currency_id": usd.id
                                if moneda == 2
                                else (ars.id or self.env.company.currency_id.id),
                                "delay": 3,
                                "company_id": elit_company_id,
                            }
                        )
                    result_tmpl = existing_product.product_tmpl_id
                    _logger.info(
                        "Duplicate barcode – product updated: %s (EAN: %s)",
                        codigo,
                        barcode,
                    )
                else:
                    _logger.error(
                        "Duplicate barcode but no existing product found: %s",
                        barcode,
                    )
            else:
                raise
        except Exception as e:
            _logger.error("Unexpected error processing product %s: %s", codigo, e)
            raise
        return result_tmpl

    @api.model
    def sync_elit_images_batch(self, batch_size=20):
        """Download images for ELIT products that have elit_image_url but no image_1920.
        Runs in small batches with commit to avoid CPU timeout.
        """
        domain = [
            ("is_elit_product", "=", True),
            ("elit_image_url", "!=", False),
            ("elit_image_url", "!=", ""),
            ("image_1920", "=", False),
        ]
        products = self.env["product.template"].search(domain, limit=batch_size)
        if not products:
            return {"downloaded": 0}
        downloaded = 0
        for product in products:
            url = (product.elit_image_url or "").strip()
            if not url:
                continue
            try:
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200 and resp.content:
                    product.write({
                        "image_1920": base64.b64encode(resp.content),
                    })
                    downloaded += 1
            except Exception as e:
                _logger.debug("ELIT image download failed %s: %s", product.elit_product_code or product.default_code, e)
        if downloaded:
            self.env.cr.commit()
        _logger.info("ELIT images batch: %d downloaded", downloaded)
        return {"downloaded": downloaded}

    @api.model
    def update_all_elit_stock_and_cost(self):
        """LEGACY: full loop that processes ALL ELIT API pages in one run.

        Kept for manual use (server action). The scheduled sync now uses
        the trigger + batch pattern (_action_request_elit_price_stock_sync).
        """
        _logger.info("Starting ELIT stock and cost batch update (full loop)")

        stats = self.env["product.template"].update_stock_batch(
            domain=[("is_elit_product", "=", True)],
            commit_interval=100,
        )

        _logger.info(
            "ELIT stock update completed: %d updated, %d not found, %d errors, %d API calls",
            stats.get("updated", 0),
            stats.get("not_found", 0),
            stats.get("errors", 0),
            stats.get("total_api_calls", 0),
        )

        return stats

    # ------------------------------------------------------------------
    # Catalog staging: ingest (1 API page) then apply (internal batch)
    # ------------------------------------------------------------------

    @api.model
    def _action_request_elit_catalog_sync(self):
        """Start or resume a unified catalog cycle (ingest then apply).

        Does not reset an in-progress run. Activates the ingest or apply
        batch cron according to the run state.
        """
        Run = self.env["elit.catalog.run"]
        run = Run._get_active_run()
        if run and run.started_at:
            age = fields.Datetime.now() - run.started_at
            if age > timedelta(hours=ELIT_SYNC_MAX_CYCLE_HOURS):
                run.write(
                    {
                        "state": "failed",
                        "error_message": _(
                            "Cycle exceeded %s hours; snapshot aborted."
                        )
                        % ELIT_SYNC_MAX_CYCLE_HOURS,
                        "finished_at": fields.Datetime.now(),
                    }
                )
                _logger.warning(
                    "ELIT catalog: run %s exceeded %sh, marked failed.",
                    run.id,
                    ELIT_SYNC_MAX_CYCLE_HOURS,
                )
                run = Run.browse()
        if run:
            _logger.info(
                "ELIT catalog: cycle already in progress run=%s state=%s offset=%s",
                run.id,
                run.state,
                run.offset,
            )
            if run.state == "ingest":
                self._elit_ensure_cron_active(CRON_ELIT_CATALOG_INGEST_XML_ID)
            else:
                self._elit_ensure_cron_active(CRON_ELIT_CATALOG_APPLY_XML_ID)
            return run
        run = Run.create({"state": "ingest", "offset": ELIT_API_OFFSET_START})
        self._elit_ensure_cron_active(CRON_ELIT_CATALOG_INGEST_XML_ID)
        _logger.info("ELIT catalog: started ingest run=%s", run.id)
        return run

    @api.model
    def _cron_elit_catalog_ingest_batch(self):
        """One API page into staging. Activate apply when ingest is complete."""
        Run = self.env["elit.catalog.run"]
        run = Run.search([("state", "=", "ingest")], limit=1, order="id desc")
        if not run:
            return
        ICP = self.env["ir.config_parameter"].sudo()
        result = run.action_ingest_one_page()
        self.env.cr.commit()
        if result.get("failed"):
            ICP.set_param(ELIT_CATALOG_INGEST_DEACTIVATE_PENDING_KEY, "1")
            self.env.cr.commit()
            return
        if result.get("done"):
            ICP.set_param(ELIT_CATALOG_INGEST_DEACTIVATE_PENDING_KEY, "1")
            self._elit_ensure_cron_active(CRON_ELIT_CATALOG_APPLY_XML_ID)
            self.env.cr.commit()
            _logger.info(
                "ELIT catalog ingest complete run=%s; apply batch activated.",
                run.id,
            )

    @api.model
    def _cron_elit_catalog_apply_batch(self):
        """Apply one internal staging batch (no API). Zero missing stock at end."""
        Run = self.env["elit.catalog.run"]
        run = Run.search(
            [("state", "in", ("ready", "apply"))],
            limit=1,
            order="id desc",
        )
        if not run:
            return
        ICP = self.env["ir.config_parameter"].sudo()
        result = run.action_apply_one_batch()
        self.env.cr.commit()
        if result.get("done"):
            ICP.set_param(ELIT_CATALOG_APPLY_DEACTIVATE_PENDING_KEY, "1")
            ICP.set_param(
                ELIT_CATALOG_LAST_DONE_KEY,
                fields.Datetime.to_string(fields.Datetime.now()),
            )
            self.env.cr.commit()
            _logger.info("ELIT catalog apply complete run=%s.", run.id)

    @api.model
    def _action_request_elit_price_stock_sync(self):
        """Backward-compatible alias: unified catalog cycle."""
        self._action_request_elit_catalog_sync()

    @api.model
    def _action_request_elit_new_products_sync(self):
        """Backward-compatible alias: unified catalog cycle."""
        self._action_request_elit_catalog_sync()

    # ------------------------------------------------------------------
    # Trigger + batch + cleanup pattern (legacy batch methods kept inactive)
    # ------------------------------------------------------------------


    @api.model
    def _elit_sync_cycle_active(self, ICP, requested_key, offset_key, default_offset=None):
        """Return True while a sync cycle is in progress.

        The trigger cron stores the cycle start datetime in ``requested_key``;
        the batch cron keeps running while it is set, even across midnight
        (the old ``requested == today`` check silently froze cycles at 00:00).
        Safety net: cycles in progress for more than ELIT_SYNC_MAX_CYCLE_HOURS
        are reset (flag + offset) to avoid zombie cycles.
        """
        if default_offset is None:
            default_offset = str(ELIT_API_OFFSET_START)
        requested = (ICP.get_param(requested_key) or "").strip()
        if not requested:
            return False
        try:
            # Handles both datetime strings and legacy date-only values
            start_dt = fields.Datetime.from_string(requested)
        except ValueError:
            _logger.warning(
                "ELIT sync: invalid cycle start %r in %s, resetting flag.",
                requested,
                requested_key,
            )
            ICP.set_param(requested_key, "")
            return False
        if fields.Datetime.now() - start_dt > timedelta(hours=ELIT_SYNC_MAX_CYCLE_HOURS):
            _logger.warning(
                "ELIT sync: cycle started %s exceeds %dh, resetting flag and offset (%s).",
                requested,
                ELIT_SYNC_MAX_CYCLE_HOURS,
                requested_key,
            )
            ICP.set_param(requested_key, "")
            ICP.set_param(offset_key, default_offset)
            return False
        return True

    @api.model
    def _cron_elit_price_stock_batch(self):
        """Called by ir.cron every few minutes while price/stock sync is active
        (in-progress flag set by the trigger; survives midnight).

        Processes ONE API page (~100 products) per execution.  When all pages
        are done, clears the in-progress flag and sets deactivate-pending
        so the cleanup cron can deactivate this batch cron (avoids row-lock).
        """
        ICP = self.env["ir.config_parameter"].sudo()
        if not self._elit_sync_cycle_active(
            ICP, ELIT_PRICE_STOCK_REQUESTED_DATE_KEY, ELIT_PRICE_STOCK_OFFSET_KEY
        ):
            return

        result = self.env["product.template"]._elit_fetch_and_apply_one_page()

        if result is None:
            _logger.warning(
                "ELIT price/stock batch: skipped (credentials missing or API error).",
            )
            return

        if result.get("done"):
            ICP.set_param(ELIT_PRICE_STOCK_REQUESTED_DATE_KEY, "")
            ICP.set_param(ELIT_PRICE_STOCK_DEACTIVATE_PENDING_KEY, "1")
            # Watchdog: record cycle completion timestamp
            ICP.set_param(
                ELIT_PRICE_STOCK_LAST_DONE_KEY,
                fields.Datetime.to_string(fields.Datetime.now()),
            )
            self.env.cr.commit()
            _logger.info(
                "ELIT price/stock sync: complete. "
                "Deactivation requested; cleanup cron will deactivate shortly.",
            )

    @api.model
    def _deactivate_cron_if_found(self, xml_id, code_fallback):
        """Deactivate a cron by xml_id; fall back to search by code string."""
        cron = None
        try:
            cron = self.env.ref(xml_id, raise_if_not_found=False)
        except Exception:
            pass
        if not cron:
            cron = (
                self.env["ir.cron"]
                .sudo()
                .search(
                    [
                        ("code", "=", code_fallback),
                        ("model_id.model", "=", "elit.sync.processor"),
                    ],
                    limit=1,
                )
            )
        if cron:
            cron.sudo().write({"active": False})
            _logger.info(
                "ELIT: batch cron id=%s (%s) deactivated.", cron.id, xml_id,
            )
        else:
            _logger.warning(
                "ELIT: deactivate pending but cron not found (xml_id=%s).", xml_id,
            )

    @api.model
    def _cron_elit_deactivate_batch_if_pending(self):
        """Called by cleanup cron every ~10 min.

        Checks all pending-deactivation flags and deactivates the
        corresponding batch crons.  A separate cron is required because
        Odoo locks the ir.cron row during execution, so a batch cron
        cannot deactivate itself.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        changed = False

        if (ICP.get_param(ELIT_PRICE_STOCK_DEACTIVATE_PENDING_KEY) or "").strip() == "1":
            self._deactivate_cron_if_found(
                CRON_ELIT_PRICE_STOCK_BATCH_XML_ID,
                "model._cron_elit_price_stock_batch()",
            )
            ICP.set_param(ELIT_PRICE_STOCK_DEACTIVATE_PENDING_KEY, "")
            changed = True

        if (ICP.get_param(ELIT_NEW_PRODUCTS_DEACTIVATE_PENDING_KEY) or "").strip() == "1":
            self._deactivate_cron_if_found(
                CRON_ELIT_NEW_PRODUCTS_BATCH_XML_ID,
                "model._cron_elit_new_products_batch()",
            )
            ICP.set_param(ELIT_NEW_PRODUCTS_DEACTIVATE_PENDING_KEY, "")
            changed = True

        if (ICP.get_param(ELIT_CATALOG_INGEST_DEACTIVATE_PENDING_KEY) or "").strip() == "1":
            self._deactivate_cron_if_found(
                CRON_ELIT_CATALOG_INGEST_XML_ID,
                "model._cron_elit_catalog_ingest_batch()",
            )
            ICP.set_param(ELIT_CATALOG_INGEST_DEACTIVATE_PENDING_KEY, "")
            changed = True

        if (ICP.get_param(ELIT_CATALOG_APPLY_DEACTIVATE_PENDING_KEY) or "").strip() == "1":
            self._deactivate_cron_if_found(
                CRON_ELIT_CATALOG_APPLY_XML_ID,
                "model._cron_elit_catalog_apply_batch()",
            )
            ICP.set_param(ELIT_CATALOG_APPLY_DEACTIVATE_PENDING_KEY, "")
            changed = True

        if changed:
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Trigger + batch for NEW products (mirrors price/stock pattern)
    # ------------------------------------------------------------------

    @api.model
    def _cron_elit_new_products_batch(self):
        """Called by ir.cron every few minutes while new-products sync is active
        (in-progress flag set by the trigger; survives midnight).

        Processes ONE API page (~100 products) per execution.  Only creates
        products whose ``elit_product_code`` does not yet exist in Odoo.
        When all pages are done, signals the cleanup cron to deactivate this
        batch cron.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        if not self._elit_sync_cycle_active(
            ICP, ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY, ELIT_NEW_PRODUCTS_OFFSET_KEY
        ):
            return

        offset = self._elit_read_api_offset(ICP, ELIT_NEW_PRODUCTS_OFFSET_KEY)
        limit = 100

        # existing_codes=None: resolve only codes present on the current API page
        result = self._run_sync_batch(
            "full", offset, limit,
            skip_existing=True, existing_codes=None,
        )

        if result is None:
            _logger.warning(
                "ELIT new products batch: API error at offset %d, will retry.",
                offset,
            )
            return

        processed, _seen_codes, api_count, done = result

        if done:
            ICP.set_param(ELIT_NEW_PRODUCTS_OFFSET_KEY, str(ELIT_API_OFFSET_START))
            ICP.set_param(ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY, "")
            ICP.set_param(ELIT_NEW_PRODUCTS_DEACTIVATE_PENDING_KEY, "1")
            ICP.set_param(
                ELIT_NEW_PRODUCTS_LAST_DONE_KEY,
                fields.Datetime.to_string(fields.Datetime.now()),
            )
            self.env.cr.commit()
            _logger.info(
                "ELIT new products sync: complete (%d created this page). "
                "Deactivation requested.",
                processed,
            )
        else:
            new_offset = offset + limit
            ICP.set_param(ELIT_NEW_PRODUCTS_OFFSET_KEY, str(new_offset))
            self.env.cr.commit()
            _logger.info(
                "ELIT new products batch: offset %d → %d (%d created).",
                offset, new_offset, processed,
            )

    @api.model
    def sync_new_products_only(self):
        """Synchronize only NEW products from ELIT API in one run.

        WARNING: This method loops over ALL API pages in a single execution.
        Use only for manual server actions or wizard calls, NEVER from a cron.
        The scheduled sync uses the trigger + batch pattern instead
        (_action_request_elit_new_products_sync).
        """
        _logger.info("Starting sync of NEW products only from ELIT (manual)")
        existing_codes = set(
            self.env["product.template"]
            .search([
                ("is_elit_product", "=", True),
                ("elit_product_code", "!=", False),
            ])
            .mapped("elit_product_code")
        )
        _logger.info("Found %d existing ELIT products in Odoo", len(existing_codes))

        offset = ELIT_API_OFFSET_START
        limit = 100
        total_new = 0

        while True:
            result = self._run_sync_batch(
                "full",
                offset,
                limit,
                skip_existing=True,
                existing_codes=existing_codes,
            )
            if result is None:
                _logger.error(
                    "API error during new products sync at offset %d, aborting.",
                    offset,
                )
                break
            updated, batch_seen, api_count, done = result
            total_new += updated
            existing_codes |= batch_seen
            if done:
                break
            offset += limit

        _logger.info(
            "Sync NEW products completed: %d created",
            total_new,
        )
        return {"new": total_new, "skipped": len(existing_codes) - total_new}

    # ------------------------------------------------------------------
    # API health check + sync watchdog
    # ------------------------------------------------------------------

    @api.model
    def _elit_get_stale_syncs(self, ICP=None):
        """Return list of (label, last_done_str) for sync types past their staleness threshold.

        A sync type is considered stale only after it completed at least once
        (no false alarms on fresh installs).
        """
        if ICP is None:
            ICP = self.env["ir.config_parameter"].sudo()
        now = fields.Datetime.now()
        checks = [
            (
                _("Catálogo ELIT"),
                ELIT_CATALOG_LAST_DONE_KEY,
                ELIT_CATALOG_STALE_HOURS_KEY,
                ELIT_CATALOG_STALE_HOURS_DEFAULT,
            ),
        ]
        stale = []
        for label, last_key, hours_key, hours_default in checks:
            last_str = (ICP.get_param(last_key) or "").strip()
            if not last_str:
                continue
            try:
                last_dt = fields.Datetime.from_string(last_str)
            except ValueError:
                continue
            try:
                max_hours = float(ICP.get_param(hours_key) or hours_default)
            except (TypeError, ValueError):
                max_hours = hours_default
            if now - last_dt > timedelta(hours=max_hours):
                stale.append((label, last_str))
        return stale

    @api.model
    def _elit_check_sync_staleness(self, ICP):
        """Sync watchdog: alert when a sync cycle is past its staleness threshold.

        Notifies the configured user via Discuss only on transition to stale
        (flag in ICP avoids hourly spam); the flag clears itself when syncs are
        fresh again so a future stall re-notifies.
        """
        stale = self._elit_get_stale_syncs(ICP)
        notified = (ICP.get_param(ELIT_STALE_NOTIFIED_KEY) or "").strip()
        if not stale:
            if notified:
                ICP.set_param(ELIT_STALE_NOTIFIED_KEY, "")
            return
        stale_labels = ", ".join(label for label, _last in stale)
        _logger.warning(
            "ELIT sync watchdog: stale syncs detected: %s",
            "; ".join("%s (última: %s)" % (label, last) for label, last in stale),
        )
        if notified == stale_labels:
            return
        ICP.set_param(ELIT_STALE_NOTIFIED_KEY, stale_labels)
        notify_uid = int(ICP.get_param("elit.api_notify_user_id") or 0)
        if not notify_uid:
            return
        user = self.env["res.users"].sudo().browse(notify_uid)
        if not user.exists() or not user.partner_id:
            return
        details = "<br/>".join(
            _("- %(sync)s: última completada %(last)s", sync=label, last=last)
            for label, last in stale
        )
        self.env["mail.thread"].message_notify(
            partner_ids=user.partner_id.ids,
            body=_(
                "<b>ELIT: sincronización vencida</b><br/>"
                "Las siguientes sincronizaciones no se completaron dentro del "
                "umbral esperado:<br/>%s",
                details,
            ),
            subject=_("ELIT: sincronización vencida"),
        )

    @api.model
    def _cron_check_elit_api_health(self):
        """Lightweight API ping: fetch 1 product to verify the API is alive.

        Updates ICP status flags and sends a Discuss notification to the
        configured user when the status transitions to error. Also acts as
        sync watchdog: alerts when a sync cycle has not completed within its
        staleness threshold (detects silently stalled syncs).
        """
        ICP = self.env["ir.config_parameter"].sudo()
        # Watchdog first: must run even when the API check below returns early.
        self._elit_check_sync_staleness(ICP)
        get_param = ICP.get_param
        previous_status = get_param("elit.api_status", "unknown")

        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        api_url = get_param("elit.api_url", "https://clientes.elit.com.ar").rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")

        now_str = fields.Datetime.to_string(fields.Datetime.now())

        if not user_id_str or not token:
            self._elit_health_set_error(
                ICP, now_str, "Credenciales ELIT no configuradas.", previous_status,
            )
            return

        try:
            response = requests.post(
                f"{api_url}{endpoint}",
                params={"limit": 1},
                json={"user_id": int(user_id_str), "token": token},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            self._elit_health_set_error(
                ICP, now_str, str(e)[:500], previous_status,
            )
            return

        # Validate response shape (API docs are unreliable; catch silent breakage)
        shape_error = self._elit_validate_api_payload(data)
        if shape_error:
            self._elit_health_set_error(ICP, now_str, shape_error, previous_status)
            return

        ICP.set_param("elit.api_status", "ok")
        ICP.set_param("elit.api_last_check", now_str)
        ICP.set_param("elit.api_last_error", "")
        _logger.info("ELIT API health check: OK")

    @api.model
    def _elit_validate_api_payload(self, data):
        """Return an error message if the API payload shape looks wrong, else False."""
        if not isinstance(data, dict):
            return _(
                "Respuesta ELIT inválida: se esperaba un objeto JSON, se recibió %s."
            ) % type(data).__name__
        if "resultado" not in data:
            return _("Respuesta ELIT inválida: falta la clave 'resultado'.")
        resultado = data.get("resultado")
        if resultado is not None and not isinstance(resultado, list):
            return _(
                "Respuesta ELIT inválida: 'resultado' debe ser una lista, se recibió %s."
            ) % type(resultado).__name__
        # codigo / paginador are documented; treat missing cotizacion as soft
        # (some filtered queries may omit it) but require paginador OR resultado.
        if "paginador" in data and not isinstance(data.get("paginador"), dict):
            return _("Respuesta ELIT inválida: 'paginador' debe ser un objeto.")
        if "cotizacion" in data:
            try:
                float(data.get("cotizacion") or 0.0)
            except (TypeError, ValueError):
                return _("Respuesta ELIT inválida: 'cotizacion' no es numérica.")
        return False

    @api.model
    def _elit_health_set_error(self, ICP, now_str, error_msg, previous_status):
        """Record error status and notify the configured user on first failure."""
        ICP.set_param("elit.api_status", "error")
        ICP.set_param("elit.api_last_check", now_str)
        ICP.set_param("elit.api_last_error", error_msg)
        _logger.warning("ELIT API health check FAILED: %s", error_msg)

        if previous_status == "error":
            return

        notify_uid = int(ICP.get_param("elit.api_notify_user_id") or 0)
        if not notify_uid:
            return
        user = self.env["res.users"].sudo().browse(notify_uid)
        if not user.exists() or not user.partner_id:
            return
        self.env["mail.thread"].message_notify(
            partner_ids=user.partner_id.ids,
            body=f"<b>ELIT API: Error de conexión</b><br/>{error_msg}",
            subject="ELIT API: Error de conexión",
        )
