import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from assistant import idle_learning, learning_batches as batches, learning_queue
from scripts.learning import check_gpu_checkpoint as probe
import test_learned_models as model_fixture
from test_parameter_acceptance import CASES


class GPUCheckpointControllerTests(unittest.TestCase):
    def stop_child(self, folder, poll_input, poll_sources, error):
        marker = folder/'test.started'
        command = [sys.executable, '-c', 'import os,sys,time; from pathlib import Path; '
                   'Path(sys.argv[1]).write_text(str(os.getpid()),encoding="ascii"); time.sleep(40)', str(marker)]
        with self.assertRaises(error):
            probe.run_owned(command,folder,'test',poll_input,poll_sources)
        record = json.loads((folder/'test.exit.json').read_text('utf8'))
        self.assertTrue(record['exit_confirmed'])
        self.assertIsNotNone(record['returncode'])
        if os.name == 'nt':
            self.assertEqual(record['worker'],int(marker.read_text('ascii')))

    def test_resumed_input_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            admitted = {'ticks_ms':700000,'last_input_tick':100000,'idle_ms':600000}
            def sensor():
                if (folder/'test.started').is_file():
                    return {'ticks_ms':700010,'last_input_tick':700008,'idle_ms':2}
                return admitted
            with patch.object(idle_learning,'idle_sample',side_effect=sensor):
                self.stop_child(folder,lambda:probe.input_gate(admitted),lambda:None,batches.Stopped)

    def test_withdrawn_revision_stops_owned_process_with_unchanged_reference_manifest(self):
        model_fixture.LearnedModelTests.setUp(self)
        item = learning_queue.confirm(self.store,'second synthetic prompt','sample')
        batch = batches.prepare(self.store,[{'id':item['id'],'revision':1}],parent=self.bid)
        jid = idle_learning.prepare(self.root,batch,CASES)
        path = idle_learning.folder(self.root,jid)
        original = (path/'manifest.json').read_bytes()
        plan = {'sources':probe.sources(),'data_dir':str(self.root),'reference_job':jid,
                'job_digest':batches.queue.digest(idle_learning.read_manifest(path,live=True))}
        probe.inputs(plan)
        def recheck():
            if (self.root/'test.started').is_file():
                learning_queue.withdraw(self.store,item['id'],1)
            probe.inputs(plan)
        self.stop_child(self.root,lambda:None,recheck,batches.Stale)
        self.assertEqual((path/'manifest.json').read_bytes(),original)
