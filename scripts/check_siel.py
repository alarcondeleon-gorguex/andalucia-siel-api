#!/usr/bin/env python3
"""Bounded live checks; never enumerate a complete polling-station catalog."""
from __future__ import annotations

from collections import Counter
from typing import Any

from download_mesas import (
    SielApiError, SielClient, assess_record, download_mesa,
    now_utc, sort_clave_valor, write_json,
)


def compare_parties(total: dict, provinces: list[dict]) -> dict:
    def votes(records):
        result = Counter()
        for record in records:
            for vote in record["escrutinio"].get("votos", []):
                result[vote["conjunto"]] += vote["numvotos"]
        return result
    tv, pv = votes([total]), votes(provinces)
    diffs = {k: tv[k] - pv[k] for k in tv.keys() | pv.keys() if tv[k] != pv[k]}
    unmatched = sorted(k for k in tv.keys() ^ pv.keys() if tv[k] or pv[k])
    status = "etiquetas_no_comparables" if diffs and unmatched else "inconsistente" if diffs else "ok"
    return {"party_differences": diffs, "unmatched_party_labels": unmatched,
            "party_comparison": status}


def run_check(client: SielClient, elections: list[int], output_dir,
              province_filter: set[int] | None = None, include_cera: bool = True,
              request_budget: int = 1600) -> int:
    calls = 0
    cache = {}

    class Reader:
        base_url = client.base_url
        build_url = client.build_url
        opciones = SielClient.opciones
        resumen_mesa = SielClient.resumen_mesa
        escrutinio_mesa = SielClient.escrutinio_mesa

        def get_json(self, path, params=None):
            nonlocal calls
            key = (path, tuple(sorted((params or {}).items())))
            if key in cache:
                return cache[key]
            if calls >= request_budget:
                raise SielApiError(f"Límite de {request_budget} consultas de comprobación alcanzado")
            calls += 1
            data = client.get_json(path, params)
            cache[key] = data
            return data

    c = Reader()
    provinces = sort_clave_valor(c.opciones("provincia", {"cautonoma": 1}))
    provinces = [p for p in provinces if province_filter is None or int(p["clave"]) in province_filter]
    results = []
    errors = 0
    issue_types: Counter = Counter()
    for election in elections:
        base = {"cautonoma": 1, "tconvocatoria": 5, "fconvocatoria": election}
        aggregates = []
        samples = []
        for province in [None, *provinces]:
            params = {**base, **({"provincia": province["clave"]} if province else {})}
            scope = "provincia" if province else "cautonoma"
            for cera in ([False, True] if include_cera else [False]):
                extra = "rausentes/" if cera else ""
                record: dict[str, Any] = {"context": {**params, "ambito": scope, "cera": cera},
                                          "source": {"params": params}, "estado_descarga": "ok"}
                for field, prefix in (("resumen", "/v1/resumen/"), ("escrutinio", "/v1/escrutinio/ambito/")):
                    path = prefix + extra + scope
                    record["source"][field + "_url"] = c.build_url(path, params)
                    try:
                        record[field] = c.get_json(path, params)
                    except SielApiError as exc:
                        record[field] = {}
                        record["estado_descarga"] = "cera_no_disponible" if cera else "no_disponible"
                        record.setdefault("errors", []).append({"endpoint": path, "message": str(exc), "status_code": exc.status_code})
                record.update(assess_record(record))
                record["known_unavailable"] = bool(cera and election == 198205
                    and record.get("errors") and all(e.get("status_code") == 500 for e in record["errors"]))
                if record["estado_calidad"] != "ok" and not record["known_unavailable"]:
                    errors += 1
                    issue_types["source_" + record["estado_calidad"]] += 1
                aggregates.append(record)
        for province in provinces:
            params = {**base, "provincia": province["clave"]}
            skipped = []
            found = None
            try:
                municipalities = sort_clave_valor(c.opciones("municipio", params))
                for municipality in municipalities[:5]:
                    selected = {"municipio": municipality}
                    p = {**params, "municipio": municipality["clave"]}
                    for option in ("distrito", "seccion", "mesa"):
                        choices = sort_clave_valor(c.opciones(option, p))
                        if not choices:
                            skipped.append({"municipio": municipality, "empty": option})
                            break
                        item = choices[0]
                        selected[option] = item
                        p[option] = item["valor"] if option in {"seccion", "mesa"} else item["clave"]
                    else:
                        context = {"tconvocatoria": 5, "fconvocatoria": election,
                                   "provincia_clave": province["clave"], "provincia_nombre": province["valor"]}
                        for key, item in selected.items():
                            context[key + "_clave"] = item["clave"]
                            context[key + ("_valor" if key in {"seccion", "mesa"} else "_nombre")] = item["valor"]
                        found = download_mesa(c, context)
                        found.update(assess_record(found))
                        break
                if found is None:
                    raise SielApiError("No se encontró una mesa en los cinco municipios de prueba")
                if found["estado_calidad"] != "ok":
                    errors += 1
                    issue_types["source_" + found["estado_calidad"]] += 1
                samples.append({"provincia": province, "skipped": skipped, "record": found})
            except SielApiError as exc:
                errors += 1
                issue_types["sample_unavailable"] += 1
                samples.append({"provincia": province, "skipped": skipped, "error": str(exc)})
        reconciliation = []
        if province_filter is None and len(provinces) == 8:
            for cera in ([False, True] if include_cera else [False]):
                group = [r for r in aggregates if r["context"]["cera"] == cera]
                if any(r["estado_descarga"] != "ok" or r["estado_calidad"] == "incompleto" for r in group):
                    continue
                total = next(r for r in group if r["context"]["ambito"] == "cautonoma")
                subs = [r for r in group if r["context"]["ambito"] == "provincia"]
                diffs = {}
                for field in ("censo", "votostotales", "abstenciones", "votosvalidos", "votosnulos", "votosblanco", "votoscandidatura"):
                    values = [r["resumen"].get(field) for r in group]
                    if all(isinstance(v, int) for v in values):
                        delta = total["resumen"][field] - sum(r["resumen"][field] for r in subs)
                        if delta:
                            diffs[field] = delta
                parties = compare_parties(total, subs)
                if diffs or parties["party_differences"]:
                    errors += 1
                    issue_types["territorial_summary" if diffs else parties["party_comparison"]] += 1
                reconciliation.append({"cera": cera, "summary_differences": diffs, **parties})
        results.append({"fconvocatoria": election, "aggregates": aggregates,
                        "samples": samples, "reconciliation": reconciliation})
        print(f"[{election}] comprobados {len(aggregates)} agregados y {sum('record' in s for s in samples)} mesas", flush=True)
    report = {"checked_at": now_utc(), "api_base": client.base_url, "tls_verified": client.verify_tls,
              "queries": calls, "request_budget": request_budget, "elections": elections,
              "issues": errors, "issues_by_type": dict(issue_types), "results": results,
              "coverage_complete": False,
              "note": "Muestra acotada; no certifica todas las mesas. CERA 1982 HTTP 500 se identifica como ausencia conocida."}
    path = output_dir / "check_siel.json"
    write_json(path, report)
    print(f"Informe: {path}; consultas: {calls}; incidencias: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    import sys
    from download_mesas import main
    sys.argv.insert(1, "--check")
    raise SystemExit(main())
