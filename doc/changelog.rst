`17.0.17.0.0`
-------------

- Catalog apply survives **deleted** (not only archived) ``product.template``
  rows: dangling supplierinfo FKs are ignored, mapping skips missing
  records, and the SKU is created again from the ELIT payload.

`17.0.16.0.0`
-------------

- Catalog apply no longer aborts on archived products (MissingError
  ``product.template(id,)``). Matching SKUs are unarchived and updated.
- Apply does not commit/rollback the cursor via the legacy importer, so one
  bad SKU cannot undo the rest of the batch.

`17.0.15.0.0`
-------------

- Unified catalog staging: ingest one API page per tick into ``elit.catalog.run``
  / ``elit.catalog.line``, then apply internally (no API). SKUs omitted from a
  **complete** snapshot get ``stock_elit=0`` (not archived) and cost is
  recalculated so dual-vendor glue can switch to Grupo Núcleo when it has stock.
- Empty first API page or incomplete ingest never zeroes stock.
- Ingest end is a short/empty page; ``paginador.total`` is not trusted.
- Legacy new-products / price-stock crons stay inactive (aliases of the
  catalog trigger). Full-loop methods remain manual.

`17.0.14.0.0`
-------------

- Settings: ELIT block uses Odoo 17 ``setting`` layout (no cramped
  one-column sync dump). Buttons start price/stock and new-products
  cycles from Inventory settings (same as the trigger crons).

`17.0.13.0.0`
-------------

- Do not reset the batch offset while a sync cycle is still in progress
  (6h/24h triggers were restarting the catalog walk).
- Ignore ``paginador.total`` when it equals the page size: that value is
  ambiguous and stopped price/stock after the first 100 products.
- Match API codes against ``codigo_alfa``, ``codigo_producto``, hyphen
  variants and internal prefixes (``LOGMOU910005795`` vs ``910-005795``)
  so existing reseller SKUs get stock updates instead of duplicates.

`17.0.12.0.0`
-------------

- ELIT rejects offset=0 with HTTP 400: omit offset on the first page
  (matches official curl examples) and only send offset when > 0.
- Log response body on HTTP errors to ease API debugging.

`17.0.11.0.0`
-------------

- Fix ELIT API pagination: offset is 0-based (paginador.offset starts at 0).
  Starting at 1 skipped the first catalog item and could leave gaps.
- Use paginador.total when present to detect the last page reliably.

`17.0.10.0.0`
-------------

- Refresh dimensions (volume / Zippin size / volumetric weight) on every
  price-stock apply, not only on create/full import.
- Cart live-stock check: fall back to stored stock_elit on API errors instead
  of treating failures as zero stock.
- Price/stock batch now creates missing products from the same API page
  (unified sync).
- New-products batch resolves existing codes per page only (no full catalog
  search each run).
- Wizard activates trigger+batch crons instead of a monolithic full-loop.
- Stronger API health check: validate JSON shape (resultado, cotizacion).
- Remove tracked __pycache__; add .gitignore.

`17.0.9.0.0`
------------

- Add elit_raw_data on product.template: last raw API JSON per product
  (including page cotización) for price/tax auditing, shown in a new
  "ELIT API" tab on the product form.

`17.0.8.0.0`
------------

- Multi-company support: optional company in Settings; supplierinfo created
  with explicit company_id (configured company or shared), taxes assigned per
  company, replenishment cost updated per company.
- Migration: clear company_id on existing ELIT supplierinfo (shared).
- Sync watchdog: last-completed timestamps per sync type, staleness alert via
  Discuss (hourly health-check cron) and status in Settings.
- Fix: batch crons no longer freeze silently when a sync cycle crosses
  midnight (in-progress flag with 48h safety reset).

.. Examples
.. `2.1.0`
.. -------

.. - Added Python Expressions

.. `2.0.0`
.. -------

.. - Migrated to Python 3

.. `1.1.0`
.. -------

.. - Add field selector


.. `1.0.0`
.. -------

.. - Init version
