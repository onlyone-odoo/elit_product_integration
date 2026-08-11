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

- Port features from 17.0_zippin without Zippin dependency: multi-company,
  sync watchdog (midnight-safe cycles), elit_raw_data, consolidated server
  action, allow_out_of_stock_order, website_sale_stock.
- Refresh dimensions (volume / volumetric weight) on every price-stock apply.
- Cart live-stock check: fall back to stored stock_elit on API errors.
- Price/stock batch creates missing products from the same API page.
- New-products batch resolves existing codes per page only.
- Wizard activates trigger+batch crons instead of a monolithic full-loop.
- Stronger API health check: validate JSON shape (resultado, cotizacion).
- Add .gitignore; migration 17.0.8.0.0 for shared ELIT supplierinfo.

`17.0.4.0.0`
------------

- Convert new-products import to trigger + batch + cleanup pattern.
- Fix _run_sync_batch error return and pagination by API page count.

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
