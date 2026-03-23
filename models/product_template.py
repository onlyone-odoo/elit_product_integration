import requests
import logging
from odoo import fields, models, api
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
            updated_products._update_cost_from_replenishment_cost()
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
                products_to_update_cost._update_cost_from_replenishment_cost()
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
            products_to_update_cost._update_cost_from_replenishment_cost()

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
                ("is_elit_product", "=", True),
                ("elit_product_code", "in", api_codes),
            ])
            code_to_product = {
                p.elit_product_code: p for p in products if p.elit_product_code
            }
        else:
            code_to_product = {}

        updated = 0
        errors = 0
        products_to_update_cost = self.env["product.template"]

        for prod in api_products:
            codigo = prod.get("codigo_alfa") or prod.get("codigo_producto")
            if not codigo or codigo not in code_to_product:
                continue
            try:
                self._apply_elit_data_to_product(code_to_product[codigo], prod, cotizacion)
                products_to_update_cost |= code_to_product[codigo]
                updated += 1
            except Exception as e:
                _logger.error("ELIT price/stock batch: error updating %s: %s", codigo, e)
                errors += 1

        if products_to_update_cost:
            products_to_update_cost._update_cost_from_replenishment_cost()

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
            "ELIT price/stock batch page done: updated=%d errors=%d offset=%d page_size=%d done=%s",
            updated, errors, api_offset, page_size, done,
        )
        return {
            "updated": updated,
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

        # Update product: only stock_elit and sync metadata (no replenishment_base_cost)
        product.write({
            "stock_elit": stock,
            "is_elit_product": True,
            "elit_last_sync": fields.Datetime.now(),
            "replenishment_cost_type": "supplier_price",
        })

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

        if supplierinfo:
            supplierinfo.write({
                "price": precio_with_tax,
                "currency_id": currency_id,
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
            })
