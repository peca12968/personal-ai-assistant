"""Validate the benchmark itself before interpreting any model scores."""
import itertools
import copy
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from scripts import reasoning_cases as tasks
from scripts import evaluate_reasoning_pair as pair


class ReasoningPairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.cases=tasks.make_cases(20261103)

    def test_reproducible_balanced_original_cases_and_public_projection(self):
        self.assertEqual(self.cases,tasks.make_cases(20261103))
        self.assertNotEqual(self.cases,tasks.make_cases(20261104))
        self.assertEqual(len({c['id'] for c in self.cases}),36)
        self.assertEqual(len({c['prompt'] for c in self.cases}),36)
        for category in tasks.CATEGORIES:
            self.assertEqual(sum(c['category']==category for c in self.cases),6)
        for batch in pair.batches(self.cases):
            self.assertEqual(len(batch),6)
            self.assertEqual({c['category'] for c in batch},set(tasks.CATEGORIES))
            public=json.loads(pair.public_prompt(batch)[len(pair.INSTRUCTIONS):])
            self.assertEqual(public,[{'id':c['id'],'prompt':c['prompt']} for c in batch])

    def test_logic_answers_against_explicit_truth_table_and_all_assignments(self):
        table={'implies':(True,True,False,True),'iff':(True,False,False,True),
               'xor':(False,True,True,False),'or':(False,True,True,True),
               'nand':(True,True,True,False),'and':(False,False,False,True)}
        def check(clause,bits):
            op,(a,b)=clause
            return table[op][2*bits[a]+bits[b]]
        for case in [c for c in self.cases if c['category']=='logic']:
            p=case['proof'];solutions=[v for v in itertools.product((0,1),repeat=6) if all(check(c,v) for c in p['clauses'])]
            outcomes={check(p['query'],v) for v in solutions}
            self.assertEqual(case['answer'],{frozenset({True}):'必真',frozenset({False}):'必假',frozenset({True,False}):'不確定'}[frozenset(outcomes)])
            self.assertEqual(len(solutions),p['solution_count'])

    def test_quantitative_probability_enumeration_and_rate_identity(self):
        cases=[c for c in self.cases if c['category']=='quantitative']
        p=cases[0]['proof'];duration=Fraction(cases[0]['answer'])
        self.assertEqual(sum(duration/n for n in p['rates'][:2])-duration/p['rates'][2],1)
        p=cases[1]['proof'];cost=10000
        final=Fraction(cost*(100+p['markup'])*(100-p['discount']),10000)
        self.assertEqual((final-cost)/cost,Fraction(cases[1]['answer']))
        p=cases[2]['proof'];balls=['r']*p['red']+['b']*p['blue']
        draws=list(itertools.combinations(range(len(balls)),3))
        successes=sum(sum(balls[i]=='r' for i in draw)==2 for draw in draws)
        self.assertEqual(Fraction(successes,len(draws)),Fraction(cases[2]['answer']))
        p=cases[3]['proof'];outcomes=[p['win']]+[-p['loss']]*(p['faces']-1)
        self.assertEqual(Fraction(sum(outcomes),len(outcomes)),Fraction(cases[3]['answer']))
        p=cases[4]['proof'];t=Fraction(cases[4]['answer'])
        self.assertEqual(p['v1']*(t+p['delay'])+p['v2']*t,p['distance'])
        n=cases[5]['proof']['n'];wanted=(set(range(4,n+1,4))|set(range(7,n+1,7)))-set(range(6,n+1,6))
        self.assertEqual(len(wanted),int(cases[5]['answer']))

    def test_spatial_keys_by_tracking_coordinates_instead_of_matrix_indexing(self):
        for case in [c for c in self.cases if c['category']=='spatial']:
            p=case['proof'];ones={(r,c) for r,row in enumerate(p['initial']) for c,v in enumerate(row) if v}
            for op in p['operations']:
                if op[0]=='rotate':ones={(c,4-r) for r,c in ones}
                elif op[0]=='horizontal':ones={(r,4-c) for r,c in ones}
                elif op[0]=='vertical':ones={(4-r,c) for r,c in ones}
                else:ones.symmetric_difference_update({tuple(op[1:])})
            result='/'.join(''.join('1' if (r,c) in ones else '0' for c in range(5)) for r in range(5))
            self.assertEqual(result,case['answer'])

    def test_tracking_keys_reverse_operations_and_permutation_constraints_unique(self):
        for case in [c for c in self.cases if c['category']=='tracking']:
            p=case['proof'];actual=case['answer'].split(',');actual.pop(p['insert'])
            deleted=(set(p['initial'])-set(actual)).pop()
            actual.insert(p['delete'],deleted)
            shift=p['shift'];actual=actual[-shift:]+actual[:-shift]
            i,j=p['swap'];actual[i],actual[j]=actual[j],actual[i];actual.reverse()
            self.assertEqual(actual,p['initial'])
        for case in [c for c in self.cases if c['category']=='constraints']:
            valid=[]
            for order in itertools.permutations('ABCDEF'):
                positions={v:i for i,v in enumerate(order)}
                checks=[]
                for op,a,b,n in case['proof']['clauses']:
                    x,y=positions[a],positions[b]
                    checks.append({'before':x<y,'distance':abs(x-y)==n,'not_adjacent':abs(x-y)>1,'not_position':x!=n}[op])
                if all(checks):valid.append(','.join(order))
            self.assertEqual(valid,[case['answer']])

    def test_induction_has_one_coefficient_triple_and_correct_target(self):
        for case in [c for c in self.cases if c['category']=='induction']:
            p=case['proof'];m=p['modulus'];solutions=[]
            for a,b,c in itertools.product(range(-3,4),range(-3,4),range(m)):
                if all((a*x+b*y+c-z)%m==0 for x,y,z in p['examples']):solutions.append([a,b,c])
            self.assertEqual(solutions,[p['coefficients']])
            a,b,c=solutions[0];x,y=p['target'];total=a*x+b*y+c
            self.assertEqual(total-(total//m)*m,int(case['answer']))

    def test_grader_equivalence_wrong_missing_extra_and_execution_errors(self):
        batch=pair.batches(self.cases)[0];answers={c['id']:c['answer'] for c in batch}
        self.assertTrue(all(r['correct'] for r in pair.grade(batch,json.dumps(answers))))
        numeric=next(c for c in batch if c['category']=='quantitative');n=Fraction(numeric['answer'])
        answers[numeric['id']]=f'{n.numerator*2}/{n.denominator*2}'
        self.assertTrue(all(r['correct'] for r in pair.grade(batch,json.dumps(answers))))
        answers[numeric['id']]='0'
        self.assertEqual(sum(r['correct'] for r in pair.grade(batch,json.dumps(answers))),5)
        answers.pop(numeric['id'])
        self.assertEqual(next(r for r in pair.grade(batch,json.dumps(answers)) if r['id']==numeric['id'])['status'],'missing')
        answers['extra']='anything'
        self.assertTrue(all(r['status']=='format_error' for r in pair.grade(batch,json.dumps(answers))))
        self.assertTrue(all(r['status']=='execution_error' for r in pair.grade(batch,'','timeout')))

    def test_invalid_json_types_duplicates_nan_and_wrong_answer_types(self):
        batch=pair.batches(self.cases)[0]
        for raw in ('[]','1','null','NaN','{"x":1,"x":2}','not json'):
            with self.subTest(raw=raw):
                self.assertTrue(all(r['status']=='format_error' for r in pair.grade(batch,raw)))
        answers={c['id']:True for c in batch}
        self.assertTrue(all(r['status']=='format_error' for r in pair.grade(batch,json.dumps(answers))))

    def test_abstention_equivalent_numbers_and_summary_cannot_double_count(self):
        batch=pair.batches(self.cases)[0]
        self.assertTrue(all(r['status']=='abstained' for r in pair.grade(batch,json.dumps({c['id']:'無法作答' for c in batch}))))
        case={'category':'quantitative'}
        for value in ('0.5','.5','1/2','-1/-2','+1/2'):
            self.assertEqual(pair.normalized(case,value),Fraction(1,2))
        calls=[]
        for repeat in range(2):
            for index,batch in enumerate(pair.batches(self.cases)):
                answer=json.dumps({c['id']:c['answer'] for c in batch})
                for arm in ('direct','assistant'):
                    calls.append({'arm':arm,'repeat':repeat,'batch':index,'case_ids':[c['id'] for c in batch],
                                  'answer':answer,'error':None,'elapsed_seconds':1,'scores':pair.grade(batch,answer)})
        report={'protocol':{'repeats':2},'status':'complete','calls':calls}
        summary=pair.summarize(report,self.cases)
        for arm in ('direct','assistant'):
            self.assertEqual(summary[arm]['correct'],72)
            self.assertEqual(summary[arm]['consistent_pairs'],36)
        bad=copy.deepcopy(report);bad['calls'].append(bad['calls'][0])
        with self.assertRaises(ValueError):pair.summarize(bad,self.cases)
        bad=copy.deepcopy(report);bad['calls'][0]['scores'][0]['correct']=False
        with self.assertRaises(ValueError):pair.summarize(bad,self.cases)
        bad=copy.deepcopy(report);bad['calls'].pop()
        with self.assertRaises(ValueError):pair.summarize(bad,self.cases)

    def test_pinned_engine_preserves_restrictions_and_only_records_header_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=pair.PinnedEngine(Path(tmp),'test-model','medium')
            with patch('assistant.engine.find_codex',return_value='codex'):
                command=e.command(Path(tmp)/'answer.txt')
            self.assertIn('--ignore-user-config',command);self.assertIn('--ephemeral',command)
            self.assertEqual(command[command.index('--model')+1],'test-model')
            self.assertIn('model_reasoning_effort="medium"',command)
            self.assertIn('web_search="disabled"',command)
            self.assertIn('shell_tool',command);self.assertIn('multi_agent',command)
            e.stderr_path.write_text('model: test-model\nreasoning effort: medium\nprivate text must not be retained\n',encoding='utf-8')
            with patch.object(pair.CodexEngine,'stop'):e.stop(None)
            self.assertEqual(e.metadata,{'model':'test-model','reasoning effort':'medium'})
            with patch.object(pair.CodexEngine,'answer',return_value='ok'):self.assertEqual(e.answer('test'),'ok')
            e.metadata['model']='different'
            with patch.object(pair.CodexEngine,'answer',return_value='ok'),self.assertRaises(pair.EngineError):e.answer('test')


if __name__=='__main__':unittest.main()
