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
