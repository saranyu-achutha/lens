"""Named client logins (lens/logins.py) and the Console's login check. Run from the app folder:

    python3 -m unittest lens.tests.test_logins -v
"""
import base64
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from lens import config, logins, studio


class Logins(unittest.TestCase):
    def setUp(self):
        self.env = Path(tempfile.mkdtemp()) / ".env"
        self.env.write_text("STUDIO_PASSWORD=shared-client-pw\nDEVELOPER_USERNAME=mlens-dev\nDEVELOPER_PASSWORD=dev-pw\n")
        self._old = config.ENV_FILE
        config.ENV_FILE = self.env
        logins._CACHE.update(at=0.0, users={}, ok={})

    def tearDown(self):
        config.ENV_FILE = self._old
        logins._CACHE.update(at=0.0, users={}, ok={})

    def add(self, user):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            logins.main(["add", user])
        return out.getvalue().split("password ")[1].split()[0]

    def test_add_verify_remove(self):
        pw = self.add("timesnow")
        self.assertNotIn(pw, self.env.read_text())            # only the hash is stored
        self.assertTrue(logins.verify("timesnow", pw))
        self.assertFalse(logins.verify("timesnow", pw + "x"))
        self.assertFalse(logins.verify("someoneelse", pw))
        with contextlib.redirect_stdout(io.StringIO()):
            logins.main(["remove", "timesnow"])
        self.assertFalse(logins.verify("timesnow", pw))       # removal takes effect at once (memory cleared)

    def test_strong_password(self):
        p = logins.generate_password()
        self.assertEqual(len(p), 20)
        self.assertTrue(any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p))

    def test_developer_name_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            logins.main(["add", "mlens-dev"])

    def test_console_roles(self):
        pw = self.add("timesnow")
        studio.PASSWORD, studio.DEV_LOGIN = "shared-client-pw", ("mlens-dev", "dev-pw")

        class Fake(studio.Handler if hasattr(studio, "Handler") else object):
            def __init__(self, user, password):
                self.headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}
                self.sent = None
            def send_response(self, code): self.sent = code
            def send_header(self, *a): pass
            def end_headers(self): pass

        handler = next(v for v in vars(studio).values() if isinstance(v, type) and hasattr(v, "_role"))
        role = lambda u, p: handler._role(Fake(u, p))
        self.assertEqual(role("timesnow", pw), "client")
        self.assertIsNone(role("timesnow", "wrong"))
        self.assertEqual(role("mlens-dev", "dev-pw"), "developer")
        self.assertEqual(role("anyone", "shared-client-pw"), "client")   # the shared client login still works


if __name__ == "__main__":
    unittest.main()
