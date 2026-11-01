import unittest

from promptvault.redact import redact

# All secrets below are synthetic.
LEAKS = [
    "my mail pwd is \"Tulip4Lantern88\"",
    "mein Passwort ist Fahrrad#2024",
    "das Kennwort lautet Zebra-Moon-77",
    "Passwort für den Router ist hunter2hunter",
    "password: S3cretPass!",
    "the api key is sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX1234",
    "use ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123",
    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AWS AKIAABCDEFGHIJKLMNOP",
    "postgres://app:Pa55word99@db.example.com/x",
    "DATABASE_PASSWORD = 'correcthorsebattery'",
    "Authorization: Bearer abcdefghijklmnop1234567890",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n-----END OPENSSH PRIVATE KEY-----",
    "my pin is 4821",
    "login is me@example.com pass: Wolke7Wolke",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstu",
]
LEAKED_VALUES = [
    "Tulip4Lantern88", "Fahrrad#2024", "Zebra-Moon-77", "hunter2hunter", "S3cretPass!",
    "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX1234", "sk-ant-api03", "ghp_abc", "AKIAABCDEFGHIJKLMNOP",
    "Pa55word99", "correcthorsebattery", "abcdefghijklmnop1234567890", "AAAAB3Nza", "4821", "Wolke7Wolke",
    "eyJhbGciOiJIUzI1NiJ9",
]
SAFE = [
    "the token is invalid, please refresh it",
    "pwd is /Users/me/Downloads",
    "the password is wrong again",
    "secret = os.environ['SECRET']",
    "API_KEY=$OPENAI_API_KEY",
    "what is the token limit for haiku?",
    "explain how the password reset flow works",
    "the secret: to be patient",
    "commit 3f2a9c1d4e5b6a7f8e9d0c1b2a3f4e5d6c7b8a9f",
    "my token is expired",
]


class RedactTest(unittest.TestCase):
    def test_leaks_are_redacted(self):
        for text, value in zip(LEAKS, LEAKED_VALUES):
            out, n, _ = redact(text)
            self.assertNotIn(value, out, text)
            self.assertGreater(n, 0, text)
            self.assertIn("«redacted:", out)

    def test_safe_text_unchanged(self):
        for text in SAFE:
            out, n, _ = redact(text)
            self.assertEqual(out, text)
            self.assertEqual(n, 0, text)

    def test_punctuation_kept(self):
        out, _, _ = redact("mein Passwort ist Fahrrad#2024.")
        self.assertTrue(out.endswith("»."), out)

    def test_idempotent(self):
        once, _, _ = redact(LEAKS[0])
        twice, n, _ = redact(once)
        self.assertEqual(once, twice)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
