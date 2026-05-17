#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from andalucia_siel.client import BASE_URL, SielApiError, SielClient

TCONVOCATORIA_AUTONOMICAS = 5
CAUTONOMA_ANDALUCIA = 1

CONTEXT_FIELDS = [
    "tconvocatoria",
    "fconvocatoria",
    "provincia_clave",
    "provincia_nombre",
    "municipio_clave",
    "municipio_nombre",
    "distrito_clave",
    "distrito_nombre",
    "seccion_clave",
    "seccion_valor",
    "mesa_clave",
    "mesa_valor",
]

SUMMARY_FIELDS = [
    "censo",
    "abstenciones",
    "votostotales",
    "votosnulos",
    "votosvalidos",
    "votosblanco",
    "votoscandidatura",
    "nummesas",
]

DOWNLOAD_FIELDS = [
    "fuente_registro",
    "estado_descarga",
    "error_descarga",
]

LONG_FIELDS = CONTEXT_FIELDS + SUMMARY_FIELDS + [
    *DOWNLOAD_FIELDS,
    "conjunto",
    "numvotos",
    "porcentajeVotos",
    "diputados",
]

RAUSENTES_CONTEXT_FIELDS = [
    "tconvocatoria",
    "fconvocatoria",
    "ambito",
    "provincia_clave",
    "provincia_nombre",
]

RAUSENTES_LONG_FIELDS = RAUSENTES_CONTEXT_FIELDS + SUMMARY_FIELDS + [
    *DOWNLOAD_FIELDS,
    "conjunto",
    "numvotos",
    "porcentajeVotos",
    "diputados",
]

COMPLETE_BASE_FIELDS = [
    "tipo_registro",
    "ambito",
    *DOWNLOAD_FIELDS,
    "tconvocatoria",
    "fconvocatoria",
    "provincia_clave",
    "provincia_nombre",
    "municipio_clave",
    "municipio_nombre",
    "distrito_clave",
    "distrito_nombre",
    "seccion_clave",
    "seccion_valor",
    "mesa_clave",
    "mesa_valor",
    "censo",
    "votantes",
    "abstenciones",
    "participacion_pct",
    "abstencion_pct",
    "votos_nulos",
    "nulos_pct_votantes",
    "votos_validos",
    "votos_blanco",
    "blanco_pct_validos",
    "votos_candidaturas",
]

MISSING_PARTY_VOTES_STATUSES = {
    "cera_no_disponible",
    "mesa_no_disponible",
    "escrutinio_no_disponible",
    "error_interno_script",
}


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sort_clave_valor(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: (str(item.get("valor", "")), str(item.get("clave", ""))))


def pct(part: int | float | None, total: int | float | None) -> float | None:
    if part is None or total in (None, 0):
        return None
    return round((part / total) * 100, 4)


def api_error_detail(exc: BaseException) -> dict[str, Any]:
    detail = {"message": str(exc)}
    url = getattr(exc, "url", None)
    status_code = getattr(exc, "status_code", None)
    response_text = getattr(exc, "response_text", None)
    if url:
        detail["url"] = url
    if status_code:
        detail["status_code"] = status_code
    if response_text:
        detail["response_text"] = response_text
    return detail


def error_text(errors: list[dict[str, Any]] | None) -> str | None:
    if not errors:
        return None
    parts = []
    for error in errors:
        endpoint = error.get("endpoint")
        status_code = error.get("status_code")
        message = error.get("message")
        prefix = endpoint or "api"
        if status_code:
            prefix = f"{prefix} HTTP {status_code}"
        parts.append(f"{prefix}: {message}")
    return " | ".join(parts)


def download_fields(record: dict[str, Any], default_fuente: str) -> dict[str, Any]:
    return {
        "fuente_registro": record.get("fuente_registro", default_fuente),
        "estado_descarga": record.get("estado_descarga", "ok"),
        "error_descarga": record.get("error_descarga") or error_text(record.get("errors")),
    }


