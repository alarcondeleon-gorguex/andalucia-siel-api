import copy
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import download_mesas as dm
from andalucia_siel.storage import raw_path, replace_records, unique_records
from andalucia_siel.quality import assess_record

ELECTION = 202605
PROVINCES = [4, 11, 14, 18, 21, 23, 29, 41]


def context(province=4, mesa="A"):
    return {"tconvocatoria": 5, "fconvocatoria": ELECTION, "provincia_clave": province,
            "provincia_nombre": str(province), "municipio_clave": 1, "municipio_nombre": "Municipio",
            "distrito_clave": 1, "distrito_nombre": "Distrito", "seccion_clave": 1,
            "seccion_valor": "0001", "mesa_clave": 1, "mesa_valor": mesa}


def record(ctx=None):
    ctx = ctx or context()
    params = dm.mesa_params(ctx)
    identity = {k: {"clave": v, "valor": v} for k, v in params.items()}
    return {"context": ctx, "source": {"params": params}, "estado_descarga": "ok",
            "resumen": {"idescrutinio": identity, "censo": 10, "votostotales": 6, "abstenciones": 4,
                        "votosvalidos": 5, "votosnulos": 1, "votosblanco": 1, "votoscandidatura": 4, "nummesas": 1},
            "escrutinio": {"idescrutinio": identity, "votos": [{"conjunto": "Z", "numvotos": 3}, {"conjunto": "A", "numvotos": 1}]}}


