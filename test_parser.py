import unittest
from parser import clean_numbers, chunks


class ParserTests(unittest.TestCase):
    def test_basic_list(self):
        result, errors = clean_numbers("26008178-03.11.2026\n26009490-20.11.2026")
        self.assertEqual(result, ["26008178", "26009490"])
        self.assertEqual(errors, [])

    def test_duplicates_and_leading_zeros(self):
        result, errors = clean_numbers("00001234-03.11.2026\n00001234-03.11.2026")
        self.assertEqual(result, ["00001234", "00001234"])
        self.assertEqual(errors, [])

    def test_plain_ids_already_clean(self):
        self.assertEqual(clean_numbers("26008178\n26008179"), (["26008178", "26008179"], []))

    def test_blank_lines_are_ignored_without_reordering(self):
        self.assertEqual(clean_numbers("\n1-01.01.2026\n\n2-02.02.2026\n"), (["1", "2"], []))

    def test_bad_date(self):
        self.assertEqual(clean_numbers("1-31.02.2026\n2-03.11.2026"), (["2"], [1]))

    def test_garbage_identified_by_line(self):
        self.assertEqual(clean_numbers("1-03.11.2026\nabc\n2-20.11.2026"), (["1", "2"], [2]))

    def test_chunk_preserves_full_list(self):
        ids = [str(n) for n in range(1000)]
        pieces = chunks(ids, 100)
        self.assertEqual("\n".join(pieces), "\n".join(ids))
        self.assertTrue(all(len(p) <= 100 for p in pieces))


if __name__ == "__main__":
    unittest.main()