def party_votes_are_known(record: dict[str, Any]) -> bool:
    return record.get("estado_descarga", "ok") not in MISSING_PARTY_VOTES_STATUSES


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_ndjson(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def count_ndjson_records(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def mesa_params(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "cautonoma": CAUTONOMA_ANDALUCIA,
        "tconvocatoria": context["tconvocatoria"],
        "fconvocatoria": context["fconvocatoria"],
        "provincia": context["provincia_clave"],
        "municipio": context["municipio_clave"],
        "distrito": context["distrito_clave"],
        "seccion": context["seccion_valor"],
        "mesa": context["mesa_valor"],
    }


def context_key(context: dict[str, Any]) -> tuple[Any, ...]:
    return (
        context.get("tconvocatoria"),
        context.get("fconvocatoria"),
        context.get("provincia_clave"),
        context.get("municipio_clave"),
        context.get("distrito_clave"),
        str(context.get("seccion_valor")),
        str(context.get("mesa_valor")),
    )


def list_municipio_mesa_contexts(
    client: SielClient,
    fconvocatoria: int,
    provincia: dict[str, Any],
    municipio: dict[str, Any],
) -> list[dict[str, Any]]:
    provincia_clave = int(provincia["clave"])
    municipio_clave = int(municipio["clave"])
    base = {
        "cautonoma": CAUTONOMA_ANDALUCIA,
        "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
        "fconvocatoria": fconvocatoria,
        "provincia": provincia_clave,
        "municipio": municipio_clave,
    }
    contexts: list[dict[str, Any]] = []
    distritos = sort_clave_valor(client.opciones("distrito", base))
    for distrito in distritos:
        distrito_clave = int(distrito["clave"])
        secciones = sort_clave_valor(client.opciones("seccion", {**base, "distrito": distrito_clave}))
        for seccion in secciones:
            mesas = sort_clave_valor(
                client.opciones(
                    "mesa",
                    {
                        **base,
                        "distrito": distrito_clave,
                        "seccion": seccion["valor"],
                    },
                )
            )
            for mesa in mesas:
                contexts.append(
                    {
                        "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
                        "fconvocatoria": fconvocatoria,
                        "provincia_clave": provincia_clave,
                        "provincia_nombre": provincia["valor"],
                        "municipio_clave": municipio_clave,
                        "municipio_nombre": municipio["valor"],
                        "distrito_clave": distrito_clave,
                        "distrito_nombre": distrito["valor"],
                        "seccion_clave": seccion["clave"],
                        "seccion_valor": seccion["valor"],
                        "mesa_clave": mesa["clave"],
                        "mesa_valor": mesa["valor"],
                    }
                )
    return contexts


def iter_mesa_contexts(
    client: SielClient,
    fconvocatoria: int,
    province_filter: set[int] | None = None,
    limit: int | None = None,
    workers: int = 1,
) -> Iterable[dict[str, Any]]:
    base = {
        "cautonoma": CAUTONOMA_ANDALUCIA,
        "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
        "fconvocatoria": fconvocatoria,
    }
    provincias = sort_clave_valor(client.opciones("provincia", {"cautonoma": CAUTONOMA_ANDALUCIA}))
    yielded = 0

    for provincia in provincias:
        provincia_clave = int(provincia["clave"])
        if province_filter and provincia_clave not in province_filter:
            continue

        municipios = sort_clave_valor(
            client.opciones(
                "municipio",
                {**base, "provincia": provincia_clave},
            )
        )
        print(
            f"[{fconvocatoria}] {provincia['valor']}: {len(municipios)} municipios",
            file=sys.stderr,
        )

        if limit is None and workers > 1:
            completed = 0
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(list_municipio_mesa_contexts, client, fconvocatoria, provincia, municipio): municipio
                    for municipio in municipios
                }
                for future in as_completed(futures):
                    completed += 1
                    for context in future.result():
                        yield context
                    if completed % 25 == 0 or completed == len(municipios):
                        print(
                            f"[{fconvocatoria}] {provincia['valor']}: "
                            f"{completed}/{len(municipios)} municipios enumerados",
                            file=sys.stderr,
                        )
            continue

        for municipio in municipios:
            for context in list_municipio_mesa_contexts(client, fconvocatoria, provincia, municipio):
                yield context
                yielded += 1
                if limit is not None and yielded >= limit:
                    return


def download_mesa(client: SielClient, context: dict[str, Any]) -> dict[str, Any]:
    params = mesa_params(context)
    source = {
        "api_base": client.base_url,
        "resumen_url": client.build_url("/v1/resumen/mesa", params),
        "escrutinio_url": client.build_url("/v1/escrutinio/ambito/mesa", params),
        "params": params,
    }
    record = {
        "source": {
            **source,
        },
        "context": context,
        "fuente_registro": "api_mesa",
        "estado_descarga": "ok",
        "error_descarga": None,
        "errors": [],
    }
    try:
        resumen = client.resumen_mesa(params)
    except SielApiError as exc:
        detail = {"endpoint": "resumen/mesa", **api_error_detail(exc)}
        record.update(
            {
                "resumen": {},
                "escrutinio": {"votos": []},
                "fuente_registro": "catalogo_mesa",
                "estado_descarga": "mesa_no_disponible",
                "errors": [detail],
                "error_descarga": error_text([detail]),
            }
        )
        return record

    record["resumen"] = resumen
    try:
        record["escrutinio"] = client.escrutinio_mesa(params)
        return record
    except SielApiError as exc:
        detail = {"endpoint": "escrutinio/ambito/mesa", **api_error_detail(exc)}
        votos_candidatura = resumen.get("votoscandidatura")
        votos_totales = resumen.get("votostotales")
        if votos_candidatura == 0 and votos_totales == 0:
            record.update(
                {
                    "escrutinio": {
                        "descripcion": (
                            "La API devolvió error en escrutinio/mesa, pero resumen/mesa indica "
                            "cero votos totales y cero votos a candidaturas."
                        ),
                        "distribucion": "ambito",
                        "idescrutinio": resumen.get("idescrutinio"),
                        "nombreconjunto": "Formación política",
                        "votos": [],
                    },
                    "fuente_registro": "api_resumen_mesa",
                    "estado_descarga": "ok_sin_votos",
                    "errors": [detail],
                    "error_descarga": (
                        "escrutinio/ambito/mesa no disponible; resumen/mesa indica "
                        "votostotales=0 y votoscandidatura=0"
                    ),
                }
            )
        else:
            record.update(
                {
                    "escrutinio": {"votos": []},
                    "fuente_registro": "api_resumen_mesa_parcial",
                    "estado_descarga": "escrutinio_no_disponible",
                    "errors": [detail],
                    "error_descarga": error_text([detail]),
                }
            )
        return record


def unavailable_mesa_record(client: SielClient, context: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    params = mesa_params(context)
    detail = {"endpoint": "download_mesa", **api_error_detail(exc)}
    return {
        "source": {
            "api_base": client.base_url,
            "resumen_url": client.build_url("/v1/resumen/mesa", params),
            "escrutinio_url": client.build_url("/v1/escrutinio/ambito/mesa", params),
            "params": params,
        },
        "context": context,
        "resumen": {},
        "escrutinio": {"votos": []},
        "fuente_registro": "catalogo_mesa",
        "estado_descarga": "error_interno_script",
        "errors": [detail],
        "error_descarga": error_text([detail]),
    }


def iter_rausentes_contexts(
    client: SielClient,
    fconvocatoria: int,
    province_filter: set[int] | None = None,
) -> Iterable[dict[str, Any]]:
    if province_filter is None:
        yield {
            "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
            "fconvocatoria": fconvocatoria,
            "ambito": "cautonoma",
            "provincia_clave": None,
            "provincia_nombre": None,
        }

    provincias = sort_clave_valor(client.opciones("provincia", {"cautonoma": CAUTONOMA_ANDALUCIA}))
    for provincia in provincias:
        provincia_clave = int(provincia["clave"])
        if province_filter and provincia_clave not in province_filter:
            continue
        yield {
            "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
            "fconvocatoria": fconvocatoria,
            "ambito": "provincia",
            "provincia_clave": provincia_clave,
            "provincia_nombre": provincia["valor"],
        }


def rausentes_params(context: dict[str, Any]) -> dict[str, Any]:
    params = {
        "cautonoma": CAUTONOMA_ANDALUCIA,
        "tconvocatoria": context["tconvocatoria"],
        "fconvocatoria": context["fconvocatoria"],
    }
    if context["ambito"] == "provincia":
        params["provincia"] = context["provincia_clave"]
    return params


def download_rausentes_record(client: SielClient, context: dict[str, Any]) -> dict[str, Any]:
    ambito = context["ambito"]
    params = rausentes_params(context)
    return {
        "source": {
            "api_base": client.base_url,
            "resumen_url": client.build_url(f"/v1/resumen/rausentes/{ambito}", params),
            "escrutinio_url": client.build_url(f"/v1/escrutinio/ambito/rausentes/{ambito}", params),
            "params": params,
        },
        "context": context,
        "fuente_registro": "api_cera",
        "estado_descarga": "ok",
        "error_descarga": None,
        "errors": [],
        "resumen": client.resumen_rausentes(ambito, params),
        "escrutinio": client.escrutinio_rausentes(ambito, params),
    }


def unavailable_rausentes_record(client: SielClient, context: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    ambito = context["ambito"]
    params = rausentes_params(context)
    detail = {"endpoint": f"rausentes/{ambito}", **api_error_detail(exc)}
    return {
        "source": {
            "api_base": client.base_url,
            "resumen_url": client.build_url(f"/v1/resumen/rausentes/{ambito}", params),
            "escrutinio_url": client.build_url(f"/v1/escrutinio/ambito/rausentes/{ambito}", params),
            "params": params,
        },
        "context": context,
        "fuente_registro": "catalogo_cera",
        "estado_descarga": "cera_no_disponible",
        "error_descarga": error_text([detail]),
        "errors": [detail],
        "resumen": {},
        "escrutinio": {"votos": []},
    }


def base_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    context = record["context"]
    resumen = record.get("resumen", {})
    row = {field: context.get(field) for field in CONTEXT_FIELDS}
    row.update({field: resumen.get(field) for field in SUMMARY_FIELDS})
    row.update(download_fields(record, "api_mesa"))
    return row


def base_rausentes_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    context = record["context"]
    resumen = record.get("resumen", {})
    row = {field: context.get(field) for field in RAUSENTES_CONTEXT_FIELDS}
    row.update({field: resumen.get(field) for field in SUMMARY_FIELDS})
    row.update(download_fields(record, "api_cera"))
    return row


def write_catalog_csv(path: Path, contexts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTEXT_FIELDS)
        writer.writeheader()
        writer.writerows(contexts)


def read_catalog_csv(path: Path) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    int_fields = {
        "tconvocatoria",
        "fconvocatoria",
        "provincia_clave",
        "municipio_clave",
        "distrito_clave",
        "seccion_clave",
        "mesa_clave",
    }
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            context: dict[str, Any] = {}
            for field in CONTEXT_FIELDS:
                value = row.get(field)
                if field in int_fields and value not in (None, ""):
                    context[field] = int(value)
                else:
                    context[field] = value
            contexts.append(context)
    return contexts


def sort_mesa_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        contexts,
        key=lambda context: (
            context["provincia_clave"],
            context["municipio_clave"],
            context["distrito_clave"],
            str(context["seccion_valor"]),
            str(context["mesa_valor"]),
        ),
    )


def write_long_csv(ndjson_path: Path, csv_path: Path) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LONG_FIELDS)
        writer.writeheader()
        for record in read_ndjson(ndjson_path):
            base = base_csv_row(record)
            for voto in record.get("escrutinio", {}).get("votos", []):
                row = dict(base)
                row.update(
                    {
                        "conjunto": voto.get("conjunto"),
                        "numvotos": voto.get("numvotos"),
                        "porcentajeVotos": voto.get("porcentajeVotos"),
                        "diputados": voto.get("diputados"),
                    }
                )
                writer.writerow(row)
                row_count += 1
    return row_count


def write_wide_csv(ndjson_path: Path, csv_path: Path) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    conjuntos: list[str] = []
    seen: set[str] = set()
    for record in read_ndjson(ndjson_path):
        for voto in record.get("escrutinio", {}).get("votos", []):
            conjunto = voto.get("conjunto")
            if conjunto and conjunto not in seen:
                seen.add(conjunto)
                conjuntos.append(conjunto)

    fieldnames = CONTEXT_FIELDS + SUMMARY_FIELDS + DOWNLOAD_FIELDS + conjuntos
    row_count = 0
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in read_ndjson(ndjson_path):
            row = base_csv_row(record)
            default_value = 0 if party_votes_are_known(record) else None
            row.update({conjunto: default_value for conjunto in conjuntos})
            for voto in record.get("escrutinio", {}).get("votos", []):
                conjunto = voto.get("conjunto")
                if conjunto:
                    row[conjunto] = voto.get("numvotos", 0)
            writer.writerow(row)
            row_count += 1
    return row_count


def write_rausentes_long_csv(ndjson_path: Path, csv_path: Path) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAUSENTES_LONG_FIELDS)
        writer.writeheader()
        for record in read_ndjson(ndjson_path):
            base = base_rausentes_csv_row(record)
            for voto in record.get("escrutinio", {}).get("votos", []):
                row = dict(base)
                row.update(
                    {
                        "conjunto": voto.get("conjunto"),
                        "numvotos": voto.get("numvotos"),
                        "porcentajeVotos": voto.get("porcentajeVotos"),
                        "diputados": voto.get("diputados"),
                    }
                )
                writer.writerow(row)
                row_count += 1
    return row_count