class FakeClient:
    base_url = "https://example.invalid"
    def __init__(self):
        self.fetches = 0
    def opciones(self, name, params):
        assert name == "provincia"
        return [{"clave": p, "valor": str(p)} for p in PROVINCES]


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.client = FakeClient()

    def tearDown(self):
        self.temp.cleanup()

    def download(self, contexts, **kwargs):
        def fetch(client, ctx):
            client.fetches += 1
            return record(ctx)
        args = dict(client=self.client, fconvocatoria=ELECTION, output_dir=self.out, workers=1,
                    province_filter=None, limit=None, raw_only=False, overwrite=False, resume=False)
        args.update(kwargs)
        with patch.object(dm, "iter_mesa_contexts", return_value=iter(contexts)) as enumerate_mock, patch.object(dm, "download_mesa", side_effect=fetch):
            dm.download_convocatoria(**args)
        return enumerate_mock

    def seed(self, records, contexts=None, scope=True, limit=None):
        contexts = contexts or [r["context"] for r in records]
        dm.write_catalog_csv(self.out / "processed" / f"mesas_{ELECTION}_catalog.csv", contexts)
        path = raw_path(self.out, f"mesas_{ELECTION}")
        replace_records(path, records)
        if scope:
            dm.write_json(self.out / f"catalog_scope_{ELECTION}.json", {
                **dm.catalog_scope(ELECTION, None, limit), "enumeration_complete": True,
                "expected_provinces": PROVINCES, "catalog_digest": dm.catalog_digest(contexts)})
        return path

    def manifest(self):
        return json.loads((self.out / f"manifest_{ELECTION}.json").read_text())

    def test_limit_then_resume_expands_catalog(self):
        self.download([context()], limit=1)
        m = self.download([context(), context(mesa="B")], resume=True)
        self.assertTrue(m.called)
        self.assertEqual(self.manifest()["counts"]["mesas"], 2)
        self.assertEqual(self.client.fetches, 2)

    def test_legacy_catalog_is_refreshed(self):
        self.seed([record()], scope=False)
        m = self.download([context(), context(mesa="B")], resume=True)
        self.assertTrue(m.called)
        self.assertEqual(self.manifest()["counts"]["mesas"], 2)

    def test_matching_scope_reuses_catalog(self):
        self.seed([record()])
        m = self.download([], resume=True)
        self.assertFalse(m.called)
        self.assertEqual(self.client.fetches, 0)

    def test_changed_province_never_silently_reuses_catalog(self):
        path = self.seed([record()])
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "fuera del ámbito"):
            self.download([context(18)], resume=True, province_filter={18})
        self.assertEqual(path.read_bytes(), original)

    def test_failed_record_only_retried_on_request(self):
        bad = record()
        bad.update(estado_descarga="escrutinio_no_disponible", escrutinio={"votos": []})
        path = self.seed([bad])
        self.download([], resume=True)
        self.assertEqual(self.client.fetches, 0)
        self.download([], resume=True, retry_failed=True)
        records = list(dm.read_ndjson(path))
        self.assertEqual(self.client.fetches, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["estado_descarga"], "ok")

    def test_http_success_with_missing_payload_is_retryable(self):
        bad = record(); bad["resumen"] = {}
        self.seed([bad])
        self.download([], resume=True, retry_failed=True)
        self.assertEqual(self.client.fetches, 1)

    def test_arithmetic_inconsistency_is_not_download_failure(self):
        bad = record(); bad["escrutinio"]["votos"][0]["numvotos"] += 190
        path = self.seed([bad])
        self.download([], resume=True, retry_failed=True)
        self.assertEqual(self.client.fetches, 0)
        self.assertEqual(list(dm.read_ndjson(path))[0], bad)
        self.assertEqual(self.manifest()["counts"]["quality_statuses"], {"inconsistente": 1})

    def test_duplicate_raw_cannot_compensate_missing_mesa(self):
        self.seed([record(), record()], [context(), context(mesa="B")])
        with self.assertRaisesRegex(ValueError, "duplicada"):
            dm.build_complete_from_existing(self.out, ELECTION)

    def test_duplicate_catalog_rejected(self):
        self.seed([record()], [context(), context()])
        with self.assertRaisesRegex(ValueError, "duplicada"):
            dm.build_complete_from_existing(self.out, ELECTION)

    def test_missing_and_foreign_keys_rejected(self):
        self.seed([record()], [context(mesa="B")])
        with self.assertRaisesRegex(ValueError, "ausentes"):
            dm.build_complete_from_existing(self.out, ELECTION)

    def test_other_election_rejected(self):
        bad = record(); bad["context"]["fconvocatoria"] = 202206
        self.seed([bad], [context()])
        with self.assertRaisesRegex(ValueError, "otra convocatoria"):
            dm.build_complete_from_existing(self.out, ELECTION)

    def test_complete_only_requires_catalog(self):
        replace_records(raw_path(self.out, f"mesas_{ELECTION}"), [record()])
        with self.assertRaisesRegex(ValueError, "raw y catálogo"):
            dm.build_complete_from_existing(self.out, ELECTION)

    def test_complete_only_cli_does_not_query_network(self):
        self.seed([record()])
        with patch.object(sys, "argv", ["download_mesas.py", "--convocatoria", str(ELECTION),
                                      "--complete-only", "--output", str(self.out)]), \
                patch.object(dm.SielClient, "get_json", side_effect=AssertionError("No network")):
            self.assertEqual(dm.main(), 0)

    def test_scope_digest_detects_modified_catalog(self):
        self.seed([record()])
        dm.write_catalog_csv(self.out / "processed" / f"mesas_{ELECTION}_catalog.csv", [context(mesa="B")])
        replace_records(raw_path(self.out, f"mesas_{ELECTION}"), [record(context(mesa="B"))])
        with self.assertRaisesRegex(ValueError, "metadatos"):
            dm.build_complete_from_existing(self.out, ELECTION)

    def test_legacy_export_is_explicitly_partial(self):
        self.seed([record()], scope=False)
        result = dm.build_complete_from_existing(self.out, ELECTION)
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(result["complete_csv"].endswith("_parcial.csv"))

    def test_gzip_resume_retry_and_export(self):
        self.download([context()], compress_raw=True, limit=1)
        path = raw_path(self.out, f"mesas_{ELECTION}")
        self.assertEqual(path.suffix, ".gz")
        bad = list(dm.read_ndjson(path))[0]
        bad.update(estado_descarga="escrutinio_no_disponible", escrutinio={"votos": []})
        replace_records(path, [bad])
        self.download([], resume=True, retry_failed=True, limit=1)
        self.assertEqual(len(list(dm.read_ndjson(path))), 1)
        self.assertEqual(list(dm.read_ndjson(path))[0]["estado_descarga"], "ok")
        dm.build_complete_from_existing(self.out, ELECTION)

    def test_corrupt_ndjson_is_not_silently_skipped(self):
        path = self.seed([record()])
        with path.open("a") as f: f.write('{"context":')
        with self.assertRaisesRegex(ValueError, "NDJSON inválido"):
            dm.build_complete_from_existing(self.out, ELECTION)

    def test_full_coverage_requires_all_provinces_and_cera(self):
        self.download([context(p) for p in PROVINCES])
        self.assertFalse(self.manifest()["coverage_complete"])
        cera = []
        for p in PROVINCES:
            r = record(context(p))
            r["context"] = {"tconvocatoria": 5, "fconvocatoria": ELECTION, "ambito": "provincia", "provincia_clave": p}
            cera.append(r)
        replace_records(raw_path(self.out, f"rausentes_{ELECTION}"), cera)
        result = dm.build_complete_from_existing(self.out, ELECTION)
        self.assertTrue(result["coverage_complete"])
        self.assertTrue(result["complete_csv"].endswith("_completo.csv"))
        self.assertEqual(result["complete_rows"], 16)
        self.assertFalse((self.out / "processed" / f"andalucia_{ELECTION}_parcial.csv").exists())

    def test_skip_cera_never_exports_stale_cera(self):
        self.download([context()])
        r = record(); r["context"] = {"tconvocatoria": 5, "fconvocatoria": ELECTION, "ambito": "provincia", "provincia_clave": 4}
        replace_records(raw_path(self.out, f"rausentes_{ELECTION}"), [r])
        result = dm.build_complete_from_existing(self.out, ELECTION, include_cera=False)
        self.assertEqual(result["complete_rows"], 1)

    def test_stable_party_columns_and_unknown_not_zero(self):
        good = record(); bad = record(context(mesa="B"))
        bad.update(estado_descarga="escrutinio_no_disponible", escrutinio={"votos": []})
        self.seed([good, bad])
        result = dm.build_complete_from_existing(self.out, ELECTION)
        with open(result["complete_csv"]) as f:
            reader = csv.DictReader(f); rows = list(reader)
            self.assertEqual(reader.fieldnames[-2:], ["A", "Z"])
        self.assertEqual(rows[0]["Z"], "3")
        self.assertEqual(rows[1]["Z"], "")
        self.assertEqual(rows[1]["estado_calidad"], "incompleto")

    def test_cera_resume_preserves_failures_until_explicit_retry(self):
        ctx = {"tconvocatoria": 5, "fconvocatoria": ELECTION, "ambito": "provincia", "provincia_clave": 4}
        bad = {"context": ctx, "estado_descarga": "cera_no_disponible", "resumen": {}, "escrutinio": {}}
        path = raw_path(self.out, f"rausentes_{ELECTION}"); replace_records(path, [bad])
        good = record(); good["context"] = ctx
        with patch.object(dm, "download_rausentes_record", return_value=good) as fetch:
            dm.download_rausentes(self.client, ELECTION, self.out, {4}, False, False, resume=True)
            fetch.assert_not_called()
            dm.download_rausentes(self.client, ELECTION, self.out, {4}, False, False, resume=True, retry_failed=True)
            fetch.assert_called_once()
        self.assertEqual(len(list(dm.read_ndjson(path))), 1)

    def test_null_vote_list_exports_as_incomplete_without_inventing_zeros(self):
        good = record(); bad = record(context(mesa="B"))
        bad["escrutinio"]["votos"] = None
        self.seed([good, bad])
        result = dm.build_complete_from_existing(self.out, ELECTION)
        with open(result["complete_csv"]) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[1]["estado_calidad"], "incompleto")
        self.assertEqual(rows[1]["Z"], "")

    def test_real_historical_anomalies_and_2026_values_are_preserved(self):
        cases = json.loads((ROOT / "tests" / "fixtures" / "quality_cases.json").read_text())
        for case in cases:
            with self.subTest(case=case["case"]):
                before = copy.deepcopy(case["record"])
                self.assertEqual(assess_record(case["record"])["estado_calidad"], case["expected"])
                self.assertEqual(case["record"], before)

    def test_zero_vote_summary_fallback_remains_valid(self):
        zero = record()
        for key in ("votostotales", "votosvalidos", "votosnulos", "votosblanco", "votoscandidatura"):
            zero["resumen"][key] = 0
        zero["resumen"]["abstenciones"] = zero["resumen"]["censo"]
        zero["escrutinio"]["votos"] = []
        zero["estado_descarga"] = "ok_sin_votos"
        self.assertEqual(assess_record(zero)["estado_calidad"], "ok")

    def test_empty_payload_and_wrong_identity_are_flagged(self):
        bad = record(); bad["resumen"] = {}
        self.assertEqual(assess_record(bad)["estado_calidad"], "incompleto")
        bad = record(); bad["resumen"]["idescrutinio"]["provincia"]["clave"] = 18
        self.assertIn("identidad provincia", assess_record(bad)["incidencias_calidad"])
        bad = record(); bad["context"]["provincia_clave"] = 18
        self.assertIn("identidad provincia", assess_record(bad)["incidencias_calidad"])


if __name__ == "__main__":
    unittest.main()
