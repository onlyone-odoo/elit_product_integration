{
    "name": "ELIT Product Integration",
    "version": "17.0.16.0.0",
    "category": "Inventory/Purchase",
    "summary": "Importación y sincronización de productos desde API ELIT",
    "description": """
        Importa productos desde la API de ELIT[](https://api.elit.com.ar).
        Crea/actualiza product.template + product.supplierinfo y guarda costo base en USD.
    """,
    "author": "Be OnlyOne",
    "maintainers": ["onlyone-odoo"],
    "website": "https://onlyone.odoo.com/",
    "license": "AGPL-3",
    "depends": [
        "purchase_stock",
        "sale_stock",
        "website_sale",
        "website_sale_stock",
        "product_replenishment_cost",
        "zippin",
    ],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "views/elit_settings_views.xml",
        "views/product_template_views.xml",
        "views/elit_catalog_views.xml",
        "views/sync_wizard_views.xml",
        "data/cron.xml",
        "data/actions.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
