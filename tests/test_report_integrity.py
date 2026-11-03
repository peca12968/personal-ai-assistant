"""Do not turn inconsistent evidence into a favorable evaluation score."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import review_understanding as review
from scripts.evaluate_understanding import load_report, save_report


def report():
    return {'format_version':1,'fingerprint':{'suite_sha256':'a'*64,'runner_sha256':'b'*64,
            'source_sha256':'c'*64,'prompt_sha256':'d'*64},'case_ids':['one','two'],
            'results':[{'id':id,'category':'synthetic','prompt':'合成問題'+id,'answer':'合成答案',
                        'criteria':['保留條件','結果正確'],'source_sha256':'e'*64,'human_grade':None}
                       for id in ('one','two')]}


def grade(r, id='one', passed=True, reviewer='development_agent'):
    review.apply_reviews(r,[{'id':id,'criteria_pass':[True,passed],'note':'合成逐項評讀'}],reviewer)
    return r


class ReportIntegrityTests(unittest.TestCase):
    def test_duplicate_unknown_and_missing_case_identity_cannot_fake_completion(self):
        for changed in ('duplicate_result','unknown_result','duplicate_ids','empty_ids','nonstring_id','overfull'):
            r=report()
            if changed=='duplicate_result':r['results'][1]=copy.deepcopy(r['results'][0])
            elif changed=='unknown_result':r['results'][1]['id']='other'
            elif changed=='duplicate_ids':r['case_ids']=['one','one']
            elif changed=='empty_ids':r['case_ids']=[]
            elif changed=='nonstring_id':r['case_ids']=[1,'two']
            else:r['results'].append(copy.deepcopy(r['results'][0]))
            original=copy.deepcopy(r)
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):review.summarize(r)
                with self.assertRaises(ValueError):grade(r)
                self.assertEqual(r,original)

    def test_empty_criteria_missing_answer_and_malformed_rows_are_rejected(self):
        for change in ({'criteria':[]},{'criteria':['']},{'criteria':[1]}, {'category':[]},
                       {'answer':None},{'reviews':None},{'mechanical_checks':{'check':1}}):
            r=report();r['results'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):review.summarize(r)
        r=report();del r['results'][0]['answer']
        with self.assertRaises(ValueError):review.summarize(r)
        for r in ([],{}, {'case_ids':['x'],'results':[None]}):
            with self.assertRaises(ValueError):review.summarize(r)

    def test_partial_empty_and_error_results_are_counted_without_fake_answers(self):
        r=report();r['case_ids'].append('three');r['results'][1].update(answer='',error='合成引擎失敗')
        s=review.summarize(r)
        self.assertEqual((s['collected'],s['not_collected'],s['answered'],s['not_answered']),(2,1,1,2))
        self.assertEqual(s['empty_answers'],1);self.assertEqual(s['collection_errors'],1)
        with self.assertRaises(ValueError):grade(r)
        r['case_ids'].pop()
        with self.assertRaises(ValueError):grade(r,'two',True)
        grade(r,'two',False)
        self.assertEqual(review.summarize(r)['reviews'],{'pass':0,'fail':1,'pending':1})
        self.assertEqual(review.compare(r,r)['pending_ids'],['one'])

    def test_every_saved_review_is_validated_and_reviewer_groups_are_separate(self):
        r=grade(grade(report(),passed=False),'two',True,'user')
        grade(r,reviewer='independent_reviewer')
        s=review.summarize(r)
        self.assertEqual(s['by_reviewer']['user']['pass'],1)
        self.assertEqual(s['by_reviewer']['independent_reviewer']['pass'],1)
        self.assertNotIn('development_agent',s['by_reviewer'])
        for key,value in [('reviewer','automatic_human'),('criteria_pass',[True,1]),('result','pass'),('note',''),('reviewed_at',None)]:
            changed=copy.deepcopy(r);changed['results'][0]['reviews'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):review.summarize(changed)
        self.assertIsNone(r['results'][0]['human_grade'])

    def test_changed_criteria_prompt_category_runner_fixture_or_input_blocks_pairing(self):
        a=grade(report(),passed=False)
        for scope,key,value in [('row','criteria',['別的標準','結果正確']),('row','prompt','不同問題'),
                                ('row','category','different'),('row','source_sha256','f'*64),
                                ('fingerprint','runner_sha256','f'*64),('fingerprint','fixture_sha256','f'*64),
                                ('fingerprint','other_input_sha256','f'*64),('report','mode','different')]:
            b=copy.deepcopy(a)
            target=b['results'][0] if scope=='row' else b['fingerprint'] if scope=='fingerprint' else b
            target[key]=value
            b['results'][0].pop('reviews',None)
            grade(b)
            with self.subTest(scope=scope,key=key),self.assertRaises(ValueError):review.compare(a,b)
        b=copy.deepcopy(a);del b['results'][0]['source_sha256']
        with self.assertRaises(ValueError):review.compare(a,b)

    def test_source_prompt_changes_are_reported_and_reviewer_change_is_visible(self):
        a=grade(report(),passed=False)
        b=report();b['fingerprint']['source_sha256']='1'*64;b['fingerprint']['prompt_sha256']='2'*64
        grade(b,reviewer='user')
        paired=review.compare(a,b)
        self.assertEqual(paired['compared'],1);self.assertEqual(paired['pending_ids'],['two'])
        self.assertEqual(paired['transitions'][0]['before_reviewer'],'development_agent')
        self.assertEqual(paired['transitions'][0]['after_reviewer'],'user')
        self.assertEqual(paired['reviewer_changed_ids'],['one'])
        self.assertEqual(set(paired['changed_runtime_fingerprints']),{'source_sha256','prompt_sha256'})
        self.assertFalse(paired['suite_verified'])
        self.assertIn('not',paired['note'])

    def test_actual_suite_bytes_bind_ids_rubrics_and_prompts(self):
        r=report();cases=[{k:v for k,v in row.items() if k in ('id','category','prompt','criteria')} for row in r['results']]
        raw=json.dumps(cases,ensure_ascii=False).encode('utf-8')
        r['fingerprint']['suite_sha256']=hashlib.sha256(raw).hexdigest()
        self.assertTrue(review.summarize(r,suite_bytes=raw)['suite_verified'])
        self.assertTrue(review.compare(r,r,suite_bytes=raw)['suite_verified'])
        for field,value in [('criteria',['被修改','結果正確']),('prompt','被修改'),('category','changed')]:
            changed=copy.deepcopy(r);changed['results'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):review.compare(changed,changed,suite_bytes=raw)
        with self.assertRaises(ValueError):review.summarize(r,suite_bytes=raw+b' ')
        changed=copy.deepcopy(r);del changed['results'][0]['prompt']
        self.assertTrue(review.summarize(changed,suite_bytes=raw)['suite_verified'])
        self.assertIn('one',review.summarize(changed,suite_bytes=raw)['missing_prompt_ids'])

    def test_invalid_review_batch_is_atomic_and_inputs_are_not_rewritten(self):
        r=report();original=copy.deepcopy(r)
        for second in ({'id':'two','criteria_pass':[True],'note':'不完整'},None,
                       {'id':'two','criteria_pass':[True,True],'note':''}):
            with self.assertRaises(ValueError):
                review.apply_reviews(r,[{'id':'one','criteria_pass':[True,True],'note':'合成核對'},second],'development_agent')
            self.assertEqual(r,original)
        grade(r)
        self.assertEqual(r['results'][0]['answer'],original['results'][0]['answer'])
        self.assertEqual(r['fingerprint'],original['fingerprint'])

    def test_cli_rejects_duplicate_json_keys_nonfinite_and_comparison_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);path=folder/'report.json';other=folder/'other.json';reviews=folder/'reviews.json'
            valid_json = json.dumps(report(), ensure_ascii=False)
            duplicate = valid_json.replace('"answer": "合成答案"', '"answer": "原答案", "answer": "合成答案"', 1)
            for text in (duplicate, valid_json[:-1] + ',"unused":NaN}'):
                path.write_text(text,encoding='utf-8');before=path.read_bytes()
                with patch('sys.argv',['review','--report',str(path)]),patch('sys.stderr',new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as raised:review.main()
                self.assertEqual(raised.exception.code,1);self.assertEqual(path.read_bytes(),before)
            r=report();save_report(path,r);before=path.read_bytes()
            changed=copy.deepcopy(r);changed['results'][0]['criteria']=['changed','結果正確'];save_report(other,changed)
            reviews.write_text(json.dumps([{'id':'one','criteria_pass':[True,True],'note':'合成核對'}]),encoding='utf-8')
            with patch('sys.argv',['review','--report',str(path),'--reviews',str(reviews),'--reviewer','development_agent','--compare',str(other)]),patch('sys.stderr',new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:review.main()
            self.assertEqual(raised.exception.code,1);self.assertEqual(path.read_bytes(),before)

    def test_file_size_bound_and_successful_suite_checked_cli_review(self):
        from scripts import evaluation_reports
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);path=folder/'report.json';suite=folder/'suite.json';reviews=folder/'reviews.json'
            r=report();cases=[{k:v for k,v in row.items() if k in ('id','category','prompt','criteria')} for row in r['results']]
            raw=json.dumps(cases,ensure_ascii=False).encode('utf-8');suite.write_bytes(raw)
            r['fingerprint']['suite_sha256']=hashlib.sha256(raw).hexdigest();save_report(path,r)
            before=path.read_bytes()
            with patch.object(evaluation_reports,'MAX_JSON_BYTES',100),self.assertRaises(ValueError):
                evaluation_reports.load_json(path)
            self.assertEqual(path.read_bytes(),before)
            reviews.write_text(json.dumps([{'id':'one','criteria_pass':[True,True],'note':'合成核對'}]),encoding='utf-8')
            with patch('sys.argv',['review','--report',str(path),'--reviews',str(reviews),'--reviewer','user','--suite',str(suite)]),patch('sys.stdout',new_callable=io.StringIO) as output:
                review.main()
            summary=json.loads(output.getvalue())
            self.assertTrue(summary['suite_verified']);self.assertEqual(summary['by_reviewer']['user']['pass'],1)
            saved=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['results'][0]['answer'],r['results'][0]['answer'])
            self.assertIsNone(saved['results'][0]['human_grade'])
            self.assertNotIn('reviews',saved['results'][1])

    def test_resume_rejects_invalid_case_selection_and_ambiguous_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'report.json'
            for ids in ([],['one','one'],[1]):
                with self.assertRaises(ValueError):load_report(p,{},ids,False)
            p.write_text('{"fingerprint":{},"case_ids":["one"],"results":[],"results":[{"id":"one"}]}',encoding='utf-8')
            before=p.read_bytes()
            with self.assertRaises(ValueError):load_report(p,{},['one'],True)
            self.assertEqual(p.read_bytes(),before)

    def test_review_binding_detects_changed_evidence_and_keeps_legacy_explicit(self):
        r=grade(report());original=copy.deepcopy(r)
        self.assertEqual(review.summarize(r)['bound_review_ids'],['one'])
        for scope,key,value in [('row','answer','事後改寫答案'),('row','criteria',['新標準','結果正確']),
                                ('row','context',{'new':'context'}),('row','source_sha256','f'*64),
                                ('report','git_head','changed'),('fingerprint','source_sha256','f'*64)]:
            changed=copy.deepcopy(r)
            target=changed['results'][0] if scope=='row' else changed['fingerprint'] if scope=='fingerprint' else changed
            target[key]=value
            with self.subTest(scope=scope,key=key),self.assertRaises(ValueError):review.summarize(changed)
            with self.assertRaises(ValueError):grade(changed)
        grade(r,reviewer='user')
        self.assertEqual(r['results'][0]['reviews'][0]['evidence_sha256'],r['results'][0]['reviews'][1]['evidence_sha256'])
        self.assertEqual(review.summarize(r)['by_reviewer']['user']['pass'],1)
        legacy=copy.deepcopy(original);del legacy['results'][0]['reviews'][0]['evidence_sha256']
        before=copy.deepcopy(legacy)
        self.assertEqual(review.summarize(legacy)['unbound_review_ids'],['one'])
        self.assertEqual(legacy,before)
        grade(legacy,reviewer='user')
        self.assertNotIn('evidence_sha256',legacy['results'][0]['reviews'][0])
        self.assertIn('evidence_sha256',legacy['results'][0]['reviews'][1])
        self.assertIsNone(legacy['results'][0]['human_grade'])

    def test_summary_and_comparison_do_not_mutate_valid_historical_reports(self):
        r=grade(report());original=copy.deepcopy(r)
        review.summarize(r);review.compare(r,r)
        self.assertEqual(r,original)
        changed=copy.deepcopy(r);changed['fingerprint']['suite_sha256']='not-a-hash'
        with self.assertRaises(ValueError):review.compare(changed,changed)


if __name__=='__main__':unittest.main()