def write_rausentes_wide_csv(ndjson_path: Path, csv_path: Path) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    conjuntos: list[str] = []
    seen: set[str] = set()
    for record in read_ndjson(ndjson_path):
        for voto in record.get("escrutinio", {}).get("votos", []):
            conjunto = voto.get("conjunto")
            if conjunto and conjunto not in seen:
                seen.add(conjunto)
                conjuntos.append(conjunto)

    fieldnames = RAUSENTES_CONTEXT_FIELDS + SUMMARY_FIELDS + DOWNLOAD_FIELDS + conjuntos
    row_count = 0
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in read_ndjson(ndjson_path):
            row = base_rausentes_csv_row(record)
            default_value = 0 if party_votes_are_known(record) else None
            row.update({conjunto: default_value for conjunto in conjuntos})
            for voto in record.get("escrutinio", {}).get("votos", []):
                conjunto = voto.get("conjunto")
                if conjunto:
                    row[conjunto] = voto.get("numvotos", 0)
            writer.writerow(row)
            row_count += 1
    return row_count


def iter_complete_source_records(mesas_path: Path, rausentes_path: Path | None) -> Iterable[dict[str, Any]]:
    for record in read_ndjson(mesas_path):
        yield {"tipo_registro": "mesa", "ambito": "mesa", "record": record}

    if rausentes_path is None or not rausentes_path.exists():
        return

    for record in read_ndjson(rausentes_path):
        context = record.get("context", {})
        if context.get("ambito") != "provincia":
            continue
        yield {"tipo_registro": "cera", "ambito": "cera_provincia", "record": record}


