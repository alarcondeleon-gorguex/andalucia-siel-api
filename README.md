# Andalusia SIEL Election Results Downloader

Reproducible downloader for the election results displayed by
<https://resultadoseleccionesandalucia.es/>.

The website does not expose static CSV files in its HTML. Its tables are powered
by the public SIEL API from the Regional Government of Andalusia:

- API base: <https://ws040.juntadeandalucia.es/siel-api>
- Swagger UI: <https://ws040.juntadeandalucia.es/siel-api/swagger-ui.html>
- OpenAPI/Swagger JSON: <https://ws040.juntadeandalucia.es/siel-api/api-docs>

This project queries that API directly, stores the raw JSON responses as NDJSON,
and generates normalized CSV files. The main output is one complete CSV per
Andalusian regional election, with one row per polling-station table plus one
CERA/absentee-vote row per province when available.

## What You Get

For each election date, the downloader can generate:

- `processed/andalucia_<election>_completo.csv`: recommended final dataset.
  It contains ordinary polling-station tables and provincial CERA rows, with
  turnout, abstention, blank votes, invalid votes, valid votes, and party votes
  in columns.
- `processed/mesas_<election>_catalog.csv`: polling-station table catalog.
- `processed/mesas_<election>_long.csv`: one row per table and party.
- `processed/mesas_<election>_wide.csv`: one row per table, parties in columns.
- `processed/mesas_<election>_incidencias.csv`: tables with API warnings or
  download incidents.
- `processed/rausentes_<election>_long.csv`: CERA/absentee results by party.
- `processed/rausentes_<election>_wide.csv`: CERA/absentee results in wide form.
- `raw/*.ndjson`: raw API responses preserved before normalization.
- `manifest_<election>.json`: source URLs, timestamps, counts, and file paths.

The complete CSV intentionally excludes the autonomous-wide CERA row because it
duplicates the sum of the eight provincial CERA rows. The autonomous CERA raw
record is still preserved in `raw/rausentes_<election>.ndjson`.

## Quick Start

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

List available Andalusian regional elections:

```bash
python3 scripts/download_mesas.py --list-convocatorias
```

Download every available election into a separate output directory:

```bash
python3 scripts/download_mesas.py --all --workers 48 --output ./datasets
```

If the process is interrupted, resume it without redownloading completed tables:

```bash
python3 scripts/download_mesas.py --all --workers 48 --resume --output ./datasets
```

The final CSV files will be in:

```bash
./datasets/processed/andalucia_*_completo.csv
```

To download a single election:

```bash
python3 scripts/download_mesas.py --convocatoria 202206 --workers 48 --output data
```

To regenerate only the final CSV from already downloaded raw files:

```bash
python3 scripts/download_mesas.py --convocatoria 202206 --complete-only --output data
```

`--complete-only` checks the polling-station catalog when it exists. If the raw
file is partial, it stops and asks you to resume the download first.

## Data Quality Flags

The output CSVs include three control columns:

- `fuente_registro`: where the row came from, such as `api_mesa`,
  `api_resumen_mesa`, `api_resumen_mesa_parcial`, `api_cera`, or
  `catalogo_cera`.
- `estado_descarga`: download status, such as `ok`, `ok_sin_votos`,
  `cera_no_disponible`, `escrutinio_no_disponible`, `mesa_no_disponible`, or
  `error_interno_script`.
- `error_descarga`: short explanation when the API did not return everything.

Some historical tables exist in the official catalog and return a valid
`/v1/resumen/mesa` response, but `/v1/escrutinio/ambito/mesa` returns HTTP 500.
One reproduced case in `198606` is a table with `votostotales=0` and
`votoscandidatura=0`. In that situation the downloader preserves the official
summary and marks the row as `ok_sin_votos`.

If a table summary exists but party-level results cannot be obtained and it is
not a zero-vote case, the row is still kept as `escrutinio_no_disponible`. Party
columns are left empty rather than silently filled with zeros.

CERA/absentee-vote endpoints are available from `198606` to `202206`. For
`198205`, the official API currently returns HTTP 500 for CERA endpoints; the
downloader keeps eight provincial CERA rows marked as `cera_no_disponible`.

## API Flow

The table catalog is built with the same selector chain used by the website:

1. `/v1/opciones/fconvocatoria`
2. `/v1/opciones/provincia`
3. `/v1/opciones/municipio`
4. `/v1/opciones/distrito`
5. `/v1/opciones/seccion`
6. `/v1/opciones/mesa`

For each polling-station table:

- `/v1/resumen/mesa`
- `/v1/escrutinio/ambito/mesa`

For CERA/absentee results:

- `/v1/resumen/rausentes/cautonoma`
- `/v1/escrutinio/ambito/rausentes/cautonoma`
- `/v1/resumen/rausentes/provincia`
- `/v1/escrutinio/ambito/rausentes/provincia`

This avoids visual scraping and keeps the original API payloads available for
auditing.

## Practical Notes

On some macOS/Python setups, the `ws040.juntadeandalucia.es` certificate chain
is rejected as self-signed. For that reason, TLS verification is disabled by
default. If your certificate store accepts the host, enable strict verification:

```bash
python3 scripts/download_mesas.py --convocatoria 202206 --verify-tls
```

The API does not provide a documented bulk endpoint for all polling-station
tables in one election. The downloader therefore queries table by table, using
`--workers` for concurrency.

## Espanol

Descargador reproducible de los resultados que muestra
<https://resultadoseleccionesandalucia.es/>.

La web no publica CSV estaticos en el HTML. Sus tablas se alimentan de la API
SIEL de la Junta de Andalucia. Este proyecto consulta esa API, guarda las
respuestas originales en NDJSON y genera CSV normalizados.

El CSV principal es:

```bash
processed/andalucia_<eleccion>_completo.csv
```

Incluye una fila por mesa electoral ordinaria y una fila CERA por provincia
cuando la API la ofrece, con censo, votantes, abstenciones, participacion,
blancos, nulos, votos validos y votos por partido en columnas.

Para descargar todas las elecciones en tu carpeta de datasets:

```bash
cd andalucia-siel-api
python3 scripts/download_mesas.py --all --workers 48 --output ./datasets
```

Si se interrumpe:

```bash
cd andalucia-siel-api
python3 scripts/download_mesas.py --all --workers 48 --resume --output ./datasets
```

Los CSV finales quedaran en:

```bash
./datasets/processed/andalucia_*_completo.csv
```

Columnas de control importantes:

- `fuente_registro`: origen de la fila.
- `estado_descarga`: estado de descarga.
- `error_descarga`: explicacion breve si la API no devolvio algun dato.

Si la API reconoce una mesa pero no devuelve el escrutinio por partidos, la
mesa se conserva igualmente y se marca la incidencia. Para mesas con cero votos,
se conserva el resumen oficial y se marca `ok_sin_votos`. Para CERA en `198205`,
la API devuelve HTTP 500; se conservan ocho filas provinciales marcadas como
`cera_no_disponible`.
