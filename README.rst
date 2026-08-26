===============
ELIT Product Integration
===============

.. |badge1| image:: https://img.shields.io/badge/maturity-Beta-yellow
    :target: https://odoo-community.org/page/development-status
    :alt: Beta
.. |badge2| image:: https://img.shields.io/badge/licence-OPL--1-blue.png
    :target: https://www.odoo.com/documentation/17.0/legal/licenses/OPL-1.html
    :alt: License: OPL-1
.. |badge3| image:: https://onlyone.odoo.com/web/image/website/1/logo/OnlyOne%20Soft?unique=dccda5b
    :target: https://onlyone.odoo.com
    :alt: OnlyOne Soft

|badge1| |badge2| |badge3|

Este módulo permite la **importación y sincronización automática de productos desde la API de ELIT** hacia Odoo.

Funcionalidades principales:
- **Catálogo unificado (staging)**: ingest de **una página API** por ejecución hacia modelos auxiliares (``elit.catalog.run`` / ``elit.catalog.line``) y apply interno por lotes (sin API). Evita timeouts y permite detectar SKUs que ELIT deja de publicar.
- El precio del proveedor se guarda en **product.supplierinfo**; el módulo setea ``replenishment_cost_type = "supplier_price"`` y llama ``_update_cost_from_replenishment_cost()`` por batch (dependencia **product_replenishment_cost** OCA), para que el costo contable y las listas de precios basadas en costos se mantengan actualizados.
- Creación automática de categorías y subcategorías (internas y de ecommerce).
- Descarga de imágenes en lote mediante cron.
- Configuración automática de rutas **Buy** + **MTO** (al vender se genera orden de compra al proveedor).
- Campo ``stock_elit`` con stock total del proveedor. Si un SKU **no aparece** en un snapshot completo, se pone ``stock_elit=0`` (no se archiva) y se recalcula el costo para el glue dual-vendor con Grupo Núcleo.
- Partner "ELIT" creado automáticamente; configurable en Ajustes.
- Wizard manual para activar el ciclo de catálogo (ingest + apply).

Ideal para revendedores que hacen dropshipping o compra bajo demanda con ELIT.

**Table of contents**

.. contents::
   :local:

Instalación
===========

1. Instalar la dependencia Python (una vez en el entorno Odoo)::

    pip install requests

2. Instalar el módulo desde **Apps** → **Actualizar lista de aplicaciones** → Buscar **ELIT Product Integration**.

Configuración
=============

1. Ir a **Inventario** (o **Compras**) → **ELIT Integration** → **Configuration**.
2. Completar:
   - **User ID** y **Token** (proporcionados por ELIT).
   - **Proveedor ELIT**: partner que se usará como proveedor para los productos (pestaña Compras). Si no se elige, se busca/crea uno con nombre "ELIT".
   - URL y endpoint (por defecto correctos).
3. Opcional: **Usuario a notificar (API ELIT)** — recibe un mensaje interno en Discuss cuando el health check detecte que la API no responde. El indicador **Estado API** (● API OK / ● API Error / Sin verificar) y la fecha del último chequeo se muestran en la misma pantalla.
4. Guardar.

Precio y costo de reposición
----------------------------
- Los precios de ELIT se guardan en **Precios de compra** (product.supplierinfo). El módulo setea ``replenishment_cost_type = "supplier_price"`` y, tras cada batch de actualización, llama ``_update_cost_from_replenishment_cost()`` (módulo OCA ``product_replenishment_cost``) para que el costo contable se derive del supplierinfo. Las listas de precios basadas en costos se mantienen así actualizadas.

Uso
===

Primera vez (sincronización completa)
------------------------------------
1. Ir a **Inventario** → **Sincronizar Productos** (o menú ELIT según instalación).
2. Elegir **Full Synchronization**.
3. **Initial Offset** dejar en 1 para empezar desde el principio.
4. Clic en **Iniciar Sincronización**.

   → Se traen todos los productos de ELIT (puede tardar según cantidad). El progreso se guarda cada 20 productos; si se interrumpe, podés reanudar indicando el offset en el wizard.

Acciones planificadas (crons)
-----------------------------
Tras instalar/actualizar el módulo, en **Ajustes** → **Técnico** → **Automatización** → **Acciones planificadas** deberías tener:

