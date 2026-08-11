# models/elit_config.py
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    elit_user_id = fields.Integer(
        string="ELIT User ID", config_parameter="elit.user_id"
    )
    elit_token = fields.Char(string="ELIT Token", config_parameter="elit.token")
    elit_api_url = fields.Char(
        string="API URL",
        default="https://clientes.elit.com.ar",
        config_parameter="elit.api_url",
    )
    elit_endpoint = fields.Char(
        string="Endpoint", default="/v1/api/productos", config_parameter="elit.endpoint"
    )

    # Campos solo lectura para mostrar última sync
    elit_last_full_sync = fields.Datetime(string="Última Sinc. Completa", readonly=True)
    elit_last_incremental_sync = fields.Datetime(
        string="Última Sinc. Incremental", readonly=True
    )
    elit_public_categ_parent_id = fields.Many2one(
        "product.public.category",
        string="Categoría padre ecommerce",
        config_parameter="elit.public_categ_parent_id",
        help="Categoría raíz para productos ELIT en la tienda",
    )

    elit_partner_id = fields.Many2one(
        "res.partner",
        string="Proveedor ELIT",
        domain="[('supplier_rank', '>', 0)]",
        config_parameter="elit.partner_id",
        help="Partner usado como proveedor para productos ELIT",
    )

    elit_company_id = fields.Many2one(
        "res.company",
        string="Compañía datos ELIT",
        config_parameter="elit.company_id",
        help="Compañía para los datos de proveedor sincronizados desde ELIT "
        "(listas de precios de proveedor, impuestos y costos). "
        "Vacío = compartido entre todas las compañías.",
    )

    # Nuevos campos para offsets por tipo (reemplazan legacy)
    elit_full_offset = fields.Char(
        string="Último offset procesado (full sync)",
        config_parameter="elit.full_offset",
        readonly=True,
        help="Usado internamente para reanudar full sync si se interrumpe",
    )
    elit_incremental_offset = fields.Char(
        string="Último offset procesado (incremental sync)",
        config_parameter="elit.incremental_offset",
        readonly=True,
        help="Usado internamente para reanudar incremental sync si se interrumpe",
    )
    elit_update_stock_offset = fields.Char(
        string="Último offset procesado (update stock)",
        config_parameter="elit.update_stock_offset",
        readonly=True,
        help="Usado internamente para reanudar update stock si se interrumpe",
    )

    # API health check
    elit_api_notify_user_id = fields.Many2one(
        "res.users",
        string="Usuario a notificar (API ELIT)",
        config_parameter="elit.api_notify_user_id",
        help="Recibe mensaje interno en Discuss si la API ELIT no responde.",
    )
    elit_api_status = fields.Selection(
        [("ok", "OK"), ("error", "Error"), ("unknown", "Sin verificar")],
        string="Estado API ELIT",
        compute="_compute_elit_api_status",
    )
    elit_api_last_check = fields.Datetime(
        string="Último chequeo API ELIT",
        compute="_compute_elit_api_status",
    )
    elit_api_last_error = fields.Char(
        string="Último error API ELIT",
        compute="_compute_elit_api_status",
    )

    # Sync watchdog (last completed cycle per sync type)
    elit_new_products_last_done = fields.Datetime(
        string="Última sync productos nuevos completada",
        compute="_compute_elit_sync_status",
    )
    elit_price_stock_last_done = fields.Datetime(
        string="Última sync precio/stock completada",
        compute="_compute_elit_sync_status",
    )
    elit_sync_stale_info = fields.Char(
        string="Syncs ELIT vencidas",
        compute="_compute_elit_sync_status",
        help="Sincronizaciones que superaron el umbral máximo sin completarse.",
    )

    @api.depends("elit_user_id")
    def _compute_elit_api_status(self):
        ICP = self.env["ir.config_parameter"].sudo()
        status = ICP.get_param("elit.api_status", "unknown")
        last_check = ICP.get_param("elit.api_last_check", False)
        last_error = ICP.get_param("elit.api_last_error", False)
        for rec in self:
            rec.elit_api_status = status if status in ("ok", "error") else "unknown"
            rec.elit_api_last_check = last_check or False
            rec.elit_api_last_error = last_error or False

    @api.depends("elit_user_id")
    def _compute_elit_sync_status(self):
        ICP = self.env["ir.config_parameter"].sudo()
        new_products_last = ICP.get_param("elit.new_products_last_done", False)
        price_stock_last = ICP.get_param("elit.price_stock_last_done", False)
        stale = self.env["elit.sync.processor"]._elit_get_stale_syncs(ICP)
        stale_info = ", ".join(label for label, _last in stale) if stale else False
        for rec in self:
            rec.elit_new_products_last_done = new_products_last or False
            rec.elit_price_stock_last_done = price_stock_last or False
            rec.elit_sync_stale_info = stale_info

    def set_values(self):
        super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            "elit.last_full_sync", self.elit_last_full_sync
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "elit.last_incremental_sync", self.elit_last_incremental_sync
        )
        # Guardar offsets (aunque readonly, por si override manual)
        self.env["ir.config_parameter"].sudo().set_param(
            "elit.full_offset", self.elit_full_offset
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "elit.incremental_offset", self.elit_incremental_offset
        )
        self.env["ir.config_parameter"].sudo().set_param(
            "elit.update_stock_offset", self.elit_update_stock_offset
        )

    def get_values(self):
        res = super().get_values()
        res.update(
            {
                "elit_last_full_sync": self.env["ir.config_parameter"]
                .sudo()
                .get_param("elit.last_full_sync", False),
                "elit_last_incremental_sync": self.env["ir.config_parameter"]
                .sudo()
                .get_param("elit.last_incremental_sync", False),
                # Cargar offsets
                "elit_full_offset": self.env["ir.config_parameter"]
                .sudo()
                .get_param("elit.full_offset", "1"),
                "elit_incremental_offset": self.env["ir.config_parameter"]
                .sudo()
                .get_param("elit.incremental_offset", "1"),
                "elit_update_stock_offset": self.env["ir.config_parameter"]
                .sudo()
                .get_param("elit.update_stock_offset", "0"),
            }
        )
        return res