def complete_row_base(source: dict[str, Any]) -> dict[str, Any]:
    record = source["record"]
    context = record["context"]
    resumen = record.get("resumen", {})
    votantes = resumen.get("votostotales")
    censo = resumen.get("censo")
    votos_validos = resumen.get("votosvalidos")

    if source["tipo_registro"] == "mesa":
        row = {
            "tipo_registro": "mesa",
            "ambito": "mesa",
            **download_fields(record, "api_mesa"),
            "tconvocatoria": context.get("tconvocatoria"),
            "fconvocatoria": context.get("fconvocatoria"),
            "provincia_clave": context.get("provincia_clave"),
            "provincia_nombre": context.get("provincia_nombre"),
            "municipio_clave": context.get("municipio_clave"),
            "municipio_nombre": context.get("municipio_nombre"),
            "distrito_clave": context.get("distrito_clave"),
            "distrito_nombre": context.get("distrito_nombre"),
            "seccion_clave": context.get("seccion_clave"),
            "seccion_valor": context.get("seccion_valor"),
            "mesa_clave": context.get("mesa_clave"),
            "mesa_valor": context.get("mesa_valor"),
        }
    else:
        row = {
            "tipo_registro": "cera",
            "ambito": "cera_provincia",
            **download_fields(record, "api_cera"),
            "tconvocatoria": context.get("tconvocatoria"),
            "fconvocatoria": context.get("fconvocatoria"),
            "provincia_clave": context.get("provincia_clave"),
            "provincia_nombre": context.get("provincia_nombre"),
            "municipio_clave": None,
            "municipio_nombre": None,
            "distrito_clave": None,
            "distrito_nombre": None,
            "seccion_clave": None,
            "seccion_valor": None,
            "mesa_clave": "CERA",
            "mesa_valor": "CERA",
        }

    row.update(
        {
            "censo": censo,
            "votantes": votantes,
            "abstenciones": resumen.get("abstenciones"),
            "participacion_pct": pct(votantes, censo),
            "abstencion_pct": pct(resumen.get("abstenciones"), censo),
            "votos_nulos": resumen.get("votosnulos"),
            "nulos_pct_votantes": pct(resumen.get("votosnulos"), votantes),
            "votos_validos": votos_validos,
            "votos_blanco": resumen.get("votosblanco"),
            "blanco_pct_validos": pct(resumen.get("votosblanco"), votos_validos),
            "votos_candidaturas": resumen.get("votoscandidatura"),
        }
    )
    return row


