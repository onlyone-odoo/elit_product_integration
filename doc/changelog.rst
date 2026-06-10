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
