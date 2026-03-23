# models/sync_processor.py
import requests
import base64
import logging
import json
from odoo import models, api, fields
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

ELIT_PRICE_STOCK_OFFSET_KEY = "elit.update_stock_offset"
ELIT_PRICE_STOCK_REQUESTED_DATE_KEY = "elit.price_stock_sync_requested_date"
ELIT_PRICE_STOCK_DEACTIVATE_PENDING_KEY = "elit.price_stock_sync_deactivate_pending"
CRON_ELIT_PRICE_STOCK_BATCH_XML_ID = "elit_product_integration.cron_elit_price_stock_batch"

ELIT_NEW_PRODUCTS_OFFSET_KEY = "elit.new_products_sync_offset"
ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY = "elit.new_products_sync_requested_date"
ELIT_NEW_PRODUCTS_DEACTIVATE_PENDING_KEY = "elit.new_products_sync_deactivate_pending"
CRON_ELIT_NEW_PRODUCTS_BATCH_XML_ID = "elit_product_integration.cron_elit_new_products_batch"


class ElitSyncProcessor(models.AbstractModel):
    _name = "elit.sync.processor"
    _description = "ELIT Product Synchronization Processor"

    @api.model
    def sync_products(self, sync_type="incremental", date_from=None, offset_start=None):
        """Synchronize products from ELIT API.

        :param sync_type: 'full' or 'incremental'
        :param date_from: Optional start date for incremental sync
        :param offset_start: Optional starting offset to resume interrupted sync
        """
        get_param = self.env["ir.config_parameter"].sudo().get_param
        set_param = self.env["ir.config_parameter"].sudo().set_param

        offset = offset_start or 1
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
            updated, batch_seen, api_count = result
            total_processed += updated
            seen_codes |= batch_seen

            if api_count < limit:
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
        set_param(f"elit.{sync_type}_offset", "1")
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

        params = {"limit": limit, "offset": offset}
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
                sync_type, offset, e,
            )
            return None

        cotizacion = float(data.get("cotizacion") or 1.0)
        products = data.get("resultado", [])

        _logger.info("Page received: %s products", len(products))

        if not products:
            return 0, set(), 0

        first_product_logged = False
        processed = 0
        errors = 0
        seen_codes = set()
        products_to_update_cost = self.env["product.template"]
        commit_interval = 20

        for prod in products:
            codigo = (
                prod.get("codigo_alfa") or prod.get("codigo_producto") or str(prod.get("id", ""))
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
                tmpl = self._process_single_product(
                    prod, partner, usd, ars, routes, cotizacion, ctx
                )
                if tmpl:
                    products_to_update_cost |= tmpl
                processed += 1
                seen_codes.add(codigo)
                if processed % commit_interval == 0:
                    if products_to_update_cost:
                        products_to_update_cost._update_cost_from_replenishment_cost()
                        products_to_update_cost = self.env["product.template"]
                    self.env.cr.commit()
                    _logger.debug("Committed %s products so far (offset %s)", processed, offset)
            except Exception as e:
                errors += 1
                _logger.error(
                    "Error processing product %s (offset %s): %s",
                    codigo,
                    offset,
                    str(e),
                    exc_info=True,
                )
                self.env.cr.rollback()
                continue

        if products_to_update_cost:
            products_to_update_cost._update_cost_from_replenishment_cost()
        if processed > 0 and processed % commit_interval != 0:
            self.env.cr.commit()

        _logger.info(
            "Batch processed: %s products, %s errors (%s, offset=%s)",
            processed,
            errors,
            sync_type,
            offset,
        )

        return processed, seen_codes, len(products)

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
        get_param = self.env["ir.config_parameter"].sudo().get_param

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
        base_cost_usd = (
            precio_costo_with_tax
            if moneda == 2
            else (
                precio_costo_with_tax / cotizacion
                if cotizacion
                else precio_costo_with_tax
            )
        )

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

        # Dimensions → volume
        dims = prod.get("dimensiones", {})
        largo = float(dims.get("largo") or 0.0)
        ancho = float(dims.get("ancho") or 0.0)
        alto = float(dims.get("alto") or 0.0)
        volume = (
            (largo * ancho * alto) / 1_000_000.0
            if largo > 0 and ancho > 0 and alto > 0
            else 0.0
        )

        peso_cubico = float(prod.get("peso_cubico") or 0.0)
        warranty_text = (prod.get("garantia") or "").strip() or "Sin garantía"
        description = prod.get("descripcion") or False

        # Taxes (cached)
        iva_rate = float(prod.get("iva") or 21.0)
        if iva_rate not in ctx["tax_cache"]:
            sale_tax = self.env["account.tax"].search(
                [
                    ("type_tax_use", "=", "sale"),
                    ("amount_type", "=", "percent"),
                    ("amount", "=", iva_rate),
                ],
                limit=1,
            )
            purchase_tax = self.env["account.tax"].search(
                [
                    ("type_tax_use", "=", "purchase"),
                    ("amount_type", "=", "percent"),
                    ("amount", "=", iva_rate),
                ],
                limit=1,
            )
            ctx["tax_cache"][iva_rate] = (sale_tax, purchase_tax)
        sale_tax, purchase_tax = ctx["tax_cache"][iva_rate]
        taxes_ids = [(6, 0, sale_tax.ids)] if sale_tax else []
        supplier_taxes_ids = [(6, 0, purchase_tax.ids)] if purchase_tax else []

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

        vals = {
            "name": prod.get("nombre") or f"ELIT Product {codigo}",
            "detailed_type": "product",
            "elit_product_code": codigo,
            "barcode": barcode,
            "categ_id": categ.id
            or self.env.ref(
                "product.product_category_all", raise_if_not_found=False
            ).id,
            "weight": float(prod.get("peso") or 0.0),
            "elit_volumetric_weight": peso_cubico,
            "elit_warranty_months": warranty_text,
            "description_sale": description,
            "volume": volume,
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
        }

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

        result_tmpl = None
        try:
            if supplierinfo:
                supplierinfo.product_tmpl_id.write(vals)
                supplierinfo.write(
                    {
                        "price": precio_costo_with_tax,
                        "currency_id": usd.id
                        if moneda == 2
                        else (ars.id or self.env.company.currency_id.id),
                    }
                )
                result_tmpl = supplierinfo.product_tmpl_id
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
    # Trigger + batch + cleanup pattern (mirrors grupo_nucleo_integration)
    # ------------------------------------------------------------------

    @api.model
    def _action_request_elit_price_stock_sync(self):
        """Called by trigger cron (e.g. every 6h).

        Sets sync-requested date to today, resets offset to 1, and activates
        the batch cron so it runs every few minutes until the full ELIT
        catalog is processed.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        today_str = fields.Date.today().isoformat()
        ICP.set_param(ELIT_PRICE_STOCK_REQUESTED_DATE_KEY, today_str)
        ICP.set_param(ELIT_PRICE_STOCK_OFFSET_KEY, "1")
        try:
            self.env.ref(CRON_ELIT_PRICE_STOCK_BATCH_XML_ID).sudo().write(
                {"active": True}
            )
            _logger.info(
                "ELIT price/stock sync: trigger set requested_date=%s, batch cron activated.",
                today_str,
            )
        except Exception as e:
            _logger.warning(
                "ELIT price/stock sync: could not activate batch cron: %s", e
            )

    @api.model
    def _cron_elit_price_stock_batch(self):
        """Called by ir.cron every few minutes while price/stock sync is active.

        Processes ONE API page (~100 products) per execution.  When all pages
        are done, clears the requested-date flag and sets deactivate-pending
        so the cleanup cron can deactivate this batch cron (avoids row-lock).
        """
        ICP = self.env["ir.config_parameter"].sudo()
        requested = (
            ICP.get_param(ELIT_PRICE_STOCK_REQUESTED_DATE_KEY) or ""
        ).strip()
        today_str = fields.Date.today().isoformat()
        if requested != today_str:
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

        if changed:
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Trigger + batch for NEW products (mirrors price/stock pattern)
    # ------------------------------------------------------------------

    @api.model
    def _action_request_elit_new_products_sync(self):
        """Called by trigger cron (e.g. every 24h).

        Sets sync-requested date to today, resets offset to 1, and activates
        the batch cron so it imports new ELIT products page by page.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        today_str = fields.Date.today().isoformat()
        ICP.set_param(ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY, today_str)
        ICP.set_param(ELIT_NEW_PRODUCTS_OFFSET_KEY, "1")
        try:
            self.env.ref(CRON_ELIT_NEW_PRODUCTS_BATCH_XML_ID).sudo().write(
                {"active": True}
            )
            _logger.info(
                "ELIT new products sync: trigger set requested_date=%s, "
                "batch cron activated.",
                today_str,
            )
        except Exception as e:
            _logger.warning(
                "ELIT new products sync: could not activate batch cron: %s", e,
            )

    @api.model
    def _cron_elit_new_products_batch(self):
        """Called by ir.cron every few minutes while new-products sync is active.

        Processes ONE API page (~100 products) per execution.  Only creates
        products whose ``elit_product_code`` does not yet exist in Odoo.
        When all pages are done, signals the cleanup cron to deactivate this
        batch cron.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        requested = (
            ICP.get_param(ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY) or ""
        ).strip()
        if requested != fields.Date.today().isoformat():
            return

        offset = int(ICP.get_param(ELIT_NEW_PRODUCTS_OFFSET_KEY, "1") or "1")
        limit = 100

        existing_codes = set(
            self.env["product.template"]
            .search([
                ("is_elit_product", "=", True),
                ("elit_product_code", "!=", False),
            ])
            .mapped("elit_product_code")
        )

        result = self._run_sync_batch(
            "full", offset, limit,
            skip_existing=True, existing_codes=existing_codes,
        )

        if result is None:
            _logger.warning(
                "ELIT new products batch: API error at offset %d, will retry.",
                offset,
            )
            return

        processed, _seen_codes, api_count = result

        if api_count < limit:
            ICP.set_param(ELIT_NEW_PRODUCTS_OFFSET_KEY, "1")
            ICP.set_param(ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY, "")
            ICP.set_param(ELIT_NEW_PRODUCTS_DEACTIVATE_PENDING_KEY, "1")
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

        offset = 1
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
            updated, batch_seen, api_count = result
            total_new += updated
            existing_codes |= batch_seen
            if api_count < limit:
                break
            offset += limit

        _logger.info(
            "Sync NEW products completed: %d created",
            total_new,
        )
        return {"new": total_new, "skipped": len(existing_codes) - total_new}

    # ------------------------------------------------------------------
    # API health check
    # ------------------------------------------------------------------

    @api.model
    def _cron_check_elit_api_health(self):
        """Lightweight API ping: fetch 1 product to verify the API is alive.

        Updates ICP status flags and sends a Discuss notification to the
        configured user when the status transitions to error.
        """
        ICP = self.env["ir.config_parameter"].sudo()
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
                params={"limit": 1, "offset": 1},
                json={"user_id": int(user_id_str), "token": token},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
        except Exception as e:
            self._elit_health_set_error(
                ICP, now_str, str(e)[:500], previous_status,
            )
            return

        ICP.set_param("elit.api_status", "ok")
        ICP.set_param("elit.api_last_check", now_str)
        ICP.set_param("elit.api_last_error", "")
        _logger.info("ELIT API health check: OK")

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
