# Registro de decisiones arquitecturales

## 2026-04-24 — No versionar datos personales (LFPDPPP)

**Decisión:** Los datos personales de contactos B2B (nombres, teléfonos, correos) y cualquier archivo derivado de ellos no se versionan en el repositorio.

**Contexto:** El pipeline procesa datos personales de terceros en México, sujetos a la Ley Federal de Protección de Datos Personales en Posesión de los Particulares (LFPDPPP). Existe además un acuerdo explícito con el cliente de mantener estos datos fuera del control de versiones.

**Alcance:** Quedan excluidos del repositorio, vía `.gitignore`:
- `inputs/icp.md`, `inputs/blacklist.csv`, `inputs/*.csv` — insumos del cliente.
- `outputs/` — listas de leads generadas.
- `data/cache/`, `data/raw/`, `data/enriched/` — datos intermedios del pipeline.
- `.env`, `.env.local`, `*.key`, `*.pem` — credenciales y secretos.

**Consecuencias:**
- Los directorios `inputs/` y `outputs/` se mantienen en el repo mediante archivos `.gitkeep` para preservar la estructura, pero su contenido real permanece local.
- Cualquier ejemplo, fixture o dato de prueba que se incluya en el repo debe ser sintético y no derivado de datos reales del cliente.
- Cualquier excepción a esta política requiere aprobación formal del cliente y debe documentarse como una nueva entrada en este archivo.
