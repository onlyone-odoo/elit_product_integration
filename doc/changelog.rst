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