+------------------------------------------+------------------+------------------------------------------------------------------+
| Nombre                                   | Frecuencia       | Descripción                                                      |
+==========================================+==================+==================================================================+
| ELIT: Activar sync de catálogo (staging) | Cada 6 horas     | Trigger: crea o reanuda una corrida ``elit.catalog.run``.        |
+------------------------------------------+------------------+------------------------------------------------------------------+
| ELIT: Lotes ingest catálogo              | Cada 5 min       | 1 página API (~100 SKUs) hacia líneas de staging. Página vacía   |
|                                          | (inactivo por     | o corta cierra el ingest. Error HTTP no avanza offset.          |
|                                          | defecto)         |                                                                  |
+------------------------------------------+------------------+------------------------------------------------------------------+
| ELIT: Lotes apply catálogo               | Cada 5 min       | Apply interno (~80 líneas). Al terminar, SKUs ELIT ausentes del  |
|                                          | (inactivo por     | snapshot quedan con ``stock_elit=0`` (no se archivan) y se      |
|                                          | defecto)         | recalcula el costo.                                              |
+------------------------------------------+------------------+------------------------------------------------------------------+
| ELIT: Desactivar crons de lotes si       | Cada 10 min      | Si ingest/apply terminó, desactiva esos crons (evita lock).      |
| corresponde                               |                  |                                                                  |
+------------------------------------------+------------------+------------------------------------------------------------------+
| ELIT: Health check API                   | Cada 1 hora      | Verifica si la API responde; indicador en Ajustes y notificación |
|                                          |                  | por Discuss al usuario configurado si hay error (solo al pasar  |
|                                          |                  | a error).                                                         |
+------------------------------------------+------------------+------------------------------------------------------------------+
| ELIT: Publicar productos con stock       | Cada 1 hora      | Publica en web productos ELIT con stock_elit > 5 y despublica   |
|                                          |                  | los que tienen stock_elit = 0. Con glue dual-vendor (ELIT+GN)    |
|                                          |                  | este cron original se desactiva si no está customizado.          |
+------------------------------------------+------------------+------------------------------------------------------------------+
| ELIT: Descargar imágenes (batch)         | Cada 2 horas      | Descarga en lote las imágenes pendientes (campo URL ELIT).       |
+------------------------------------------+------------------+------------------------------------------------------------------+
| ELIT: crons legacy (nuevos / precio)     | Desactivado      | Reemplazados por el ciclo de catálogo. El trigger alias arranca  |
|                                          |                  | el mismo staging. Full loop sigue como acción de servidor.       |
+------------------------------------------+------------------+------------------------------------------------------------------+

Resumen del flujo recomendado
-----------------------------
- **Catálogo**: el trigger cada 6 h arranca ingest (1 página API por lote) y luego apply interno. Al cerrar un snapshot **completo**, los productos ELIT que la API ya no lista quedan con ``stock_elit=0``. Dual-vendor: el módulo glue ``product_cost_vendor_in_stock`` pasa costo/MTO a Grupo Núcleo si ``stock_gn > 0``.
- Primera página API vacía o ingest incompleto: **no** se pone stock en 0 (evita un falso catálogo vacío).
- **Estado de la API**: el cron "Health check API" (cada 1 h) actualiza el indicador en Ajustes y notifica por Discuss si la API falla.

Acciones manuales desde la lista de productos
----------------------------------------------
- **ELIT: Sincronizar desde API**: 1 producto → refresh individual; varios → batch.
- **ELIT: Actualizar todo el catálogo (full loop)**: acción de servidor, no programada.
- **ELIT: Importar productos nuevos (full loop manual)**: acción de servidor, no programada.
- Inventario → **ELIT catalog runs**: auditoría de corridas y líneas de staging.

Los productos importados
------------------------
- Tienen rutas Buy + MTO (al vender se genera orden de compra al proveedor).
- Precio de ELIT en **Precios de compra** (supplierinfo); ``replenishment_cost_type = "supplier_price"`` y ``_update_cost_from_replenishment_cost()`` por batch mantienen el costo contable y las listas basadas en costos al día.
- Stock del proveedor en el campo **Stock ELIT**.
- Imagen principal (descarga en lote por cron o al crear).
- Listos para publicar en la tienda; el cron puede publicar/despublicar según stock_elit.

Known issues / Roadmap
======================

- Stock virtual (qty_available) no se sincroniza desde ELIT; el campo **Stock ELIT** es informativo.
- Próximas versiones: más opciones de publicación, manejo de atributos.

Bug Tracker
===========

¿Encontraste un bug o tenés una mejora?
→ https://github.com/onlyonesoft/odoo-custom/issues

Credits
=======

Authors
~~~~~~~
* Be OnlyOne

Contributors
~~~~~~~~~~~
* Matías Bressanello <matias@onlyone.odoo.com>

Maintainers
~~~~~~~~~~~
Este módulo es mantenido por:

.. image:: https://onlyone.odoo.com/web/image/website/1/logo/OnlyOne%20Soft?unique=dccda5b
   :alt: OnlyOne Soft
   :target: https://onlyone.odoo.com

Be OnlyOne – https://onlyone.odoo.com