def write_complete_csv(mesas_path: Path, rausentes_path: Path | None, csv_path: Path) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    conjuntos: list[str] = []
    seen: set[str] = set()
    for source in iter_complete_source_records(mesas_path, rausentes_path):
        for voto in source["record"].get("escrutinio", {}).get("votos", []):
            conjunto = voto.get("conjunto")
            if conjunto and conjunto not in seen:
                seen.add(conjunto)
                conjuntos.append(conjunto)

    fieldnames = COMPLETE_BASE_FIELDS + conjuntos
    row_count = 0
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for source in iter_complete_source_records(mesas_path, rausentes_path):
            record = source["record"]
            row = complete_row_base(source)
            default_value = 0 if party_votes_are_known(record) else None
            row.update({conjunto: default_value for conjunto in conjuntos})
            for voto in record.get("escrutinio", {}).get("votos", []):
                conjunto = voto.get("conjunto")
                if conjunto:
                    row[conjunto] = voto.get("numvotos", 0)
            writer.writerow(row)
            row_count += 1
    return row_count


def write_mesa_incidents_csv(ndjson_path: Path, csv_path: Path) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = CONTEXT_FIELDS + DOWNLOAD_FIELDS + ["resumen_url", "escrutinio_url"]
    row_count = 0
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in read_ndjson(ndjson_path):
            status = record.get("estado_descarga", "ok")
            if status == "ok" and not record.get("errors"):
                continue
            row = {field: record.get("context", {}).get(field) for field in CONTEXT_FIELDS}
            row.update(download_fields(record, "api_mesa"))
            source = record.get("source", {})
            row["resumen_url"] = source.get("resumen_url")
            row["escrutinio_url"] = source.get("escrutinio_url")
            writer.writerow(row)
            row_count += 1
    return row_count


