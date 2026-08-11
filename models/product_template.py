import base64
import json
import requests
import logging
from odoo import _, fields, models, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    elit_brand = fields.Char(string="ELIT Brand")
    is_gamer = fields.Boolean(string="Gamer Product")
    stock_elit = fields.Float(string="Stock ELIT", readonly=True)
    is_elit_product = fields.Boolean(
        string="Es producto ELIT",
        help="Indica si el producto viene de ELIT y debe chequear stock_elit",
    )

    # Nuevos campos de la API
    elit_volumetric_weight = fields.Float(string="Peso Volumétrico ELIT (kg)")
    elit_warranty_months = fields.Char(
        string="Garantía ELIT", help="Garantía tal como viene de ELIT (ej. 24 MESES)"
    )
    elit_product_code = fields.Char(
        string="Código ELIT",
        index=True,
        copy=False,
        help="Código alfanumérico del producto en ELIT (campo 'codigo_alfa' de la API).",
    )
    elit_last_sync = fields.Datetime(
        string="Última sincronización ELIT",
        readonly=True,
        help="Fecha/hora de la última actualización de stock/precio desde ELIT",
    )
    elit_image_url = fields.Char(
        string="URL imagen ELIT",
        help="URL de imagen desde la API; un cron descarga las imágenes en lote.",
    )
    elit_raw_data = fields.Text(
        string="Raw data API ELIT",
        readonly=True,
        copy=False,
        help="Último JSON crudo recibido de la API ELIT para este producto "
        "(se actualiza en cada sincronización). Útil para auditar precios, "
        "cotización e impuestos.",
    )

    @api.model
    def _elit_dump_raw_data(self, prod):
        """Serialize an API product dict to pretty JSON for elit_raw_data."""
        try:
            return json.dumps(prod, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(prod)

    @api.model
    def _elit_parse_dimensions(self, elit_data):
        """Parse dimension fields from an ELIT API product dict.

        :return: dict with keys length, width, height, volume, peso_cubico
        """
        dims = elit_data.get("dimensiones") or {}
        if not isinstance(dims, dict):
            dims = {}
        length = float(dims.get("largo") or 0.0)
        width = float(dims.get("ancho") or 0.0)
        height = float(dims.get("alto") or 0.0)
        volume = (
            (length * width * height) / 1_000_000.0
            if length > 0 and width > 0 and height > 0
            else 0.0
        )
        return {
            "length": length,
            "width": width,
            "height": height,
            "volume": volume,
            "peso_cubico": float(elit_data.get("peso_cubico") or 0.0),
        }

    @api.model
    def _elit_dimension_write_vals(self, elit_data):
        """Build write vals for volume / volumetric weight / Zippin size fields.

        Zippin fields are only included when present on the model (zippin branch).
        """
        parsed = self._elit_parse_dimensions(elit_data)
        vals = {
            "volume": parsed["volume"],
            "elit_volumetric_weight": parsed["peso_cubico"],
        }
        if "zippin_product_length" in self._fields:
            vals.update(
                {
                    "zippin_product_length": parsed["length"],
                    "zippin_product_width": parsed["width"],
                    "zippin_product_height": parsed["height"],
                }
            )
        return vals

    # ------------------------------------------------------------------
    # Multi-company helpers
    # ------------------------------------------------------------------

    @api.model
    def _elit_get_company_id(self):
        """Return configured company id for ELIT supplier data, or False (shared)."""
        # sudo() for ir.config_parameter: safe, module configuration only.
        raw = (
            self.env["ir.config_parameter"].sudo().get_param("elit.company_id", "")
        ).strip()
        try:
            return int(raw) if raw else False
        except (TypeError, ValueError):
            return False

    @api.model
    def _elit_get_target_companies(self):
        """Return companies for which ELIT data (taxes, costs) must be maintained.

        Configured company in Settings, or all companies when not set (shared).
        """
        company_id = self._elit_get_company_id()
        if company_id:
            company = self.env["res.company"].sudo().browse(company_id)
            if company.exists():
                return company
        return self.env["res.company"].sudo().search([])

    @api.model
    def _elit_update_cost_all_companies(self, products):
        """Run replenishment cost update per company (standard_price is company-dependent)."""
        if not products:
            return
        for company in self._elit_get_target_companies():
            products.with_company(company)._update_cost_from_replenishment_cost()

    def update_single_stock_elit(self):
        """Update stock_elit for selected products - LEGACY method (1 API call per product).

        Use update_stock_batch() for better performance when updating many products.
        This method is kept for manual updates of small selections.
        """
        if len(self) > 10:
            _logger.warning(
                "update_single_stock_elit called with %d products. "
                "Consider using update_stock_batch() for better performance.",
                len(self),
            )

        get_param = self.env["ir.config_parameter"].sudo().get_param
        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        api_url = get_param("elit.api_url", "https://clientes.elit.com.ar").rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")

        if not user_id_str or not token:
            raise UserError("Faltan credenciales ELIT en la configuración.")

        user_id = int(user_id_str)
        payload = {"user_id": user_id, "token": token}
        headers = {"Content-Type": "application/json"}

        updated_products = self.env["product.template"]

        for rec in self:
            elit_code = rec.elit_product_code
            if not elit_code:
                partner = self._get_elit_partner()
                if partner:
                    si = self.env["product.supplierinfo"].search([
                        ("partner_id", "=", partner.id),
                        ("product_tmpl_id", "=", rec.id),
                    ], limit=1)
                    elit_code = si.product_code if si else None
            if not elit_code:
                _logger.warning(
                    "Producto %s sin elit_product_code – saltando",
                    rec.name,
                )
                continue

            params = {"codigo_alfa": elit_code, "limit": 1}

            try:
                response = requests.post(
                    f"{api_url}{endpoint}",
                    params=params,
                    json=payload,
                    headers=headers,
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                _logger.warning(
                    "Error consultando API ELIT para %s: %s",
                    elit_code,
                    e,
                )
                continue

            products = data.get("resultado", [])
            if not products:
                rec.write({"is_elit_product": False})
                _logger.info(
                    "Producto %s no encontrado en ELIT",
                    elit_code,
                )
                continue

            prod = products[0]
            cotizacion = float(data.get("cotizacion") or 1.0)
            self._apply_elit_data_to_product(rec, prod, cotizacion)
            updated_products |= rec

        # Update costs in batch at the end (much faster)
        if updated_products:
            self._elit_update_cost_all_companies(updated_products)
            _logger.info(
                "Stock ELIT actualizado para %d productos",
                len(updated_products),
            )

    @api.model
    def update_stock_batch(self, domain=None, limit=None, offset=0, commit_interval=100):
        """Update stock_elit efficiently using batched API calls.

        This method fetches data from ELIT API in pages (100 products per call)
        and updates matching Odoo products in batch.

        :param domain: Optional domain to filter products (default: is_elit_product=True)
        :param limit: Optional limit of products to process
        :param offset: Starting offset for pagination
        :param commit_interval: How often to commit (default: every 100 products)
        :return: dict with stats {updated, not_found, errors, total_api_calls}
        """
        if domain is None:
            domain = [("is_elit_product", "=", True)]

        get_param = self.env["ir.config_parameter"].sudo().get_param
        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        api_url = get_param("elit.api_url", "https://clientes.elit.com.ar").rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")

        if not user_id_str or not token:
            raise UserError("Faltan credenciales ELIT en la configuración.")

        user_id = int(user_id_str)
        payload = {"user_id": user_id, "token": token}
        headers = {"Content-Type": "application/json"}

        # Get all ELIT products from Odoo
        products = self.search(domain, limit=limit, offset=offset)
        if not products:
            return {"updated": 0, "not_found": 0, "errors": 0, "total_api_calls": 0}

        code_to_product = {p.elit_product_code: p for p in products if p.elit_product_code}
        all_codes = set(code_to_product.keys())

        _logger.info(
            "update_stock_batch: processing %d products with %d unique codes",
            len(products),
            len(all_codes),
        )

        stats = {"updated": 0, "not_found": 0, "errors": 0, "total_api_calls": 0}
        found_codes = set()
        products_to_update_cost = self.env["product.template"]

        ICP = self.env["ir.config_parameter"].sudo()
        api_limit = 100
        api_offset = int(ICP.get_param("elit.update_stock_offset", "1") or "1")
        if api_offset < 1:
            api_offset = 1
        started_from_one = api_offset == 1

        while True:
            params = {"limit": api_limit, "offset": api_offset}

            try:
                response = requests.post(
                    f"{api_url}{endpoint}",
                    params=params,
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
                stats["total_api_calls"] += 1
            except Exception as e:
                _logger.error("Error fetching ELIT API (offset %d): %s", api_offset, e)
                stats["errors"] += 1
                break

            api_products = data.get("resultado", [])
            cotizacion = float(data.get("cotizacion") or 1.0)

            if not api_products:
                _logger.info("No more products from API at offset %d", api_offset)
                ICP.set_param("elit.update_stock_offset", "0")
                break

            _logger.info(
                "API page: offset=%d, received=%d products",
                api_offset,
                len(api_products),
            )

            for prod in api_products:
                codigo = prod.get("codigo_alfa") or prod.get("codigo_producto")
                if not codigo:
                    continue

                if codigo in all_codes:
                    found_codes.add(codigo)
                    odoo_product = code_to_product[codigo]
                    try:
                        self._apply_elit_data_to_product(odoo_product, prod, cotizacion)
                        products_to_update_cost |= odoo_product
                        stats["updated"] += 1
                    except Exception as e:
                        _logger.error("Error updating product %s: %s", codigo, e)
                        stats["errors"] += 1

            if products_to_update_cost:
                self._elit_update_cost_all_companies(products_to_update_cost)
            self.env.cr.commit()

            if len(api_products) < api_limit:
                ICP.set_param("elit.update_stock_offset", "0")
                _logger.info("Stock update complete, offset reset to 0")
                break

            api_offset += api_limit
            ICP.set_param("elit.update_stock_offset", str(api_offset))
            _logger.info("Saved resume offset %d", api_offset)

        # Mark products not found in API only when we completed a full pass from offset 1
        not_found_codes = all_codes - found_codes
        if started_from_one and not_found_codes:
            not_found_products = self.browse([
                code_to_product[code].id for code in not_found_codes
            ])
            not_found_products.write({"is_elit_product": False})
            stats["not_found"] = len(not_found_codes)
            _logger.info(
                "%d products not found in ELIT API, marked as not ELIT",
                len(not_found_codes),
            )

        # Update costs in batch at the end (MUCH faster than per-product)
        if products_to_update_cost:
            _logger.info("Updating accounting cost for %d products...", len(products_to_update_cost))
            self._elit_update_cost_all_companies(products_to_update_cost)

        # Final commit
        self.env.cr.commit()

        _logger.info(
            "update_stock_batch completed: %d updated, %d not found, %d errors, %d API calls",
            stats["updated"],
            stats["not_found"],
            stats["errors"],
            stats["total_api_calls"],
        )

        return stats

    @api.model
    def _elit_fetch_and_apply_one_page(self):
        """Fetch ONE page from ELIT API and apply stock/cost data to matching products.

        Reads ``elit.update_stock_offset`` from ICP; after processing, saves
        the next offset (or resets to 1 when the last page is reached).

        :return: dict with keys *updated*, *errors*, *api_offset*, *page_size*,
                 *done* (bool).  Returns ``None`` when API is not configured.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        get_param = ICP.get_param

        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        api_url = get_param("elit.api_url", "https://clientes.elit.com.ar").rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")

        if not user_id_str or not token:
            _logger.warning("ELIT price/stock batch: missing credentials.")
            return None

        user_id = int(user_id_str)
        payload = {"user_id": user_id, "token": token}
        headers = {"Content-Type": "application/json"}

        api_limit = 100
        api_offset = int(get_param("elit.update_stock_offset", "1") or "1")
        if api_offset < 1:
            api_offset = 1

        try:
            response = requests.post(
                f"{api_url}{endpoint}",
                params={"limit": api_limit, "offset": api_offset},
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            _logger.error("ELIT price/stock batch: API error (offset %d): %s", api_offset, e)
            return {"updated": 0, "errors": 1, "api_offset": api_offset, "page_size": 0, "done": False}

        api_products = data.get("resultado", [])
        cotizacion = float(data.get("cotizacion") or 1.0)
        page_size = len(api_products)

        if not api_products:
            _logger.info("ELIT price/stock batch: no more products at offset %d.", api_offset)
            ICP.set_param("elit.update_stock_offset", "1")
            self.env.cr.commit()
            return {"updated": 0, "errors": 0, "api_offset": api_offset, "page_size": 0, "done": True}

        _logger.info("ELIT price/stock batch: offset=%d, received=%d products.", api_offset, page_size)

        api_codes = [
            prod.get("codigo_alfa") or prod.get("codigo_producto")
            for prod in api_products
        ]
        api_codes = [c for c in api_codes if c]
        if api_codes:
            products = self.search([
                ("elit_product_code", "in", api_codes),
            ])
            code_to_product = {
                p.elit_product_code: p for p in products if p.elit_product_code
            }
        else:
            code_to_product = {}

        updated = 0
        created = 0
        errors = 0
        products_to_update_cost = self.env["product.template"]
        missing_prods = []

        for prod in api_products:
            codigo = prod.get("codigo_alfa") or prod.get("codigo_producto")
            if not codigo:
                continue
            if codigo not in code_to_product:
                missing_prods.append(prod)
                continue
            try:
                self._apply_elit_data_to_product(code_to_product[codigo], prod, cotizacion)
                products_to_update_cost |= code_to_product[codigo]
                updated += 1
            except Exception as e:
                _logger.error("ELIT price/stock batch: error updating %s: %s", codigo, e)
                errors += 1

        # Create products present in the API page but missing in Odoo (unified sync).
        if missing_prods:
            create_stats = self.env["elit.sync.processor"]._import_api_products(
                missing_prods, cotizacion, skip_existing=False
            )
            created = create_stats.get("processed", 0)
            errors += create_stats.get("errors", 0)

        if products_to_update_cost:
            self._elit_update_cost_all_companies(products_to_update_cost)

        done = page_size < api_limit
        if done:
            ICP.set_param("elit.update_stock_offset", "1")
            _logger.info("ELIT price/stock batch: last page reached, offset reset to 1.")
        else:
            new_offset = api_offset + api_limit
            ICP.set_param("elit.update_stock_offset", str(new_offset))
            _logger.info("ELIT price/stock batch: saved resume offset %d.", new_offset)

        self.env.cr.commit()
        _logger.info(
            "ELIT price/stock batch page done: updated=%d created=%d errors=%d "
            "offset=%d page_size=%d done=%s",
            updated, created, errors, api_offset, page_size, done,
        )
        return {
            "updated": updated,
            "created": created,
            "errors": errors,
            "api_offset": api_offset,
            "page_size": page_size,
            "done": done,
        }

    def _get_elit_partner(self):
        """Return the ELIT partner from config or by name. Used for supplierinfo updates."""
        get_param = self.env["ir.config_parameter"].sudo().get_param
        partner_id = int(get_param("elit.partner_id") or 0) or False
        if partner_id:
            partner = self.env["res.partner"].browse(partner_id)
            if partner.exists():
                return partner
        return self.env["res.partner"].search(
            [("name", "ilike", "ELIT")], limit=1
        )

    def _apply_elit_data_to_product(self, product, elit_data, cotizacion):
        """Apply ELIT API data to a single product record.

        Updates stock_elit on the product and the **supplier price** in
        product.supplierinfo for the ELIT partner. The replenishment cost
        is NOT overwritten here: it is derived from supplierinfo (e.g. main
        supplier or lowest price) so that if the product has ELIT and
        another supplier, the lowest price can be used.

        :param product: product.template record
        :param elit_data: dict from ELIT API
        :param cotizacion: USD exchange rate from API
        """
        stock = float(elit_data.get("stock_total") or 0.0)
        precio = float(elit_data.get("precio") or 0.0)
        impuesto_interno = float(elit_data.get("impuesto_interno") or 0.0)
        moneda = elit_data.get("moneda", 1)

        # Apply internal tax if > 0
        if impuesto_interno > 0:
            precio_with_tax = precio * (1 + impuesto_interno / 100)
        else:
            precio_with_tax = precio

        usd = self.env.ref("base.USD")
        ars = self.env["res.currency"].search([("name", "=", "ARS")], limit=1)
        currency_id = usd.id if moneda == 2 else (ars.id if ars else self.env.company.currency_id.id)

        image_url_elit = None
        imagenes = elit_data.get("imagenes")
        if imagenes and isinstance(imagenes, list) and imagenes[0]:
            first = imagenes[0]
            if isinstance(first, str):
                image_url_elit = first

        # Include page-level cotización in the dump: it is needed to audit prices
        raw_payload = dict(elit_data)
        raw_payload["_cotizacion_api"] = cotizacion
        write_vals = {
            "stock_elit": stock,
            "is_elit_product": True,
            "elit_last_sync": fields.Datetime.now(),
            "replenishment_cost_type": "supplier_price",
            "allow_out_of_stock_order": True,
            "elit_raw_data": self._elit_dump_raw_data(raw_payload),
        }
        write_vals.update(self._elit_dimension_write_vals(elit_data))
        if image_url_elit:
            write_vals["elit_image_url"] = image_url_elit
        product.write(write_vals)

        # Update supplier price in product.supplierinfo for ELIT partner.
        # This allows Odoo / product_replenishment_cost to use main supplier
        # or lowest price among ELIT and other suppliers.
        partner = self._get_elit_partner()
        if not partner:
            return

        codigo = product.elit_product_code
        supplierinfo = self.env["product.supplierinfo"].search(
            [
                ("partner_id", "=", partner.id),
                ("product_tmpl_id", "=", product.id),
            ],
            limit=1,
        )
        if not supplierinfo and codigo:
            supplierinfo = self.env["product.supplierinfo"].search(
                [
                    ("partner_id", "=", partner.id),
                    ("product_code", "=", codigo),
                ],
                limit=1,
            )

        # company_id explicit (configured or False=shared) for multi-company visibility
        elit_company_id = self._elit_get_company_id()
        if supplierinfo:
            supplierinfo.write({
                "price": precio_with_tax,
                "currency_id": currency_id,
                "company_id": elit_company_id,
            })
        else:
            # Create supplierinfo for ELIT so the price is available for cost computation
            self.env["product.supplierinfo"].create({
                "partner_id": partner.id,
                "product_tmpl_id": product.id,
                "product_code": codigo or "",
                "product_name": product.name,
                "price": precio_with_tax,
                "currency_id": currency_id,
                "delay": 3,
                "company_id": elit_company_id,
            })

    def _elit_download_image(self, force=False):
        """Download image from elit_image_url into image_1920.

        Skips products that already have an image unless *force* is True.
        """
        for product in self:
            url = (product.elit_image_url or "").strip()
            if not url:
                continue
            if product.image_1920 and not force:
                continue
            try:
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200 and resp.content:
                    product.write({
                        "image_1920": base64.b64encode(resp.content),
                    })
            except Exception as e:
                _logger.debug(
                    "ELIT image download failed %s: %s",
                    product.elit_product_code or product.default_code,
                    e,
                )

    def elit_refresh_from_api(self):
        """Re-fetch a single ELIT product from the API and update all data.

        Intended as a manual "fix this product" action: queries the API by
        ``codigo_alfa`` for one product, applies stock/price/image data via
        :meth:`_apply_elit_data_to_product`, and immediately downloads the
        image if available.

        Raises :class:`~odoo.exceptions.UserError` if credentials are missing
        or the product cannot be resolved in ELIT.

        Must be called on exactly one record (use :meth:`action_elit_sync_from_api`
        for multi-select batch stock sync).
        """
        self.ensure_one()

        elit_code = self.elit_product_code
        if not elit_code:
            partner = self._get_elit_partner()
            if partner:
                si = self.env["product.supplierinfo"].search([
                    ("partner_id", "=", partner.id),
                    ("product_tmpl_id", "=", self.id),
                ], limit=1)
                elit_code = si.product_code if si else None
        if not elit_code:
            raise UserError(
                _("El producto no tiene código ELIT ni supplierinfo asociada.")
            )

        get_param = self.env["ir.config_parameter"].sudo().get_param
        user_id_str = get_param("elit.user_id")
        token = get_param("elit.token")
        api_url = get_param(
            "elit.api_url", "https://clientes.elit.com.ar"
        ).rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")

        if not user_id_str or not token:
            raise UserError(
                _("Faltan credenciales ELIT en la configuración del sistema.")
            )

        payload = {"user_id": int(user_id_str), "token": token}
        headers = {"Content-Type": "application/json"}
        params = {"codigo_alfa": elit_code, "limit": 1}

        try:
            response = requests.post(
                f"{api_url}{endpoint}",
                params=params,
                json=payload,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            raise UserError(
                _("Error consultando API ELIT para %s: %s") % (elit_code, e)
            )

        productos = data.get("resultado", [])
        if not productos:
            raise UserError(
                _("El producto %s no fue encontrado en la API de ELIT.")
                % elit_code
            )

        cotizacion = float(data.get("cotizacion") or 1.0)
        self._apply_elit_data_to_product(self, productos[0], cotizacion)
        self._elit_update_cost_all_companies(self)
        self._elit_download_image(force=True)

    def action_elit_sync_from_api(self):
        """Entry point for the consolidated product action menu.

        - **One** selected template: full refresh (stock, supplier price,
          image URL + binary) via :meth:`elit_refresh_from_api` — one API call.
        - **Several** templates: efficient paginated stock/cost sync via
          :meth:`update_stock_batch` (batched API pattern; no per-row loop).

        :return: Nothing for a single record; for several, the stats dict from
            :meth:`update_stock_batch` when applicable.
        """
        if not self:
            return
        if len(self) == 1:
            return self.elit_refresh_from_api()
        return self.env["product.template"].update_stock_batch(
            domain=[("id", "in", self.ids)],
            commit_interval=50,
        )
