"""Three-way field merges for explicitly edited local memories; no model calls."""
FIELDS = ('content', 'kind', 'expires_on', 'active')
LABELS = {'content': '內容', 'kind': '類型', 'expires_on': '到期日', 'active': '啟用狀態'}
ROW_FIELDS = {'id', 'content', 'kind', 'created_at', 'active', 'expires_on', 'updated_at'}


class MemoryConflict(ValueError):
    def __init__(self, original, current, draft, comparison):
        super().__init__('同一筆記憶已有不同修改，請核對衝突欄位；你的草稿尚未儲存。')
        self.original = dict(original)
        self.current = dict(current)
        self.draft = dict(draft)
        self.comparison = comparison


def validate_original(original, memory_id):
    if (not isinstance(original, dict) or set(original) != ROW_FIELDS or
            type(original['id']) is not int or original['id'] != memory_id):
        raise ValueError('需要選取記憶時的完整版本，請重新核對。')


def compare(original, current, draft):
    validate_original(original, current['id'])
    if original['created_at'] != current['created_at'] or set(draft) != set(FIELDS):
        raise ValueError('記憶版本或編輯欄位不一致，請重新核對。')
    merged, conflicts, preserved = {}, [], []
    for key in FIELDS:
        if draft[key] == original[key]:
            merged[key] = current[key]
            if current[key] != original[key]:
                preserved.append(key)
        elif current[key] == original[key] or current[key] == draft[key]:
            merged[key] = draft[key]
        else:
            # A default for display only; writes must reject unresolved conflicts.
            merged[key] = current[key]
            conflicts.append(key)
    return {'merged': merged, 'conflicts': conflicts, 'preserved_fields': preserved}


def resolve(comparison, current, draft, choices):
    if set(choices) != set(comparison['conflicts']) or any(v not in {'current', 'draft'} for v in choices.values()):
        raise ValueError('每個衝突欄位都需選擇要保留的版本。')
    merged = dict(comparison['merged'])
    for key, choice in choices.items():
        merged[key] = current[key] if choice == 'current' else draft[key]
    return merged
