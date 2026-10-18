import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_siel import run_check
from test_downloader import record


class CheckClient:
    base_url = "https://example.invalid"
    verify_tls = True

    def __init__(self, unavailable=False, bad_arithmetic=False):
        self.paths = []
        self.unavailable = unavailable
        self.bad_arithmetic = bad_arithmetic

    def build_url(self, path, params=None):
        return self.base_url + path

    def get_json(self, path, params=None):
        from download_mesas import SielApiError
        self.paths.append(path)
        if self.unavailable and "rausentes" in path:
            raise SielApiError("HTTP 500", status_code=500)
        if "/opciones/" in path:
            name = path.split("/")[-1]
            values = {
                "provincia": [{"clave": 4, "valor": "Almería"}],
                "municipio": [{"clave": 1, "valor": "Primero"}, {"clave": 2, "valor": "Segundo"}],
                "distrito": [] if params["municipio"] == 1 else [{"clave": 1, "valor": "Distrito"}],
                "seccion": [{"clave": 1, "valor": "0001"}],
                "mesa": [{"clave": 1, "valor": "A"}],
            } if name not in {"provincia", "municipio"} else {
                "provincia": [{"clave": 4, "valor": "Almería"}],
                "municipio": [{"clave": 1, "valor": "Primero"}, {"clave": 2, "valor": "Segundo"}],
            }
            return values[name]
        payload = copy.deepcopy(record()["resumen" if "/resumen/" in path else "escrutinio"])
        payload["idescrutinio"] = {k: {"clave": v, "valor": v} for k, v in params.items()}
        if self.bad_arithmetic and "votoscandidatura" in payload:
            payload["votoscandidatura"] += 54
        return payload


class CheckTests(unittest.TestCase):
    def test_bounded_sample_falls_back_after_empty_historical_municipality(self):
        with tempfile.TemporaryDirectory() as t:
            client = CheckClient()
            code = run_check(client, [202605], Path(t), {4})
            result = json.loads((Path(t) / "check_siel.json").read_text())
            sample = result["results"][0]["samples"][0]
            self.assertEqual(code, 0)
            self.assertEqual(sample["record"]["context"]["municipio_clave"], 2)
            self.assertEqual(len(sample["skipped"]), 1)
            self.assertEqual(client.paths.count("/v1/resumen/mesa"), 1)
            self.assertFalse(result["coverage_complete"])
            self.assertLess(result["queries"], 25)

    def test_known_cera_1982_unavailability_reported_without_hiding_other_errors(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(run_check(CheckClient(unavailable=True), [198205], Path(t), {4}), 0)
            result = json.loads((Path(t) / "check_siel.json").read_text())
            self.assertEqual(sum(r["known_unavailable"] for r in result["results"][0]["aggregates"]), 2)
            self.assertEqual(run_check(CheckClient(unavailable=True), [202605], Path(t), {4}), 1)

    def test_arithmetic_failure_returns_nonzero_with_report(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(run_check(CheckClient(bad_arithmetic=True), [202605], Path(t), {4}), 1)
            self.assertGreater(json.loads((Path(t) / "check_siel.json").read_text())["issues"], 0)

    def test_request_budget_is_enforced_and_reported(self):
        with tempfile.TemporaryDirectory() as t:
            client = CheckClient()
            self.assertEqual(run_check(client, [202605], Path(t), {4}, request_budget=3), 1)
            result = json.loads((Path(t) / "check_siel.json").read_text())
            self.assertEqual(result["queries"], 3)
            self.assertEqual(len(client.paths), 3)


if __name__ == "__main__":
    unittest.main()
