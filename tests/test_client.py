import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from andalucia_siel.client import SielApiError, SielClient


def response(status, retry_after=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps({"ok": True}).encode()
    if retry_after:
        r.headers["Retry-After"] = retry_after
    return r


class ClientTests(unittest.TestCase):
    def test_tls_enabled_by_default(self):
        self.assertTrue(SielClient().session().verify)
        self.assertFalse(SielClient(verify_tls=False).session().verify)

    def test_retry_after_respected(self):
        c = SielClient(retries=1)
        session = Mock()
        session.get.side_effect = [response(429, "3"), response(200)]
        with patch.object(SielClient, "session", return_value=session), patch("andalucia_siel.client.time.sleep") as sleep:
            self.assertEqual(c.get_json("/test"), {"ok": True})
            sleep.assert_called_once_with(3.0)

    def test_http_date_retry_after_respected(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        c = SielClient(retries=1)
        session = Mock()
        session.get.side_effect = [response(503, format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))), response(200)]
        with patch.object(SielClient, "session", return_value=session), patch("andalucia_siel.client.time.sleep") as sleep:
            c.get_json("/test")
            self.assertGreater(sleep.call_args.args[0], 28)

    def test_permanent_http_failure_is_not_retried(self):
        c = SielClient(retries=3)
        session = Mock(); session.get.return_value = response(404)
        with patch.object(SielClient, "session", return_value=session), patch("andalucia_siel.client.time.sleep") as sleep:
            with self.assertRaises(SielApiError): c.get_json("/test")
            self.assertEqual(session.get.call_count, 1)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
