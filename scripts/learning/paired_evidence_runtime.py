"""Run a prepared evidence protocol through the shared paired model worker."""
from pathlib import Path
import time

from scripts.learning import assess_polarity_evidence as paired

ROOT = Path(__file__).resolve().parents[2]


def collect(folder, name, driver, *, frozen, checked, result):
    with paired.batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        plan = checked(folder)
        admitted = paired.short.segments.idle.idle_sample()
        if admitted['idle_ms'] < paired.gpu.IDLE_MS:
            raise ValueError('尚未達到原生閒置門檻。')
        run = dict(protocol_sha256=paired.digest(plan), admitted=admitted)
        paired.save(folder / 'run.json', run)
        deadline = time.monotonic() + paired.LIMIT_SECONDS

        def check():
            paired.gpu.input_gate(admitted)
            if time.monotonic() >= deadline:
                raise TimeoutError('配對證據實測已達時限。')
            if paired.read(folder / 'run.json') != run or frozen(folder) != plan:
                raise ValueError('配對證據的來源或規約已變更。')

        try:
            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(driver).resolve()), '_worker', name, paired.digest(run)]
            check()
            paired.gpu.run_owned(command, folder, paired.PHASE, lambda: paired.gpu.input_gate(admitted), check)
            if checked(folder) != plan:
                raise ValueError('配對證據的參考來源已變更。')
            report = result(folder, plan, run)
            paired.save(folder / 'review.json', report)
            return report
        except BaseException as exc:
            paired.save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(folder, *, checked, result):
    report = result(folder, checked(folder), paired.read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or paired.read(folder / 'review.json') != report:
        raise ValueError('配對報告與重算證據不符。')
    return report
