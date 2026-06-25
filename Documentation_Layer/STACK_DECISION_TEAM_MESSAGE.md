# Fusion Flow V3 QAS - Stack Decision For Team

Mensaje sugerido para Teams:

Hola equipo, para arrancar el desarrollo de Fusion Flow V3 QAS necesitamos cerrar una decision base de stack e infraestructura. Revise la estructura actual de V3 y la rama `prod` de Fusion Flow V2 BKD, y la propuesta es mantenernos cerca de lo que ya funciona en produccion.

Propuesta:

- Infraestructura principal: Azure.
- Render: solo como opcion rapida para QAS/demo o fallback temporal.
- Backend: Flask con blueprints y servicios internos.
- Frontend: HTML/CSS/JS nativo con Jinja templates. No React/Angular de momento.
- Python: 3.12.
- Base de datos: Azure SQL / SQL Server.
- Driver SQL: ODBC Driver 18 como estandar nuevo, Driver 17 solo fallback.
- Portal de soporte: si. Debe incluir health, ingestion queue, technical logs, job history, settings operativos, retry/cancel cuando aplique.
- Analytics: si, pero fase 1 solo operacional: volumenes, errores, latencia, estado de jobs e ingesta. No BI avanzado hasta estabilizar los contratos de datos.

Motivo:

- V2 prod ya usa Flask, Jinja, HTML/CSS/JS, Gunicorn, Docker/Render, Azure SQL y pyodbc.
- V3 QAS ya esta organizado por capas y tiene Flask minimo, Graph scripts, SQL por esquemas y prototipo frontend nativo.
- Evitamos meter React/Angular o FastAPI antes de necesitarlos.
- La prioridad es trazabilidad, soporte y velocidad de entrega.

Estructura propuesta:

```text
Fusion_Flow_V3_QAS/
  Documentation_Layer/
  Configuration_Layer/
    SQL/
      migrations/
      seeds/
  Integration_Layer/
    Portal/
      fusion_portal/
        blueprints/
        services/
        templates/
        static/
      wsgi.py
    FLOW_V3/
    Graph/
  Infrastructure_Layer/
    Azure/
    Render/
    Docker/
  scripts/
  tests/
```

Decision que necesitamos confirmar:

1. Estamos de acuerdo en usar Azure como infraestructura objetivo?
2. Estamos de acuerdo en seguir con Flask en backend?
3. Estamos de acuerdo en mantener frontend nativo HTML/CSS/JS por ahora?
4. Estamos de acuerdo en incluir portal de soporte desde fase 1?
5. Estamos de acuerdo en analytics operacional basico, sin BI avanzado por ahora?

Si nadie ve bloqueo, propongo cerrar esta decision y empezar a crear la estructura base del proyecto con esta direccion.