def count_download_statuses(ndjson_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in read_ndjson(ndjson_path):
        status = record.get("estado_descarga", "ok")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def save_source_metadata(client: SielClient, output_dir: Path) -> None:
    raw_dir = output_dir / "raw"
    opciones_dir = raw_dir / "opciones"
    write_json(raw_dir / "api_docs.json", client.api_docs())
    write_json(
        opciones_dir / "fconvocatoria_t5.json",
        client.opciones("fconvocatoria", {"tconvocatoria": TCONVOCATORIA_AUTONOMICAS}),
    )
    write_json(
        opciones_dir / "provincias.json",
        client.opciones("provincia", {"cautonoma": CAUTONOMA_ANDALUCIA}),
    )


def list_convocatorias(client: SielClient) -> None:
    convocatorias = client.opciones("fconvocatoria", {"tconvocatoria": TCONVOCATORIA_AUTONOMICAS})
    print("clave,valor")
    for item in convocatorias:
        print(f"{item['clave']},{item['valor']}")


def download_convocatoria(
    client: SielClient,
    fconvocatoria: int,
    output_dir: Path,
    workers: int,
    province_filter: set[int] | None,
    limit: int | None,
    raw_only: bool,
    overwrite: bool,
    resume: bool,
) -> None:
    raw_path = output_dir / "raw" / f"mesas_{fconvocatoria}.ndjson"
    catalog_path = output_dir / "processed" / f"mesas_{fconvocatoria}_catalog.csv"
    long_path = output_dir / "processed" / f"mesas_{fconvocatoria}_long.csv"
    wide_path = output_dir / "processed" / f"mesas_{fconvocatoria}_wide.csv"
    complete_path = output_dir / "processed" / f"andalucia_{fconvocatoria}_completo.csv"
    incidents_path = output_dir / "processed" / f"mesas_{fconvocatoria}_incidencias.csv"
    rausentes_raw_path = output_dir / "raw" / f"rausentes_{fconvocatoria}.ndjson"
    manifest_path = output_dir / f"manifest_{fconvocatoria}.json"

    if raw_path.exists() and not overwrite and not resume:
        raise SystemExit(f"{raw_path} already exists. Use --overwrite to replace it.")

    if resume and catalog_path.exists() and limit is None:
        contexts = sort_mesa_contexts(read_catalog_csv(catalog_path))
        print(f"[{fconvocatoria}] usando catálogo existente: {len(contexts)} mesas", file=sys.stderr)
    else:
        print(f"[{fconvocatoria}] enumerando mesas", file=sys.stderr)
        contexts = sort_mesa_contexts(list(iter_mesa_contexts(client, fconvocatoria, province_filter, limit, workers)))
        print(f"[{fconvocatoria}] {len(contexts)} mesas encontradas", file=sys.stderr)
        write_catalog_csv(catalog_path, contexts)

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    existing_keys: set[tuple[Any, ...]] = set()
    if resume and raw_path.exists() and not overwrite:
        for record in read_ndjson(raw_path):
            existing_keys.add(context_key(record["context"]))
        print(
            f"[{fconvocatoria}] reanudando: {len(existing_keys)}/{len(contexts)} mesas ya descargadas",
            file=sys.stderr,
        )

    remaining_contexts = [context for context in contexts if context_key(context) not in existing_keys]
    mode = "w" if overwrite or not raw_path.exists() else "a"
    with raw_path.open(mode, encoding="utf-8") as handle:
        if remaining_contexts:
            with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
                futures = {executor.submit(download_mesa, client, context): context for context in remaining_contexts}
                for index, future in enumerate(as_completed(futures), start=1):
                    context = futures[future]
                    try:
                        record = future.result()
                    except Exception as exc:
                        record = unavailable_mesa_record(client, context, exc)
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    completed = len(existing_keys) + index
                    if completed % 500 == 0 or completed == len(contexts):
                        print(f"[{fconvocatoria}] descargadas {completed}/{len(contexts)} mesas", file=sys.stderr)
        else:
            print(f"[{fconvocatoria}] raw de mesas ya completo; regenerando derivados", file=sys.stderr)

    long_rows = None
    wide_rows = None
    complete_rows = None
    incident_rows = write_mesa_incidents_csv(raw_path, incidents_path)
    status_counts = count_download_statuses(raw_path)
    if not raw_only:
        print(f"[{fconvocatoria}] generando CSV largo", file=sys.stderr)
        long_rows = write_long_csv(raw_path, long_path)
        print(f"[{fconvocatoria}] generando CSV ancho", file=sys.stderr)
        wide_rows = write_wide_csv(raw_path, wide_path)
        print(f"[{fconvocatoria}] generando CSV completo mesas + CERA", file=sys.stderr)
        complete_rows = write_complete_csv(
            raw_path,
            rausentes_raw_path if rausentes_raw_path.exists() else None,
            complete_path,
        )

    manifest = {
        "source": {
            "home_page": "https://resultadoseleccionesandalucia.es/",
            "api_base": BASE_URL,
            "swagger_ui": f"{BASE_URL}/swagger-ui.html",
            "api_docs": f"{BASE_URL}/api-docs",
        },
        "fetched_at": now_utc(),
        "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
        "fconvocatoria": fconvocatoria,
        "counts": {
            "mesas": len(contexts),
            "download_statuses": status_counts,
            "incidents": incident_rows,
            "long_rows": long_rows,
            "wide_rows": wide_rows,
            "complete_rows": complete_rows,
        },
        "files": {
            "raw_ndjson": str(raw_path),
            "catalog_csv": str(catalog_path),
            "long_csv": None if raw_only else str(long_path),
            "wide_csv": None if raw_only else str(wide_path),
            "complete_csv": None if raw_only else str(complete_path),
            "incidents_csv": str(incidents_path),
        },
    }
    write_json(manifest_path, manifest)
    print(f"[{fconvocatoria}] listo: {manifest_path}", file=sys.stderr)


def download_rausentes(
    client: SielClient,
    fconvocatoria: int,
    output_dir: Path,
    province_filter: set[int] | None,
    raw_only: bool,
    overwrite: bool,
) -> dict[str, Any]:
    raw_path = output_dir / "raw" / f"rausentes_{fconvocatoria}.ndjson"
    long_path = output_dir / "processed" / f"rausentes_{fconvocatoria}_long.csv"
    wide_path = output_dir / "processed" / f"rausentes_{fconvocatoria}_wide.csv"

    if raw_path.exists() and not overwrite:
        raise SystemExit(f"{raw_path} already exists. Use --overwrite to replace it.")

    contexts = list(iter_rausentes_contexts(client, fconvocatoria, province_filter))
    print(f"[{fconvocatoria}] descargando CERA/residentes ausentes ({len(contexts)} agregados)", file=sys.stderr)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []
    with raw_path.open("w", encoding="utf-8") as handle:
        for context in contexts:
            try:
                record = download_rausentes_record(client, context)
            except SielApiError as exc:
                errors.append({"context": context, "error": str(exc)})
                record = unavailable_rausentes_record(client, context, exc)
                print(f"[{fconvocatoria}] CERA marcado como no disponible para {context}: {exc}", file=sys.stderr)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    long_rows = None
    wide_rows = None
    status_counts = count_download_statuses(raw_path)
    if not raw_only:
        long_rows = write_rausentes_long_csv(raw_path, long_path)
        wide_rows = write_rausentes_wide_csv(raw_path, wide_path)

    return {
        "counts": {
            "agregados": len(contexts),
            "downloaded": status_counts.get("ok", 0),
            "failed": len(errors),
            "download_statuses": status_counts,
            "long_rows": long_rows,
            "wide_rows": wide_rows,
        },
        "files": {
            "raw_ndjson": str(raw_path),
            "long_csv": None if raw_only else str(long_path),
            "wide_csv": None if raw_only else str(wide_path),
        },
        "note": "La API SIEL expone residentes ausentes en agregado autonómico y provincial.",
        "errors": errors,
    }


def build_complete_from_existing(output_dir: Path, fconvocatoria: int) -> dict[str, Any]:
    mesas_path = output_dir / "raw" / f"mesas_{fconvocatoria}.ndjson"
    catalog_path = output_dir / "processed" / f"mesas_{fconvocatoria}_catalog.csv"
    rausentes_path = output_dir / "raw" / f"rausentes_{fconvocatoria}.ndjson"
    complete_path = output_dir / "processed" / f"andalucia_{fconvocatoria}_completo.csv"
    incidents_path = output_dir / "processed" / f"mesas_{fconvocatoria}_incidencias.csv"
    if not mesas_path.exists():
        raise SystemExit(f"No existe {mesas_path}. Descarga primero la convocatoria.")
    raw_count = count_ndjson_records(mesas_path)
    if catalog_path.exists():
        catalog_count = len(read_catalog_csv(catalog_path))
        if raw_count < catalog_count:
            raise SystemExit(
                f"Raw incompleto para {fconvocatoria}: {raw_count}/{catalog_count} mesas. "
                "Ejecuta de nuevo con --resume antes de generar el CSV completo."
            )

    complete_rows = write_complete_csv(
        mesas_path,
        rausentes_path if rausentes_path.exists() else None,
        complete_path,
    )
    incident_rows = write_mesa_incidents_csv(mesas_path, incidents_path)
    status_counts = count_download_statuses(mesas_path)
    manifest_path = output_dir / f"manifest_{fconvocatoria}.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("counts", {})["complete_rows"] = complete_rows
        manifest.setdefault("counts", {})["download_statuses"] = status_counts
        manifest.setdefault("counts", {})["incidents"] = incident_rows
        manifest.setdefault("files", {})["complete_csv"] = str(complete_path)
        manifest.setdefault("files", {})["incidents_csv"] = str(incidents_path)
        write_json(manifest_path, manifest)

    return {
        "complete_rows": complete_rows,
        "complete_csv": str(complete_path),
        "incident_rows": incident_rows,
        "incidents_csv": str(incidents_path),
        "download_statuses": status_counts,
    }


def parse_convocatorias(values: list[str] | None) -> list[int]:
    if not values:
        return []
    parsed: list[int] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                parsed.append(int(part))
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Descarga resultados de mesas desde la API SIEL.")
    parser.add_argument("--list-convocatorias", action="store_true", help="Muestra las convocatorias disponibles y termina.")
    parser.add_argument("--all", action="store_true", help="Descarga todas las autonómicas disponibles.")
    parser.add_argument("--convocatoria", action="append", help="Convocatoria a descargar, por ejemplo 202206. Se puede repetir o separar por comas.")
    parser.add_argument("--output", default="data", help="Directorio de salida. Por defecto: data")
    parser.add_argument("--workers", type=int, default=6, help="Descargas concurrentes de mesas. Por defecto: 6")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout por petición HTTP en segundos. Por defecto: 30")
    parser.add_argument("--retries", type=int, default=2, help="Reintentos por petición HTTP. Por defecto: 2")
    parser.add_argument("--province", action="append", type=int, help="Filtra por código de provincia INE, por ejemplo 4 o 41.")
    parser.add_argument("--limit-mesas", type=int, help="Límite de mesas para pruebas rápidas.")
    parser.add_argument("--raw-only", action="store_true", help="Solo escribe el NDJSON bruto, sin CSV derivados.")
    parser.add_argument("--skip-rausentes", action="store_true", help="No descarga CERA/residentes ausentes.")
    parser.add_argument("--rausentes-only", action="store_true", help="Descarga solo CERA/residentes ausentes.")
    parser.add_argument("--complete-only", action="store_true", help="Genera solo el CSV completo desde raw ya descargado.")
    parser.add_argument("--resume", action="store_true", help="Reanuda una descarga parcial de mesas ya existente.")
    parser.add_argument("--overwrite", action="store_true", help="Sobrescribe una descarga existente.")
    parser.add_argument("--verify-tls", action="store_true", help="Fuerza verificación TLS estricta en la API de la Junta.")
    args = parser.parse_args()

    if args.rausentes_only and args.skip_rausentes:
        parser.error("--rausentes-only no se puede combinar con --skip-rausentes")
    if args.complete_only and (args.rausentes_only or args.raw_only):
        parser.error("--complete-only no se puede combinar con --rausentes-only ni --raw-only")
    if args.resume and args.overwrite:
        parser.error("--resume y --overwrite son mutuamente excluyentes")

    client = SielClient(verify_tls=args.verify_tls, timeout=args.timeout, retries=args.retries, pool_size=max(args.workers, 32))
    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    try:
        if args.list_convocatorias:
            list_convocatorias(client)
            return 0

        save_source_metadata(client, output_dir)
        if args.all:
            convocatorias = [
                int(item["clave"])
                for item in client.opciones("fconvocatoria", {"tconvocatoria": TCONVOCATORIA_AUTONOMICAS})
            ]
        else:
            convocatorias = parse_convocatorias(args.convocatoria)

        if not convocatorias:
            parser.error("usa --all, --convocatoria o --list-convocatorias")

        province_filter = set(args.province) if args.province else None
        for fconvocatoria in convocatorias:
            if args.complete_only:
                result = build_complete_from_existing(output_dir, fconvocatoria)
                print(
                    f"[{fconvocatoria}] CSV completo generado: {result['complete_csv']} "
                    f"({result['complete_rows']} filas; {result['incident_rows']} incidencias)",
                    file=sys.stderr,
                )
                continue
            partidos = client.opciones(
                "partido/cautonoma",
                {
                    "cautonoma": CAUTONOMA_ANDALUCIA,
                    "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
                    "fconvocatoria": fconvocatoria,
                },
            )
            write_json(output_dir / "raw" / "opciones" / f"partidos_{fconvocatoria}.json", partidos)
            rausentes_manifest = None
            if not args.skip_rausentes:
                rausentes_manifest = download_rausentes(
                    client=client,
                    fconvocatoria=fconvocatoria,
                    output_dir=output_dir,
                    province_filter=province_filter,
                    raw_only=args.raw_only,
                    overwrite=args.overwrite or args.resume,
                )
            if args.rausentes_only:
                manifest_path = output_dir / f"manifest_{fconvocatoria}_rausentes.json"
                write_json(
                    manifest_path,
                    {
                        "source": {
                            "home_page": "https://resultadoseleccionesandalucia.es/",
                            "api_base": BASE_URL,
                            "swagger_ui": f"{BASE_URL}/swagger-ui.html",
                            "api_docs": f"{BASE_URL}/api-docs",
                        },
                        "fetched_at": now_utc(),
                        "tconvocatoria": TCONVOCATORIA_AUTONOMICAS,
                        "fconvocatoria": fconvocatoria,
                        "rausentes": rausentes_manifest,
                    },
                )
                continue
            download_convocatoria(
                client=client,
                fconvocatoria=fconvocatoria,
                output_dir=output_dir,
                workers=args.workers,
                province_filter=province_filter,
                limit=args.limit_mesas,
                raw_only=args.raw_only,
                overwrite=args.overwrite,
                resume=args.resume,
            )
            if rausentes_manifest:
                manifest_path = output_dir / f"manifest_{fconvocatoria}.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["rausentes"] = rausentes_manifest
                write_json(manifest_path, manifest)
    except SielApiError as exc:
        print(f"Error consultando la API SIEL: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
