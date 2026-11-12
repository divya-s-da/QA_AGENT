"""User accounts: hashing, verification, sessions, and expiry."""
import os, tempfile, time, unittest
from v2 import accounts as A
from v2.accounts import Accounts


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.ac = Accounts(os.path.join(tempfile.mkdtemp(), "users.db"))

    def test_create_and_verify(self):
        self.assertTrue(self.ac.create_user("alice", "hunter2pass")[0])
        self.assertTrue(self.ac.verify("alice", "hunter2pass"))
        self.assertFalse(self.ac.verify("alice", "wrong"))
        self.assertFalse(self.ac.verify("ghost", "anything"))

    def test_validation(self):
        self.assertFalse(self.ac.create_user("al", "longenough1")[0])      # name too short
        self.assertFalse(self.ac.create_user("a b", "longenough1")[0])     # space in name
        self.assertFalse(self.ac.create_user("bob", "short")[0])           # password too short
        self.ac.create_user("bob", "longenough1")
        self.assertFalse(self.ac.create_user("bob", "another12")[0])       # duplicate

    def test_password_not_stored_plaintext(self):
        self.ac.create_user("carol", "sekritpass1")
        row = self.ac._db.execute("SELECT pw_hash FROM users WHERE username='carol'").fetchone()
        self.assertNotIn(b"sekritpass1", row[0])

    def test_sessions(self):
        self.ac.create_user("dan", "password12")
        self.assertIsNone(self.ac.login("dan", "wrong"))
        tok = self.ac.login("dan", "password12")
        self.assertEqual(self.ac.user_for(tok), "dan")
        self.assertIsNone(self.ac.user_for("garbage"))
        self.ac.logout(tok)
        self.assertIsNone(self.ac.user_for(tok))

    def test_session_expiry(self):
        self.ac.create_user("eve", "password12")
        old = A.SESSION_TTL
        try:
            A.SESSION_TTL = 0.01
            tok = self.ac.login("eve", "password12")
        finally:
            A.SESSION_TTL = old
        time.sleep(0.05)
        self.assertIsNone(self.ac.user_for(tok))


if __name__ == "__main__":
    unittest.main()
