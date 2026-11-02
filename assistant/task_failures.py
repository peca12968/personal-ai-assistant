"""Public failure guidance from exact host error contracts, never raw exceptions."""
import re


def failure_guidance(task):
    status, error = task['status'], task.get('error', '')
    if status not in {'failed', 'cancelled'}:
        return None
    guidance = {'requires_new_plan': True, 'basis': 'recorded_status',
                'reason': 'unclassified', 'explanation': '這次沒有完成結果；聊天沒有可確認的具體失敗原因摘要。',
                'next_step': '在工作工具查看本機錯誤訊息；處理原因後重新建立並核對計畫，再明確執行。'}
    if status == 'cancelled':
        guidance.update(reason='cancelled', explanation='這次工作已取消，沒有完成結果。',
                        next_step='若仍要處理，重新建立並核對計畫，再明確執行。')
        return guidance
    contracts = {
        '來源已在預覽後改變，請建立並確認新計畫。': ('source_changed', '輸入資料與建立計畫時的版本不同。',
            '重新選取目前要處理的來源、建立新計畫，核對預覽後再執行。'),
        '來源在執行期間改變，結果未標記完成。': ('source_changed_during_execution', '來源在執行過程中改變，結果未被採用。',
            '先確保來源暫時不再修改，再重新建立並核對計畫後執行。'),
        '工作超過處理時間上限。': ('timeout', '工作超過處理時間上限，沒有完成結果。',
            '縮小輸入範圍後重新建立並核對計畫，再執行。'),
        '分組超過 5000 組，請縮小輸入。': ('too_many_groups', '分組數超過 5000 組上限。',
            '縮小來源或調整篩選條件，再重新建立並核對計畫後執行。'),
        '結果超過 4 MiB，請縮小輸入。': ('result_too_large', '結果超過 4 MiB 上限。',
            '縮小來源或調整篩選條件，再重新建立並核對計畫後執行。'),
        '不支援的任務計畫版本或操作。': ('unsupported_plan', '目前程式不支援這份計畫版本或操作。',
            '使用目前版本支援的工作工具重新建立計畫，不要重跑舊計畫。'),
        '分組結果與來源總和不一致。': ('verification_failed', '計算結果與來源的筆數或總和核對不一致。',
            '保留來源與本機錯誤紀錄供檢查，不把這次結果當作完成；問題排除後再建立新計畫。'),
    }
    if error in contracts:
        reason, explanation, next_step = contracts[error]
        guidance.update(reason=reason, explanation=explanation, next_step=next_step, basis='recognized_recorded_error')
    else:
        match = re.fullmatch(r'第 ([1-9][0-9]{0,5}) 筆資料的數值不是支援的十進位數字；未略過或當成零。', error)
        if match:
            guidance.update(reason='invalid_number', basis='recognized_recorded_error', record_number=int(match[1]),
                            explanation=f'第 {match[1]} 筆資料有不支援的數值，工作整體失敗；未略過或當成零。',
                            next_step='檢查該筆資料的數值欄。修正來源後重新建立並核對計畫，再明確執行。')
    return guidance
