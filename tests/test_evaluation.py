import unittest
from evaluation.score import normalize, score

class EvaluationTests(unittest.TestCase):
    def test_number_meaning_is_not_removed(self):
        for a,b in [('70%','70'),('27.75','2775'),('4-7','47'),('有投票权','没有投票权'),('A类股','B类股')]:
            with self.subTest(a=a):self.assertNotEqual(normalize(a),normalize(b))
        self.assertEqual(normalize('1,109'),normalize('1109'))

    def test_overlap_cannot_inflate_score(self):
        with self.assertRaises(ValueError):
            score([{'start':0,'end':2,'segments':[]},{'start':1,'end':3,'segments':[]}],[])

    def test_insertions_and_deletions_count(self):
        window={'start':0,'end':2,'segments':[{'text':'甲乙丙'}]}
        result=score([window],[{'start':0,'end':1,'text':'甲丁乙'}])
        self.assertEqual(result['errors'],2)
        self.assertAlmostEqual(result['accuracy'],1/3)
