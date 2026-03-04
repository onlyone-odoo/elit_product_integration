# controllers/website_sale.py
from odoo import http
from odoo.http import request
from odoo.addons.website_sale.controllers.main import WebsiteSale
from odoo.exceptions import UserError
import requests
import logging

_logger = logging.getLogger(__name__)


class WebsiteSaleElit(WebsiteSale):
    @http.route(
        ["/shop/cart/update"],
        type="http",
        auth="public",
        methods=["GET", "POST"],
        website=True,
        csrf=False,
    )
    def cart_update(self, product_id, add_qty=1, set_qty=0, **kw):
        """Extend cart update to check ELIT stock in real time."""
        product = request.env["product.product"].sudo().browse(int(product_id))
        template = product.product_tmpl_id

        if template.is_elit_product:
            # Consulta API en vivo para stock real de ELIT
            stock_elit = self._get_elit_stock_real_time(template.default_code)
            if stock_elit <= 0:
                # Eliminar la línea si no hay stock
                sale_order = request.website.sale_get_order()
                if sale_order:
                    line = sale_order.order_line.filtered(
                        lambda l: l.product_id.id == product.id
                    )
                    if line:
                        line.unlink()
                # Mensaje al cliente
                request.session[
                    "website_sale_cart_error"
                ] = "El producto no tiene stock disponible en el proveedor y fue eliminado del carrito."
                return request.redirect("/shop/cart")

        # Continuar con el cart_update normal
        return super(WebsiteSaleElit, self).cart_update(
            product_id, add_qty=add_qty, set_qty=set_qty, **kw
        )

    def _get_elit_stock_real_time(self, codigo):
        """Consulta API ELIT en vivo para stock de un producto."""
        get_param = request.env["ir.config_parameter"].sudo().get_param
        user_id = int(get_param("elit.user_id"))
        token = get_param("elit.token")
        api_url = get_param("elit.api_url", "https://clientes.elit.com.ar").rstrip("/")
        endpoint = get_param("elit.endpoint", "/v1/api/productos")

        payload = {"user_id": user_id, "token": token}
        headers = {"Content-Type": "application/json"}
        params = {"codigo_alfa": codigo, "limit": 1}

        try:
            response = requests.post(
                f"{api_url}{endpoint}",
                params=params,
                json=payload,
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()
            data = response.json()
            products = data.get("resultado", [])
            if products:
                return float(products[0].get("stock_total") or 0.0)
            return 0.0
        except Exception as e:
            _logger.warning(
                "Error consultando stock ELIT en vivo para %s: %s", codigo, e
            )
            # Fallback a stock_elit guardado si API falla
            return 0.0  # O request.env['product.template'].sudo().search([('default_code', '=', codigo)], limit=1).stock_elit or 0.0
