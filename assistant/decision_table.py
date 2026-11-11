"""Evaluate a finite table of possible scenarios and choose the next useful question."""
import json
import re
from functools import cache
from itertools import product
from itertools import combinations
from math import prod

from . import goal_guidance_sources, goal_reply_sources
from .goal_stages import digest


GUIDANCE = '''check_decision: {"conditions":["入口","通行證"],"rows":[{"when":["北門","有"],"result":"入場"},{"when":["北門","無"],"result":"等待"}],"known":{"入口":"北門"}}。
目標或已讀原文已列明完整可行情境，或明定各欄選項及哪些組合不可行時，使用此工具核對結論、找缺列或挑選最少追問。conditions依原欄位順序，rows逐列保留原對應，when依欄位順序給值，known只填已確認且適用於本次的值。最多5個條件、32列，每個條件最多4種值。不把待查、舊值、假設或模型推測填進known。
若使用者交代先核對原安排再問更新，原規則和原條件齊備時先用此工具保存原核對，再詢問更新，以便後續保留原值與正式改動的區別。
若已知各欄完整選項，且各種組合皆可發生或已明文排除例外，另傳domains（如{"入口":["北門","南門"],"通行證":["有","無"]}）與excluded（依欄位順序列明文排除的組合，沒有則[]）。此模式各欄選項組合最多32種。保留已公布的對應；不同同級資料若針對同一組合給不同結果，在rows為該組合分列，不自行選定。只有明文不可能或排除的組合放excluded，缺規則與另案安排都不是排除，也不能代填缺列。分清兩份表是否適用同一事項。無法確定適用範圍或完整選項時沿用一般分析。收到正式補充後只新增有效對應或修正已解決的列，保留舊來源及適用範圍。
incomplete_table表示目前相容組合中有缺列或不同結果。coverage列出相容的缺列、衝突與明文排除，recorded_results僅為已記載結果。保留仍有來源的部分；需要唯一結果時，只問影響本次決策的處置規則或哪份說法生效，勿重問已確認欄位。收到可解決缺列或衝突的正式補充後，先更新該筆對應並再次執行check_decision，再交付。其餘適用的欄位、規則和排除照原資料，舊步驟保留。要求只交條件式成果時，標示缺漏或衝突，不追問。其他範圍的缺列不阻擋本次可確定的結果。
matching_rows是所送表格的1起算列號，並非原文件行號。resolved表示符合已知條件的各列結果相同，直接交付並保留無須核對的欄位為待查。needs_check時，question給最少化最壞追問輪數的第一欄與各答案可能結果，同分依表列欄位順序。若使用者需要唯一結果且允許提問，先交仍有依據的暫表，只問該欄；收到確定答覆後，沿用相同表列，只更新known並重算。若使用者只要條件式交付或明確不提問，保留所有相容分支後直接交付。no_match表示已知值與所送表列不相容，核對適用範圍及來源，不補造結論。
這個工具只計算送入的對照表，不能證明表格完整性或條件已核對。保留原需求和已知欄位，原文、使用者更正及回覆仍各用相應來源。
原文已明定可行情境、規則優先級及同級衝突時，優先用rules取代rows，讓程式展開並核對每個相容情境，並提供conditions、domains、excluded、known。例如rules為[{"priority":0,"when":{},"result":"前台"},{"priority":2,"when":{"級別":"高"},"result":"南架"}]。最多16條，priority為0至100整數且較大優先，只按原文明定關係轉錄。when只列該規則明定的限制，空物件只用於明文通用規則。不要推定最後一條或較具體的規則優先。
priority_cases逐一列適用的最高級規則、被覆蓋規則和仍缺依據的情境。同級有不同處置時保留各結果；沒有適用規則時保留缺口。winning_rules及overridden_rules是所送規則的1起算編號，引用仍用已讀的實際原行。若有question，先確認會區分結果或是否需要來源的欄位。max_questions只計確認欄位，不計補正式規則。沒有可區分的欄位且needs_evidence為真時，核對仍缺的依據。收到確認值後沿用同一規則重算。只要條件式暫表時按要求交付，不追問。
原文只有成對的無條件優先記載時，同樣用rules，各條省略priority，另傳precedence，例如[[1,2]]表示第1條優先於第2條。只送原文明定的關係，連彼此矛盾的方向也照實保留；沒有記載時可用空陣列。不要將部分順序轉成數字或推成同級。若關係只在特定條件成立，不可當作通用連線。
若優先關係只在特定條件或版本生效，用{"higher":1,"lower":2,"when":{"班別":"晚"}}替代該對編號，可與無條件編號對混用。when只填原文明定的條件與值。逐一在相容情境內核對順序，同一情境才可能構成循環。active_precedence與disputed_precedence是送入precedence的1起算序號，引用時仍用實際原文。未生效的關係不參與傳遞。
disputed_relations列出參與矛盾循環的原連線，工具不以它們推導其他順序。不矛盾的無條件關係可以傳遞，即使中間規則未觸發。未被覆蓋的候選若有不同結果，unranked_pairs列出尚無有效優先依據的組合，cyclic_pairs列出受循環影響的組合。只問仍會影響本次的候選關係；正式補充只修改它確實確認或取代的關係，再保留原規則重算。
若使用者以「核對#2｜條件｜班別｜晚」或「核對#2｜優先｜2｜2｜1」作為完整單行正式答覆或主動更正，可只送{"revision":"[補充#3]"}或實際更正編號。程式核對原話指定的最新決策步驟，沿用原表及規則。條件格式為步驟、條件、欄名、值；優先格式為步驟、優先、關係序號、優先規則編號、被覆蓋規則編號，只能確認或反轉該關係原方向，適用條件不變。來源須是該核對後唯一一則已保存的補充或更正。自然語句或不同格式沿用完整重算；不要自行把原話改寫為正式格式。結果引用實際補充或更正及原行。
同一則已保存的補充若有2至4行正式更正，每行皆須帶同一個核對編號，可混合條件及優先更正。仍只傳該來源的revision編號。各行按同一原表驗證，全部有效且新條件相容才整批重算。批次以changes列每項改動。任一行無效或重複時，標示整批未採用並沿用原核對。依使用者要求說明無效原因或請修正。若使用者之後另行正式確認部分更正，再按新來源重算。
使用者要求對照前後或說明未受影響範圍時，送{"revision":"[補充#3]","compare":true}。revision.comparison的before、after分別是舊核對和這次重算，retained_known保留未改的已知欄位。優先關係有明文範圍時，scope_comparison另外列各種假設條件的前後結果，其餘已知欄位分別沿用前後核對的值。用它核對如「晚班有變、早班仍用原規則」，不要把假設分支當成本次已確認。未決時只列仍有依據的候選；引用仍用原文或已保存的補充。只要求本次結果時維持單一revision參數。'''

REPLACEMENT_GUIDANCE = '''正式整批補充被check_decision退回後，需要補正時，只問一次完整的新補充。請使用者在首行寫「取代｜[補充#3]」（用實際退回的補充或更正編號），接著寫一至四行原核對編號與要採用的更正。收到後仍用{"revision":"[補充#5]"}，只傳新補充的實際編號。新補充取代整批退回內容，未重述欄位沿用原核對。revision.replaces保存上次退回步驟、來源與原因。若新批再因內容無效被退回，下次須指向該次退回的來源。取代編號不符時保留原核對，依要求交代原因或請使用者確認，勿把未核實的值改送完整表。'''

NATURAL_UPDATE_GUIDANCE = '''同一目標已有決策核對及最新自然語句補充，且只需調整已知欄位時，用check_decision的{"known_update":{"區位":"南","門證":null},"source":"[補充#3]"}，填實際來源及完整欄名。只轉錄本次明確更正的值；原值撤回、改為待查，或原先確認值已不能再沿用時，填null移除已知值，不能寫成相反值。未改的欄位及原表規則由工具沿用。程式核對來源位置、欄位及相容性，文字是否支持轉錄須由你先核對。舊值、未採用提案、另案及引述不成為本次事實。若回覆只說「其中一欄變了」，這是目前待澄清的欄位，不會增加原表的 excluded。若哪一欄變動會影響本次決定，直接問一次是哪欄，等確定後以原核對做 known_update；無須先重算一張加入臨時排除的表。若缺漏不會改變使用者要的結果，保留未知後直接交付。若只求假設試算，加"what_if":true，試算結果在what_if_result，頂層仍為本次實際核對，原表及本次條件不改。正式格式維持revision；要修改規則或選項時沿用完整核對。
若補充同時談本批、相近名稱、另案或舊狀態，先辨認各句的對象、時間及是否採用。沒有本次改動就沿用原核對。對象不明而影響結論時只確認是哪個，待答覆後把原狀態短句和最新指代確認一起引用。遇到這類混合或跨輪補充，或使用者要求引用原話，請為每個更動欄位附evidence，例如{"時段":[{"citation":"[補充#3]","quote":"本批改成晚班"}]}。每欄一至兩則，由早到晚，最後一則必須是source。quote逐字擷取唯一可定位的原文，連同必要的對象或撤回措辭。不要重寫引文。核對短句確實支持該欄的改值、撤回或試算，再採用。成果需要交代採用的原話或範圍時，引用本次採用的短句，並簡短標記可能混淆的相近名稱或舊狀態各屬哪一案。'''

NATURAL_UPDATE_GUIDANCE += '''
同一則最新補充同時明確更新正式條件，並要求以更新後的正式條件為起點做一次假設時，用同一個check_decision。known_update、source及evidence放正式改動，另附"scenario":{"updates":{"原欄名":"試算值"},"evidence":{"原欄名":[{"citation":"[補充#3]","quote":"假設…"}]}}，不用what_if:true。正式與試算各附逐字短句，最新來源的正式短句與試算短句不能重疊；試算短句從原文的假設、如果、若或試算標記開始。只改明列欄位，撤回用null。工具先算正式結果，再從更新後的條件只套用試算欄位，回傳what_if_result與what_if_update。只保存正式條件供之後接續；交付分開標示兩種結果及各自原話。未要求試算的提案不填scenario。'''

NATURAL_UPDATE_GUIDANCE += '''
最新原話明列本輪只可能是二至八個配對，且每組都明列相同的兩個以上完整欄位時，在known_update旁加"alternatives":{"cases":[{"日期":"週三","件數":"18"},{"日期":"週五","件數":"12"}],"quote":"本輪只剩兩種可能：週三18件或週五12件"}。用實際source、原表的完整選項及逐字的限制短句。已撤回的原已知值用null，沒有欄位要改可給空物件。這些配對只限制本輪，不改原表excluded和規則。未採用提案、獨立待查，或只說某個欄變動但未列完整配對時，不填alternatives。current_alternatives.remaining_cases是仍可能的配對，implied_known是依仍可能配對可推得的欄位，引用配對原話及正式確認。若各配對結果一致或使用者只要條件成果，直接交付；須確認時只問會區分結果的欄位。收到確認只更新該欄，工具會沿用本輪配對。'''

NATURAL_UPDATE_GUIDANCE += '''
原表用明列domains和rows，且有可行組合未公布結果或同一組有多個結果時，送本輪alternatives請同時填"track_coverage":true在alternatives內。原表的缺列仍缺，互相衝突的來源仍並列。coverage_cases逐組列已知結果、缺規則或衝突，recorded_results是各組原有記載。若使用者要本次確定結果，question可先確認能決定是否或哪一組須補規則的欄位；先交可核對部分，再只問該欄。等確定仍適用的缺口才補問規則，不能把未公布當成不可能。max_questions只計確認條件。只要條件報告或禁止追問時，各組照實交付，不問。'''

NATURAL_UPDATE_GUIDANCE += '''
本輪配對已確認唯一完整組合，仍缺原表結果或有同組衝突時，只問該組正式規則。可請使用者用整則原話「核對#6｜補規則｜週四｜6｜西廳」或「核對#6｜選規則｜側門｜有｜北桌」，核對編號用最後實際check_decision步驟，選項照原表欄序及原值，最後一格是處置。補規則用於缺列，選規則限同組原有結果。收到完整格式後只送{"revision":"[補充#7]"}或實際更正編號，不重建原表。current_row_rules列本輪採用、原列及正式補充。原表的記載與其他組的缺口仍可核對。沒有唯一生效組合或只需條件報告時，按原要求處理。'''

NATURAL_UPDATE_GUIDANCE += '''
本輪配對唯一且仍缺規則時，也可讓使用者直接說「這一組正式用西廳」或「本輪正式採用丁台」。同一則可先明寫「入口正式改為西口」，再寫「這一組正式採用丁台」；欄名與值須是原表完整文字。句子以句號或分行分開。可附「群聊提議『換東廳』，未採用」等明確不採用的引述。收到符合的自然原話，用check_decision的{"row_confirmation":"[補充#7]"}（實際來源編號），工具從原話套用正式欄位、確認唯一組合，再核對規則。只轉述或尚未決定就保留待查，不送此動作，也不把提案寫入新表。'''

NATURAL_UPDATE_GUIDANCE += '''
有宣告選項的原表時，若整則補充明確只更改一欄、卻省略欄名，例如「正式改成晚班。」，用check_decision的{"field_reference":"[補充#3]"}核對，並用實際來源。可另列整句明確未採用的提案。工具比對完整選項，逐個可能欄位分開核對。只有唯一候選時直接套用；各候選的確定結果一致時直接交共同結果並保留欄位待查。其餘若使用者需確定結果，先列各分支，只問是哪個欄位，無須重問已知值。收到一個完整欄名，送{"field_reference":"[補充#5]","resolve":true}，引用欄名答覆。最後引用原值的補充及欄名確認。未採用的轉述不進入這個流程；候選未確認時，不先用完整新表或known_update猜欄位。'''

RECHECK_GUIDANCE = '''欄位指代的field_reference.recheck是保存時已重核的正式欄位更正；其evidence是新原話。較早的finish是更正前的歷史成果。若有overwritten_fields，該欄一律用最新明確值；branches的known是先套第一筆候選改值、再套新更正後的各欄值。即使後續確認原欄名，也保留被覆蓋欄位的最新值。各候選結果仍一致時直接交更新的結果，原欄位待查；結果分開且要定稿時，交分支並只問第一筆是哪一欄。欄名答覆用resolve，不重建決策表。superseded表示所有原候選已被後續明確值覆蓋，不問歷史欄名，依現行欄值核對。定稿引用原改值、相關正式更正及實際欄名確認。'''


WAITING_REFERENCE_GUIDANCE = '''
先依交付需求判斷原欄名是否只用來區分決策結果。沿用尚未被撤回或更正的已知條件，查讀來源已確認的份數或人名無須重問。
如果成果本身需要欄名，或其他交付項由欄名決定（例如改值欄位決定該通知哪位負責人），用一般ask自然地詢問原欄名並說明用途。若來源可由欄名推得負責人，只問缺的欄名。原欄名仍有上述用途或使用者要求維持整題時，使用一般問句。
原欄名只用來區分處置時，ask的question可逐字使用field_reference_question，分支放在partial_result。另有一項必要且未確認的資料，若兩種欄名和去向下都問同一件事，可同一步分項詢問。依交付要求及來源判斷獨立性，使用者無須指定分項格式。
reference_remaining只填一個短而完整的獨立問句。份數用「本次要交付幾份？」、通知對象用「本次要通知誰？」、領件人用「本次由誰領件？」。來源、待回報說明與可先答一項的提示放在partial_result。question依序接上field_reference_question、換行、reference_remaining的原句、換行、「請分兩行回答，先回完整欄名，再答補問。」。
精確分項問句必須有reference_remaining。漏標記被拒時，保持原question補上該欄即可直接重試，程式會核對原核對及失敗紀錄的連結。條件或問句改變時先重新核對。
field_reference.waiting_recheck是等待期間的重核，原暫表是歷史。若欄名已無必要，原題撤下。有remaining_question時，其後新題及其回答只對應獨立補問。若仍有分歧且原問已有欄名答覆，用resolve引用原問補充。分兩行的回覆只採第一行判定欄名，第二行回答補問。'''

_REFERENCE_REPLY_HINT = '請分兩行回答，先回完整欄名，再答補問。'
_MISSING_REFERENCE_MARKER = ('分項問句缺少reference_remaining。請保持原question，將第二行的完整獨立問句填入reference_remaining後重試。'
                             '原表與欄名分支仍有效；這次規劃尚未成為待答問題。')
_PARTIAL_REFERENCE_REPLY = re.compile(r'(?P<label>欄名|補問)[：:] *(?P<value>[^\r\n]*)')
_PARTIAL_REPLY_CLAUSES = re.compile(r'[^,，。；;\r\n]+[,，。；;][^,，。；;\r\n]+。?')
_DEFERRED_REPLY = re.compile(
    r'(?P<item>(?:第一筆(?:改值)?的?)?欄名|補問|通知對象|交付份數|份數|領件人)(?:我)?'
    r'(?:還沒確認|還未確認|尚未確認|還沒確定|尚未確定|還不確定|仍不確定|暫不確定|仍待確認|待確認|待查|未定)')
_NAMED_FIELD_REPLY = re.compile(r'(?:(?:第一筆(?:改值)?(?:指的是|指向|指|是)|欄名(?:我)?(?:確認是|已確認為|是|為)) *)?(?P<value>.+)')
_PERSON_REPLY = (r'(?P<value>「[\u3400-\u9fff]{2,16}」|[\u3400-\u9fff]{2,5}|'
                 r'[\u3400-\u9fff]{1,8}(?:值班員|同仁|先生|小姐|主任|經理|組長|專員|老師|主管|承辦人|管理員))')
_UNCERTAIN_REMAINING = re.compile(
    r'可能|也許|或|還沒|不知道|不確定|未定|未決|未確認|未確定|先不|不用|不要|不必|不是|尚未|待查|待確認|再確認|暫定|暫時'
    r'|大概|應該|請勿|取消|還有|以及|如果|但|建議|再說|稍後|誰|嗎'
    r'|[\u3400-\u9fff]{2,}[及和跟與][\u3400-\u9fff]{2,}')
_NATURAL_REMAINING = (
    (re.compile(r'(?:這次|本次|本輪|這輪)?(?:要|需)?通知(?:誰|哪一位)[？?]'), ('通知對象',),
     (re.compile(r'(?:請通知|先通知|通知對象(?:已確認為|是|為)|通知) *' + _PERSON_REPLY),)),
    (re.compile(r'(?:這次|本次|本輪|這輪)?(?:要|需)?交付幾份[？?]'), ('交付份數', '份數'),
     (re.compile(r'(?:(?:這次|本次|本輪|這輪)?(?:要交付|交付份數(?:是|為)|交付) *)?'
                 r'(?P<value>(?:[0-9０-９]{1,6}|[零〇一二兩三四五六七八九十百千]{1,8}) *份)'),)),
    (re.compile(r'(?:這次|本次|本輪|這輪)?(?:由誰領件|領件人是誰)[？?]'), ('領件人',),
     (re.compile(r'由 *' + _PERSON_REPLY + r' *領件'), re.compile(r'領件人(?:是|為) *' + _PERSON_REPLY))),
)
PARTIAL_REFERENCE_GUIDANCE = '''field_reference.partial_reply表示使用者明確只答原分項提問的一項。依receipt的question、quote與citation採用已答項；next_question是唯一待答項。若後續已有該問的答覆，合用兩項證據交付，不重問。補問答覆即使與候選欄名同字也不能用來resolve。'''
ANSWERED_REVISION_GUIDANCE = '''field_reference.answered_revision是等待剩餘問句時對已答項的正式更正。依其value及最後一筆changes的evidence採用並引用最新值。原partial_reply和較早更正是歷史。condition_changes是同時核對的原表正式新值，現行分支已採用這些新值。withdraws_question為true時，原欄名已不影響結果且已撤下，依共同結果或現行完整條件交付，原歷史欄名待查。其他情況ask_sequence指維持原樣的剩餘問句；若已收到回覆，依該回覆完成剩餘項目。若剩餘項是欄名，用resolve引用該題的補充。'''
_ANSWERED_CHANGE = re.compile(
    r'(?P<target>已答欄名|第一筆欄名|已答補問|通知對象|領件人|交付份數|份數)正式(?:改為|改成) *'
    r'(?P<value>[^\r\n，,；;。]+?)。?')
_NATURAL_ANSWERED_CHANGE = (
    r'(?:(?:剛才|剛剛|先前|之前)(?:的)? *)?'
    r'(?P<target>已答欄名|第一筆欄名|欄名|已答補問|通知對象|領件人|交付份數|份數)'
    r'(?:(?:我)?(?:剛才|剛剛|先前|之前)?(?:說錯了|填錯了|記錯了|寫錯了)[，,] *'
    r'(?:應該是|正確是|正確應為|應改為|改成|改為)|(?:正式)?(?:改成|改為|更正為)) *'
    r'(?P<value>[^\r\n，,；;。]+)')
_COMBINED_CORRECTION = re.compile(_NATURAL_ANSWERED_CHANGE)
_COMBINED_CLAUSE = re.compile(
    r"""(?P<clause>(?:「[^「」\r\n]*」|『[^『』\r\n]*』|"[^"\r\n]*"|'[^'\r\n]*'|[^，,。；;\r\n「『"'])+)""")
_ANSWERED_SEPARATORS = re.compile(r'[，,。；;\r\n ]*')


def _label(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 120 or value != value.strip()
            or any(char in value for char in '\0\r\n')):
        raise ValueError('決策欄名、選項與結果需為1至120字的完整單行文字。')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('決策表含無效文字。') from exc


def _assignment(values, size):
    if not isinstance(values, list) or len(values) != size:
        raise ValueError('每個組合需按原欄位順序提供全部選項。')
    for value in values:
        _label(value)
    return tuple(values)


def _declared_scenarios(args, conditions):
    if 'domains' not in args:
        return None, set()
    domains, excluded = args['domains'], args['excluded']
    if not isinstance(domains, dict) or domains.keys() != set(conditions):
        raise ValueError('宣告選項需涵蓋每個決策欄。')
    options = []
    for name in conditions:
        values = domains[name]
        if not isinstance(values, list) or not 1 <= len(values) <= 4:
            raise ValueError('每個條件需有1至4種宣告值。')
        options.append(_assignment(values, len(values)))
        if len(set(options[-1])) != len(values):
            raise ValueError('同一決策欄的宣告值不可重複。')
    if prod(map(len, options)) > 32:
        raise ValueError('宣告選項的全部組合最多32種。')
    declared = tuple(product(*options))
    if not isinstance(excluded, list) or len(excluded) > len(declared):
        raise ValueError('排除需列出有限且不同的宣告組合。')
    omitted = {_assignment(row, len(conditions)) for row in excluded}
    if len(omitted) != len(excluded) or not omitted <= set(declared):
        raise ValueError('排除的組合需在宣告選項內且不得重複。')
    return declared, omitted


def validate(args):
    base = {'conditions', 'known'}
    cases = {'domains', 'excluded'}
    rule_fields = base | cases | {'rules'}
    if not isinstance(args, dict) or set(args) not in (
            base | {'rows'}, base | cases | {'rows'}, rule_fields, rule_fields | {'precedence'}):
        raise ValueError('決策表需提供conditions、known及rows，或提供domains、excluded及rules。')
    conditions, known = args['conditions'], args['known']
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 5:
        raise ValueError('決策表需有1至5個條件。')
    for name in conditions:
        _label(name)
    if len(set(conditions)) != len(conditions):
        raise ValueError('決策欄名不可重複。')
    if not isinstance(known, dict) or not known.keys() <= set(conditions):
        raise ValueError('已知條件需使用所列的完整欄名。')
    for value in known.values():
        _label(value)
    declared, excluded = _declared_scenarios(args, conditions)
    if 'rules' in args:
        relative = 'precedence' in args
        _validate_priority_rules(args['rules'], args['domains'], relative=relative)
        if relative:
            _validate_precedence(args['precedence'], len(args['rules']), args['domains'])
        return declared, excluded
    rows = args['rows']
    minimum = 1 if declared is None else 0
    if not isinstance(rows, list) or not minimum <= len(rows) <= 32:
        raise ValueError('決策表最多32列；未宣告欄位選項時至少需要一列。')
    assignments = set()
    reports = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'when', 'result'}:
            raise ValueError('每列需按原欄位順序提供全部選項及一個結果。')
        assignment = _assignment(row['when'], len(conditions))
        _label(row['result'])
        if declared is None and assignment in assignments:
            raise ValueError('決策表不能重複列出同一情境。')
        if declared is not None and (assignment not in declared or assignment in excluded):
            raise ValueError('表列情境需在宣告選項內，且不能同時排除。')
        report = assignment, row['result']
        if report in reports:
            raise ValueError('同一情境的相同結果不得重複列出。')
        assignments.add(assignment)
        reports.add(report)
    if any(len(set(values)) > 4 for values in zip(*assignments)):
        raise ValueError('每個條件最多4種表列值。')
    return declared, excluded


def _validate_priority_rules(rules, domains, *, relative):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 16:
        raise ValueError('決策需提供1至16條明文規則。')
    fields = {'when', 'result'} if relative else {'priority', 'when', 'result'}
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != fields:
            raise ValueError('規則欄位須符合所選的優先順序格式。')
        condition = rule['when']
        if not relative and (type(rule['priority']) is not int or not 0 <= rule['priority'] <= 100):
            raise ValueError('規則優先級須為0至100的整數。')
        _validate_rule_condition(condition, domains)
        _label(rule['result'])


def _validate_rule_condition(condition, domains):
    if not isinstance(condition, dict) or not condition.keys() <= domains.keys():
        raise ValueError('規則條件須使用宣告欄名。')
    for field, value in condition.items():
        _label(value)
        if value not in domains[field]:
            raise ValueError('規則條件值須為宣告選項。')


def _validate_precedence(edges, rule_count, domains):
    if not isinstance(edges, list) or len(edges) > 64:
        raise ValueError('最多可提供64個成對優先關係。')
    seen = set()
    for edge in edges:
        condition = {}
        if isinstance(edge, dict):
            if set(edge) != {'higher', 'lower', 'when'}:
                raise ValueError('條件式優先關係須指定higher、lower及when。')
            condition = edge['when']
            _validate_rule_condition(condition, domains)
            pair = [edge['higher'], edge['lower']]
        else:
            pair = edge
        if (not isinstance(pair, list) or len(pair) != 2
                or any(type(index) is not int or not 1 <= index <= rule_count for index in pair)
                or pair[0] == pair[1]):
            raise ValueError('每個優先關係須指定兩個不同的已列規則。')
        key = tuple(pair), frozenset(condition.items())
        if key in seen:
            raise ValueError('同條件的同方向優先關係不可重複。')
        seen.add(key)


def question_order(assignments, labels, active):
    def groups(indices, field):
        result = {}
        for index in indices:
            result.setdefault(assignments[index][field], []).append(index)
        return result

    @cache
    def solve(indices):
        if len({labels[index] for index in indices}) == 1:
            return 0, None
        choices = []
        for field in range(len(assignments[indices[0]])):
            branches = groups(indices, field)
            if len(branches) > 1:
                depth = 1 + max(solve(tuple(branch))[0] for branch in branches.values())
                choices.append((depth, field))
        return min(choices)

    depth, first = solve(active)
    branches = [] if first is None else [(value, indices, solve(tuple(indices))[0])
                                         for value, indices in groups(active, first).items()]
    return depth, first, branches


def _matched_rules(assignment, conditions, rules):
    values = dict(zip(conditions, assignment))
    return {index: rule for index, rule in enumerate(rules, 1)
            if all(values[field] == value for field, value in rule['when'].items())}


def _rule_case(assignment, matched, winners, **details):
    outcomes = list(dict.fromkeys(matched[index]['result'] for index in winners))
    status = 'missing_rule' if not winners else 'resolved' if len(outcomes) == 1 else 'conflicting_rules'
    return {'when': list(assignment), 'status': status, **details, 'winning_rules': winners,
            'overridden_rules': [index for index in matched if index not in winners],
            'recorded_results': outcomes}


def _priority_case(assignment, conditions, rules):
    matched = _matched_rules(assignment, conditions, rules)
    priority = max((rule['priority'] for rule in matched.values()), default=None)
    winners = [index for index, rule in matched.items() if rule['priority'] == priority]
    return _rule_case(assignment, matched, winners, priority=priority)


def _reachable_rules(rule_count, edges):
    reachable = {index: set() for index in range(1, rule_count + 1)}
    for higher, lower in edges:
        reachable[higher].add(lower)
    for through, successors in reachable.items():
        for destinations in reachable.values():
            if through in destinations:
                destinations.update(successors)
    return reachable


def _precedence_case(assignment, conditions, rules, reachable, stated_paths):
    matched = _matched_rules(assignment, conditions, rules)
    overridden = set().union(*(reachable[index] for index in matched))
    winners = [index for index in matched if index not in overridden]
    unranked, cyclic = [], []
    for first, second in combinations(winners, 2):
        if matched[first]['result'] == matched[second]['result']:
            continue
        pairs = cyclic if second in stated_paths[first] and first in stated_paths[second] else unranked
        pairs.append([first, second])
    return _rule_case(assignment, matched, winners, unranked_pairs=unranked, cyclic_pairs=cyclic)


def _precedence_paths(rule_count, edges):
    stated_paths = _reachable_rules(rule_count, edges)
    disputed = [edge for edge in edges if edge[0] in stated_paths[edge[1]]]
    # Keep recorded contradictions visible, but do not infer through them.
    reachable = _reachable_rules(rule_count, [edge for edge in edges if edge not in disputed]) if disputed else stated_paths
    return reachable, stated_paths, disputed


def _scoped_precedence_cases(assignments, conditions, rules, edges):
    relations = [(index, (edge['higher'], edge['lower']), edge['when']) if isinstance(edge, dict)
                 else (index, tuple(edge), {}) for index, edge in enumerate(edges, 1)]
    cases, disputes = [], set()
    for assignment in assignments:
        values = dict(zip(conditions, assignment))
        applicable = [(index, pair) for index, pair, when in relations if when.items() <= values.items()]
        active_pairs = list(dict.fromkeys(pair for _, pair in applicable))
        reachable, stated_paths, disputed = _precedence_paths(len(rules), active_pairs)
        case = _precedence_case(assignment, conditions, rules, reachable, stated_paths)
        case['active_precedence'] = [index for index, _ in applicable]
        case['disputed_precedence'] = [index for index, pair in applicable if pair in disputed]
        disputes.update(case['disputed_precedence'])
        cases.append(case)
    return cases, [edge for index, edge in enumerate(edges, 1) if index in disputes]


def _calculate_priority(args, compatible, excluded):
    conditions, rules = args['conditions'], args['rules']
    active = [row for row in compatible if row not in excluded]
    if 'precedence' in args:
        edges = args['precedence']
        if any(isinstance(edge, dict) for edge in edges):
            cases, disputed = _scoped_precedence_cases(active, conditions, rules, edges)
        else:
            reachable, stated_paths, disputed = _precedence_paths(len(rules), edges)
            cases = [_precedence_case(row, conditions, rules, reachable, stated_paths) for row in active]
    else:
        cases = [_priority_case(row, conditions, rules) for row in active]
    observation = {'priority_cases': cases, 'coverage': {
        'missing_rows': [case['when'] for case in cases if case['status'] == 'missing_rule'],
        'conflicting_rows': [{'when': case['when'], 'rules': case['winning_rules'], 'results': case['recorded_results']}
                             for case in cases if case['status'] == 'conflicting_rules'],
        'excluded_rows': [list(row) for row in compatible if row in excluded],
    }}
    if 'precedence' in args:
        observation['disputed_relations'] = disputed
    if not cases:
        return {'status': 'no_match', **observation, 'possible_results': []}
    results = list(dict.fromkeys(result for case in cases for result in case['recorded_results']))
    observation.update(_case_questions(conditions, cases))
    if observation['needs_evidence']:
        return {'status': 'incomplete_table', **observation, 'recorded_results': results}
    return {'status': 'needs_check' if 'question' in observation else 'resolved', **observation, 'possible_results': results}


def _case_questions(conditions, cases, *, distinguish_gaps=False):
    outcomes = [case['recorded_results'][0] if case['status'] == 'resolved' else None for case in cases]
    labels = [(outcome, tuple(case['when']) if outcome is None and distinguish_gaps else ())
              for outcome, case in zip(outcomes, cases)]
    depth, first, groups = question_order([case['when'] for case in cases], labels, tuple(range(len(cases))))
    result = {'max_questions': depth, 'needs_evidence': None in outcomes}
    if first is not None:
        branches = []
        for value, indices, remaining in groups:
            distinct = dict.fromkeys(outcomes[index] for index in indices)
            branches.append({'value': value, 'max_questions': remaining,
                             'possible_results': [outcome for outcome in distinct if outcome is not None],
                             'needs_evidence': None in distinct})
        result['question'] = {'condition': conditions[first], 'branches': branches}
    return result


def calculate(args, *, alternatives=None, require_all=False, row_rules=()):
    declared, excluded = validate(args)
    conditions, known = args['conditions'], args['known']
    indices = {name: index for index, name in enumerate(conditions)}
    fixed = [(indices[name], value) for name, value in known.items()]
    cases = alternatives['cases'] if alternatives is not None else ()
    pairs = [tuple((indices[name], value) for name, value in case.items()) for case in cases]

    def fits(assignment, requirements):
        return all(assignment[index] == value for index, value in requirements)

    def matches(assignment):
        return fits(assignment, fixed) and (not pairs or any(fits(assignment, pair) for pair in pairs))

    if 'rules' in args:
        result = _calculate_priority(args, [row for row in declared if matches(row)], excluded)
    else:
        result = _calculate_rows(args, declared, excluded, matches,
                                 track_coverage=bool(alternatives and alternatives.get('track_coverage')),
                                 row_rules=row_rules)
    if alternatives is None:
        return result

    universe = declared if declared is not None else [tuple(row['when']) for row in args['rows']]
    active = [row for row in universe if row not in excluded and fits(row, fixed)]
    remaining = [case for case, pair in zip(cases, pairs) if any(fits(row, pair) for row in active)]
    if require_all and len(remaining) != len(cases):
        raise ValueError('本輪配對有與原表可行情境或正式條件不相容的選項，未套用配對。')
    implied = {name: value for name, value in (remaining[0] if remaining else {}).items()
               if name not in known and all(case[name] == value for case in remaining)}
    return {**result, 'current_alternatives': {**alternatives, 'remaining_cases': remaining, 'implied_known': implied}}


def _calculate_rows(args, declared, excluded, matches, *, track_coverage=False, row_rules=()):
    conditions, rows = args['conditions'], args['rows']
    selected = {tuple(rule['when']): rule['row'] for rule in row_rules}
    if row_rules:
        rows = rows + [{'when': rule['when'], 'result': rule['result']} for rule in row_rules if not rule['original_rows']]
    active = tuple(index for index, row in enumerate(rows)
                   if matches(row['when']) and selected.get(tuple(row['when']), index + 1) == index + 1)

    def outcomes(indices):
        return list(dict.fromkeys(rows[index]['result'] for index in indices))

    observation = {'matching_rows': [index + 1 for index in active]}
    if declared is not None:
        compatible = [assignment for assignment in declared if matches(assignment)]
        reported = {}
        for index in active:
            reported.setdefault(tuple(rows[index]['when']), []).append(index)
        missing, conflicts = [], []
        for assignment in compatible:
            if assignment in excluded:
                continue
            indices = reported.get(assignment)
            if indices is None:
                missing.append(list(assignment))
            elif len(indices) > 1:
                conflicts.append({'when': list(assignment), 'rows': [index + 1 for index in indices],
                                  'results': outcomes(indices)})
        observation['coverage'] = {'missing_rows': missing, 'conflicting_rows': conflicts,
                                   'excluded_rows': [list(assignment) for assignment in compatible
                                                     if assignment in excluded]}
        if missing or conflicts:
            if track_coverage:
                coverage_cases = []
                for assignment in compatible:
                    if assignment in excluded:
                        continue
                    indices = reported.get(assignment, [])
                    status = 'missing_rule' if not indices else 'conflicting_rules' if len(indices) > 1 else 'resolved'
                    coverage_cases.append({'when': list(assignment), 'status': status,
                                           'matching_rows': [index + 1 for index in indices],
                                           'recorded_results': outcomes(indices)})
                observation.update(coverage_cases=coverage_cases,
                                   **_case_questions(conditions, coverage_cases, distinguish_gaps=True))
            return {'status': 'incomplete_table', **observation, 'recorded_results': outcomes(active)}
    observation['possible_results'] = outcomes(active)
    if not active:
        return {'status': 'no_match', **observation}
    depth, first, groups = question_order([row['when'] for row in rows], [row['result'] for row in rows], active)
    if first is None:
        return {'status': 'resolved', 'max_questions': depth, **observation}
    branches = [{'value': value, 'possible_results': outcomes(indices), 'max_questions': remaining}
                for value, indices, remaining in groups]
    return {'status': 'needs_check', 'max_questions': depth, **observation,
            'question': {'condition': conditions[first], 'branches': branches}}


_REVISION_SOURCE = re.compile(r'\[(?:補充|更正)#[1-9][0-9]{0,3}\]')
_SCENARIO_START = re.compile(r'^(?:假設|假如|如果|若|(?:(?:另|只|僅)(?:做|想|供)?)?試算)')
_PAIR_LIMIT = re.compile(r'(?:只|僅)(?:有|剩|可能|限|能)|限於|二選一')
_ROW_REVISION_KINDS = {'補規則': 'row_completion', '選規則': 'row_selection'}
_ROW_SENTENCE = re.compile(
    r'(?:(?:這一組|本輪|本次)(?:的結果)?正式(?:使用|採用|定為|用)|正式確認(?:這一組|本輪|本次)(?:的?結果)?(?:為|是|採用|用)) *'
    r'(?:「(?P<quoted>[^「」｜\x00\r\n]{1,120})」|(?P<plain>[\u3400-\u9fffA-Za-z0-9 -]{0,23}[廳台架區位口席桌門庫站室]))')
_UNADOPTED_SENTENCE = re.compile(
    r'(?:群聊提議|有人提議|有人建議|轉述|引述)(?:「[^「」]{1,200}」|『[^『』]{1,200}』)[，,] *(?:未採用|不採用|僅供參考)')
_ANSWERED_NOTE_TOKEN = re.compile(
    rf'(?P<proposal>{_UNADOPTED_SENTENCE.pattern})|(?P<deferred>{_DEFERRED_REPLY.pattern})|'
    rf'(?P<correction>{_NATURAL_ANSWERED_CHANGE})')
_UNCERTAIN_PLAIN = re.compile(r'不|未|非|暫|待|若|或|可能|但|假設|試算|還要|再確認')
_FIELDLESS_CHANGE = re.compile(r'(?:這一欄|那一欄|這欄|那欄)?(?:正式)?(?:改為|改成) *(?P<value>.+)')
_STEP_METADATA = {'companion_profile', 'engine_selection', 'goal_conditions_sha256'}
_TRANSITION_FIELDS = {'revision', 'known_update', 'row_confirmation', 'field_reference'}


class _RejectedRevision(ValueError):
    def __init__(self, message, source, citations):
        super().__init__(message)
        self.source = source
        self.citations = citations


def validate_tool(args):
    if isinstance(args, dict) and 'revision' in args:
        if (set(args) not in ({'revision'}, {'revision', 'compare'}) or not isinstance(args['revision'], str)
                or not _REVISION_SOURCE.fullmatch(args['revision'])
                or ('compare' in args and type(args['compare']) is not bool)):
            raise ValueError('局部重算只接受一個已保存的補充或更正編號。')
    elif isinstance(args, dict) and 'row_confirmation' in args:
        if (args.keys() != {'row_confirmation'} or not isinstance(args['row_confirmation'], str)
                or not _REVISION_SOURCE.fullmatch(args['row_confirmation'])):
            raise ValueError('自然規則確認只接受同一目標已保存的補充或更正編號。')
    elif isinstance(args, dict) and 'field_reference' in args:
        if (args.keys() not in ({'field_reference'}, {'field_reference', 'resolve'}, {'field_reference', 'recheck'},
                               {'field_reference', 'waiting_recheck'}, {'field_reference', 'partial_reply'},
                               {'field_reference', 'answered_revision'}, {'field_reference', 'combined_reply'})
                or not isinstance(args['field_reference'], str) or not _REVISION_SOURCE.fullmatch(args['field_reference'])
                or any(args[key] is not True for key in args.keys() - {'field_reference'})):
            raise ValueError('欄位指代核對須引用已保存的補充，確認欄名時另用resolve。')
    elif isinstance(args, dict) and 'known_update' in args:
        if (args.keys() - {'known_update', 'source', 'what_if', 'evidence', 'scenario', 'alternatives'}
                or not isinstance(args.get('source'), str) or not _REVISION_SOURCE.fullmatch(args['source'])
                or ('what_if' in args and type(args['what_if']) is not bool)):
            raise ValueError('已知欄位重算需指定一至五個欄位及最新補充來源。')
        _validate_update_fields(args['known_update'], allow_empty='alternatives' in args)
        if 'evidence' in args:
            _validate_update_evidence(args['evidence'], args['known_update'], args['source'])
        if 'scenario' in args:
            scenario = args['scenario']
            if ('what_if' in args or 'evidence' not in args or not isinstance(scenario, dict)
                    or scenario.keys() != {'updates', 'evidence'}):
                raise ValueError('同則正式更新與試算須分別提供欄位及原話證據。')
            _validate_update_fields(scenario['updates'])
            _validate_update_evidence(scenario['evidence'], scenario['updates'], args['source'])
        if 'alternatives' in args:
            if args.get('what_if'):
                raise ValueError('本輪正式配對不能當成單獨試算。')
            _validate_alternatives(args['alternatives'])
    else:
        validate(args)


def _validate_update_fields(updates, *, allow_empty=False):
    if not isinstance(updates, dict) or not (0 if allow_empty else 1) <= len(updates) <= 5:
        raise ValueError('已知欄位重算需指定一至五個欄位及最新補充來源。')
    for field, value in updates.items():
        _label(field)
        if value is not None:
            _label(value)


def _validate_alternatives(alternatives):
    required = {'cases', 'quote'}
    if (not isinstance(alternatives, dict) or not required <= alternatives.keys() <= required | {'track_coverage'}
            or ('track_coverage' in alternatives and alternatives['track_coverage'] is not True)):
        raise ValueError('本輪配對須提供各組條件及同則補充的逐字原話。')
    _label(alternatives['quote'])
    cases = alternatives['cases']
    if not isinstance(cases, list) or not 2 <= len(cases) <= 8:
        raise ValueError('本輪配對需列出二至八組。')
    unique = set()
    for case in cases:
        if not isinstance(case, dict) or not 2 <= len(case) <= 5 or case.keys() != cases[0].keys():
            raise ValueError('本輪各配對須使用相同的二至五個完整欄位。')
        for name, value in case.items():
            _label(name)
            _label(value)
        unique.add(frozenset(case.items()))
    if len(unique) != len(cases):
        raise ValueError('本輪配對不得重複。')


def _validate_update_evidence(evidence, updates, latest_citation):
    if not isinstance(evidence, dict) or evidence.keys() != updates.keys():
        raise ValueError('欄位證據須涵蓋每一個更新欄位。')
    for excerpts in evidence.values():
        if not isinstance(excerpts, list) or not 1 <= len(excerpts) <= 2:
            raise ValueError('每個更新欄位須附一至兩則證據。')
        for excerpt in excerpts:
            if (not isinstance(excerpt, dict) or excerpt.keys() != {'citation', 'quote'}
                    or not isinstance(excerpt['citation'], str) or not _REVISION_SOURCE.fullmatch(excerpt['citation'])):
                raise ValueError('欄位證據須提供已保存的來源編號與逐字短句。')
            _label(excerpt['quote'])
        if excerpts[-1]['citation'] != latest_citation:
            raise ValueError('每個更新欄位的證據須以最新補充作結。')


def _edge_key(edge):
    if isinstance(edge, dict):
        return (edge['higher'], edge['lower']), frozenset(edge['when'].items())
    return tuple(edge), frozenset()


def _precedence_change(base, values):
    if any(not re.fullmatch(r'[1-9][0-9]?', value) for value in values):
        raise ValueError('優先更正須指定關係序號及兩個規則編號。')
    number, higher, lower = map(int, values)
    edges = base.get('precedence', ())
    if number > len(edges):
        raise ValueError('優先更正須對應原核對中已列的關係。')
    old = edges[number - 1]
    pair, _ = _edge_key(old)
    if (higher, lower) not in (pair, pair[::-1]):
        raise ValueError('優先更正只能確認或反轉同一對規則。')
    new = {**old, 'higher': higher, 'lower': lower} if isinstance(old, dict) else [higher, lower]
    return {'kind': 'precedence', 'relation': number, 'before': old, 'after': new}


def _revised_precedence(original, edits):
    edges = list(original)
    for index, change in edits.items():
        edges[index - 1] = change['after']
    retained = {}
    for index, edge in enumerate(edges, 1):
        key = _edge_key(edge)
        prior = retained.get(key)
        if prior is None or (prior in edits and index not in edits):
            retained[key] = index
    order = sorted(retained.values())
    final_indexes = {original_index: index for index, original_index in enumerate(order, 1)}
    for index, change in edits.items():
        kept = retained[_edge_key(change['after'])]
        if kept != index:
            change['retained_relation'] = final_indexes[kept]
    return [edges[index - 1] for index in order]


def _revision_changes(text, base_step, base):
    lines = text.splitlines()
    if text.strip() != text or not 1 <= len(lines) <= 4:
        raise ValueError('正式補充須為一至四行，整批未套用。')
    changes, seen = [], set()
    for number, line in enumerate(lines, 1):
        try:
            parts = line.split('｜')
            if parts[0] != f'核對#{base_step}':
                raise ValueError('須指定本目標最新的決策核對編號。')
            if len(parts) == 4 and parts[1] == '條件':
                field, value = parts[2:]
                if value not in base.get('domains', {}).get(field, ()):
                    raise ValueError('條件更正須使用原核對的宣告欄位及選項。')
                key = 'condition', field
                change = {'kind': 'condition', 'field': field, 'before': base['known'].get(field), 'after': value}
            elif len(parts) == 5 and parts[1] == '優先':
                change = _precedence_change(base, parts[2:])
                key = 'precedence', change['relation']
            else:
                raise ValueError('補充原話未使用條件或優先更正格式。')
            if key in seen:
                raise ValueError('同一欄位或關係不可在同批重複更正。')
            seen.add(key)
            changes.append(change)
        except ValueError as exc:
            if len(lines) == 1:
                raise
            raise ValueError(f'第 {number} 行未通過，整批未套用。{exc}') from exc
    return changes


def _result_summary(result):
    return {key: result[key] for key in ('status', 'possible_results', 'recorded_results') if key in result}


def _current_result(plan, *, require_all=False):
    if 'current_alternatives' not in plan:
        return calculate(plan)
    core = {key: value for key, value in plan.items() if key not in {'current_alternatives', 'current_row_rules'}}
    rules = plan.get('current_row_rules', ())
    result = calculate(core, alternatives=plan['current_alternatives'], require_all=require_all, row_rules=rules)
    if rules:
        result['current_row_rules'] = [{**rule, 'applicable': rule['row'] in result['matching_rows']} for rule in rules]
    return result


def _revision_comparison(base, plan, changes, result):
    changed_fields = {change['field'] for change in changes if change['kind'] == 'condition'}
    comparison = {'before': _result_summary(_current_result(base)), 'after': _result_summary(result),
                  'retained_known': {name: value for name, value in base['known'].items()
                                     if name not in changed_fields}}
    scopes = [dict(_edge_key(change['before'])[1]) for change in changes if change['kind'] == 'precedence']
    fields = [name for name in base['conditions'] if any(name in scope for scope in scopes)]
    if not fields:
        return comparison
    branches = []
    for values in product(*(base['domains'][name] for name in fields)):
        when = dict(zip(fields, values))
        branches.append({'when': when, 'within_revision_scope': any(scope.items() <= when.items() for scope in scopes),
                         'before': _result_summary(_current_result({**base, 'known': {**base['known'], **when}})),
                         'after': _result_summary(_current_result({**plan, 'known': {**plan['known'], **when}}))})
    comparison['scope_comparison'] = branches
    return comparison


def _followup_sources(base_step, sequence, replies, guidance):
    sources = {row['citation']: row for row in replies if base_step < row['ask_sequence'] < sequence}
    sources.update((row['citation'], row) for row in guidance if base_step <= row['steps_seen'] < sequence)
    return sources


def _source_text(source):
    return source.get('answer', source.get('content'))


def _revision_source(citation, base_step, sequence, replies, guidance, rejected, what_if_sources):
    sources = _followup_sources(base_step, sequence, replies, guidance)
    for ignored in what_if_sources:
        sources.pop(ignored, None)
    source = sources.get(citation)
    if source is None:
        raise ValueError('局部重算須引用最新核對後唯一保存的正式補充或更正。')
    text = _source_text(source)
    header, _, body = text.partition('\n')
    if header.startswith('取代｜'):
        replaces = rejected['receipt'] if rejected else None
        prior_steps = source.get('steps_seen', source.get('ask_sequence', 0) - 1)
        if (replaces is None or header.removesuffix('\r') != f"取代｜{replaces['citation']}"
                or sources.keys() - {citation} != rejected['citations']
                or citation in rejected['citations']
                or prior_steps < replaces['step']):
            raise ValueError('取代首行須指向同一原核對最後一次退回的正式補充，原核對保留。')
        return source, body, replaces, sources.keys()
    if len(sources) != 1:
        raise ValueError('局部重算須引用最新核對後唯一保存的正式補充或更正。')
    return source, text, None, sources.keys()


def _revision_plan(base_step, base, text):
    changes = _revision_changes(text, base_step, base)
    plan = dict(base)
    fields = {change['field']: change['after'] for change in changes if change['kind'] == 'condition'}
    edges = {change['relation']: change for change in changes if change['kind'] == 'precedence'}
    if fields:
        plan['known'] = {**base['known'], **fields}
    if edges:
        plan['precedence'] = _revised_precedence(base['precedence'], edges)
    return plan, changes, _current_result(plan)


def _row_revision_plan(base_step, base, text, source):
    parts = text.split('｜')
    if (parts[0] != f'核對#{base_step}' or len(parts) != len(base['conditions']) + 3
            or 'rows' not in base or 'domains' not in base or 'current_alternatives' not in base):
        raise ValueError('本輪規則補充須以最後決策編號及原表欄序明列唯一生效組合。')
    when = _assignment(parts[2:-1], len(base['conditions']))
    result = parts[-1]
    _label(result)
    expected, missing = _pending_rule_assignment(base)
    if when != expected:
        raise ValueError('只可補充目前唯一生效且仍有規則缺口的完整組合。')
    evidence = {'citation': source['citation'], 'source_sha256': source['sha256'], 'quote': text}
    plan, change = _resolve_row_rule(base, when, result, _ROW_REVISION_KINDS[parts[1]], missing, evidence)
    return plan, [change], _current_result(plan)


def _pending_rule_assignment(base):
    invalid = '只可補充目前唯一生效且仍有規則缺口的完整組合。'
    if not {'rows', 'domains', 'current_alternatives'} <= base.keys():
        raise ValueError(invalid)
    observed = _current_result(base)
    rules = base.get('current_row_rules', [])
    matched = set(observed['matching_rows'])
    covered = {tuple(row['when']) for index, row in enumerate(base['rows'], 1) if index in matched}
    covered.update(tuple(rule['when']) for rule in rules if rule['row'] in matched)
    missing = {tuple(row) for row in observed['coverage']['missing_rows']}
    if observed['status'] != 'incomplete_table' or len(covered | missing) != 1:
        raise ValueError(invalid)
    when, = covered | missing
    return when, when in missing


def _resolve_row_rule(base, when, result, kind, missing, evidence):
    rules = base.get('current_row_rules', [])
    original_rows = [index for index, row in enumerate(base['rows'], 1) if tuple(row['when']) == when]
    if kind == 'row_completion':
        if not missing:
            raise ValueError('補規則只適用原表未記載的結果。')
        row = len(base['rows']) + sum(not rule['original_rows'] for rule in rules) + 1
    else:
        chosen = [index for index in original_rows if base['rows'][index - 1]['result'] == result]
        if len(original_rows) < 2 or len(chosen) != 1:
            raise ValueError('選規則須指定同組原有衝突結果之一。')
        row, = chosen
    change = {'kind': kind, 'when': list(when), 'result': result, 'row': row, 'original_rows': original_rows}
    return {**base, 'current_row_rules': [*rules, {**change, **evidence}]}, change


def _apply_revision(args, previous, sequence, replies, guidance, rejected, what_if_sources):
    if previous is None:
        raise ValueError('局部重算需有同一目標已保存的決策核對。')
    base_step, base = previous
    source, text, replaces, citations = _revision_source(args['revision'], base_step, sequence, replies, guidance,
                                                         rejected, what_if_sources)
    try:
        parts = text.split('｜', 2)
        if len(parts) > 1 and parts[1] in _ROW_REVISION_KINDS:
            if replaces is not None or len(text.splitlines()) != 1:
                raise ValueError('本輪規則補充須為一則完整單行原話。')
            plan, changes, result = _row_revision_plan(base_step, base, text, source)
        else:
            plan, changes, result = _revision_plan(base_step, base, text)
        if (replaces is not None or len(changes) > 1) and result['status'] == 'no_match':
            raise ValueError('新條件與宣告的可行情境不相容，整批未套用。')
        if 'current_alternatives' in base and result['status'] == 'no_match':
            raise ValueError('新條件與目前配對不相容，原核對保留。')
    except ValueError as exc:
        if replaces is not None or (text.startswith(f'核對#{base_step}｜') and len(text.splitlines()) > 1):
            raise _RejectedRevision(str(exc), source, set(citations)) from exc
        raise
    receipt = {'base_step': base_step, 'base_sha256': digest(base),
               'citation': source['citation'], 'source_sha256': source['sha256']}
    if len(changes) > 1:
        receipt['changes'] = changes
    else:
        receipt['change'] = changes[0]
    if replaces is not None:
        receipt['replaces'] = replaces
    if args.get('compare'):
        receipt['comparison'] = _revision_comparison(base, plan, changes, result)
    return plan, {**result, 'revision': receipt}


def _updated_plan(base, updates):
    domains = base.get('domains')
    if domains is None:
        domains = dict(zip(base['conditions'], zip(*(row['when'] for row in base['rows']))))
    known = dict(base['known'])
    changes = []
    for field, value in updates.items():
        if field not in domains or (value is not None and value not in domains[field]):
            raise ValueError('已知欄位重算須使用原表欄名及選項。')
        changes.append({'field': field, 'before': known.get(field), 'after': value})
        if value is None:
            known.pop(field, None)
        else:
            known[field] = value
    return {**base, 'known': known}, changes


def _natural_clauses(text):
    return [clause.strip() for clause in re.split(r'[。；\r\n]+', text) if clause.strip()]


def _unadopted_note(source):
    clauses = _natural_clauses(_source_text(source))
    return clauses and all(_UNADOPTED_SENTENCE.fullmatch(clause) for clause in clauses)


def _natural_update_source(citation, base_step, sequence, replies, guidance, what_if_sources):
    sources = _followup_sources(base_step, sequence, replies, guidance)
    latest, _ = _latest_source(citation, sources)
    if any(not _unadopted_note(source) for key, source in sources.items()
           if key != citation and key not in what_if_sources):
        raise ValueError('另有尚未核對的補充，請先確認其影響。')
    return latest


def _natural_condition(base, clause):
    for field, values in base['domains'].items():
        if clause == f'{field}正式撤回':
            return field, None
        match = re.fullmatch(rf'{re.escape(field)}正式(?:改為|確認為|確認是) *(.*)', clause)
        if match:
            value = _unquote_value(match[1])
            if value in values:
                return field, value
    return None


def _natural_row_clauses(base, source):
    updates, result = {}, None
    for clause in _natural_clauses(_source_text(source)):
        if _UNADOPTED_SENTENCE.fullmatch(clause) or clause in {'原配對仍適用', '先前配對仍適用', '其他欄位照舊'}:
            continue
        formal = _ROW_SENTENCE.fullmatch(clause)
        if formal and result is None:
            value = formal['quoted'] or formal['plain']
            _label(value)
            if (value in {'待查', '未定', '尚未公布', '暫定'} or '或' in value
                    or (formal['plain'] and _UNCERTAIN_PLAIN.search(value))):
                raise ValueError('尚未確認的結果不能作為本輪正式規則。')
            result = value, _source_excerpt(source, clause)
            continue
        condition = _natural_condition(base, clause) if result is None else None
        if condition is None or condition[0] in updates:
            raise ValueError('正式規則確認須用獨立完整原句，欄位更正須在結果確認前。')
        field, value = condition
        updates[field] = value, _source_excerpt(source, clause)
    if result is None:
        raise ValueError('這則補充沒有明確正式確認本輪規則。')
    return updates, result


def _apply_natural_row_confirmation(args, previous, sequence, replies, guidance, what_if_sources):
    if previous is None or not {'rows', 'domains', 'current_alternatives'} <= previous[1].keys():
        raise ValueError('自然規則確認須先核對本輪配對及原表。')
    base_step, base = previous
    latest = _natural_update_source(args['row_confirmation'], base_step, sequence, replies, guidance, what_if_sources)
    updates, (result, proof) = _natural_row_clauses(base, latest)
    plan, changes = _updated_plan(base, {field: value for field, (value, _) in updates.items()})
    when, missing = _pending_rule_assignment(plan)
    kind = 'row_completion' if missing else 'row_selection'
    plan, change = _resolve_row_rule(plan, when, result, kind, missing, proof)
    receipt = {'base_step': base_step, 'base_sha256': digest(base), 'citation': latest['citation'],
               'source_sha256': latest['sha256'], 'change': change}
    if changes:
        receipt['condition_changes'] = [{**item, 'evidence': updates[item['field']][1]} for item in changes]
    return plan, {**_current_result(plan), 'revision': receipt}


def _unquote_value(text):
    return text[1:-1] if text.startswith('「') and text.endswith('」') else text


def _reference_value(source):
    clauses = [clause for clause in _natural_clauses(_source_text(source))
               if not _UNADOPTED_SENTENCE.fullmatch(clause) and clause != '其他欄位照舊']
    if len(clauses) != 1 or not (match := _FIELDLESS_CHANGE.fullmatch(clauses[0])):
        raise ValueError('省略欄名的更正須是單一明確改值原句。')
    value = _unquote_value(match['value'])
    _label(value)
    return value, _source_excerpt(source, clauses[0])


def _choose_reference(base, source):
    pending = base.get('pending_field_reference')
    if pending is None:
        raise ValueError('目前沒有待確認的欄位指代。')
    remaining, lines = _compound_reply(pending, source)
    clauses = [lines['field'].strip()] if lines else _natural_clauses(_source_text(source))
    choices = {branch['field'] for branch in pending['branches']}
    field = _unquote_value(clauses[0]) if len(clauses) == 1 else None
    if field is not None and field not in choices:
        field = _unquote_value(re.sub(r'^(?:是|指|確認欄位為) *', '', clauses[0]))
    if field not in choices:
        raise ValueError('請只確認先前候選中的一個完整欄名。')
    plan, result = _apply_reference_choice(base, field)
    proof = {**pending, 'status': 'applied', 'field': field}
    if lines:
        proof['confirmation'] = _reply_line(source, lines, 'field')
        proof['remaining_reply'] = {'question': remaining, **_reply_line(source, lines, 'remaining')}
    else:
        proof['confirmation'] = _source_excerpt(source, clauses[0])
    return plan, {**result, 'field_reference': proof}


def _apply_reference_choice(base, field):
    pending = base['pending_field_reference']
    core = {key: value for key, value in base.items() if key != 'pending_field_reference'}
    plan = _reference_plan(core, field, pending['value'], pending.get('overwritten_fields', ()))
    result = _current_result(plan)
    if result['status'] == 'no_match':
        raise ValueError('所選欄位改值與目前可行情境不相容，請確認原條件。')
    return plan, result


def _reference_plan(base, field, value, overwritten):
    return base if field in overwritten else _updated_plan(base, {field: value})[0]


def _reference_results(base, candidates, value, overwritten=()):
    plans = [_reference_plan(base, field, value, overwritten) for field in candidates]
    results = [_current_result(plan) for plan in plans]
    branches = [{'field': field, **_result_summary(result)} for field, result in zip(candidates, results)]
    if overwritten:
        for branch, plan in zip(branches, plans):
            branch['known'] = plan['known']
    return plans, results, branches


def _pending_reference(base, proof, results):
    agreed = all(result['status'] == 'resolved' and result['possible_results'] == results[0].get('possible_results')
                 for result in results)
    proof = {**proof, 'status': 'agreement' if agreed else 'needs_field'}
    observation = {'status': 'resolved' if agreed else 'needs_check', 'max_questions': 0 if agreed else 1,
                   'field_reference': proof}
    if agreed:
        observation['possible_results'] = results[0]['possible_results']
    return {**base, 'pending_field_reference': proof}, observation


def _recheck_reference(base_step, base, source):
    pending = base.get('pending_field_reference')
    if not pending or 'steps_seen' not in source:
        raise ValueError('重核須有已保存的欄位指代及主動更正原話。')
    clauses = [clause for clause in _natural_clauses(_source_text(source))
               if not _UNADOPTED_SENTENCE.fullmatch(clause)]
    update = _natural_condition(base, clauses[0]) if len(clauses) == 1 else None
    if update is None or update[1] is None:
        raise ValueError('請用一個完整欄名及原表新值明確更正，例如「入口正式改為南口」。')
    return _update_reference(base_step, base, update, _source_excerpt(source, clauses[0]))


def _update_reference(base_step, base, update, evidence):
    pending = base['pending_field_reference']
    field, value = update
    candidates = [branch['field'] for branch in pending['branches']]
    prior_overwritten = pending.get('overwritten_fields', ())
    replaces_reference = field in candidates and field not in prior_overwritten
    if base['known'].get(field) == value and not replaces_reference:
        raise ValueError('新值與已核對的欄位值相同，無須更正。')
    overwritten = [name for name in candidates if name in prior_overwritten or name == field]
    core = {key: item for key, item in base.items() if key != 'pending_field_reference'}
    plan, changes = _updated_plan(core, {field: value})
    _, results, branches = _reference_results(plan, candidates, pending['value'], overwritten)
    if all(result['status'] == 'no_match' for result in results):
        raise ValueError('這個更正與所有原候選分支不相容，請核對原條件。')
    receipt = {'base_step': base_step, 'base_sha256': digest(base), 'change': changes[0],
               'evidence': evidence}
    proof = {**pending, 'branches': branches, 'recheck': receipt}
    if overwritten:
        proof['overwritten_fields'] = overwritten
    if len(overwritten) == len(candidates):
        return plan, {**results[0], 'field_reference': {**proof, 'status': 'superseded'}}
    return _pending_reference(plan, proof, results)


def reference_report(steps, result):
    if (not result or not steps or steps[-1]['action']['tool'] != 'finish'
            or steps[-1]['action']['args'].get('report') != result or steps[-1]['observation'].get('report') != result):
        return None
    latest = next((step for step in reversed(steps) if step['action']['tool'] in ('check_decision', 'check_source_decision')), None)
    if latest and latest['observation'].get('field_reference', {}).get('status') == 'agreement':
        return latest
    return None


def _reference_question(proof):
    fields = '、'.join(f'「{branch["field"]}」' for branch in proof['branches'])
    return f'第一筆改值指哪個欄位？請回完整欄名{fields}。'


def reference_question(step):
    if step['action']['tool'] == 'check_decision':
        proof = step['observation'].get('field_reference', {})
        if proof.get('status') == 'needs_field':
            return _reference_question(proof)
    return None


def _valid_remaining(question):
    return (isinstance(question, str) and 2 <= len(question) <= 300 and question == question.strip()
            and bool(re.fullmatch(r'[^？?\r\n\x00]+[？?]', question)))


def _unmarked_reference_ask(args, checked):
    question = args.get('question', '')
    lines = question.split('\n') if isinstance(question, str) else []
    return (not args.keys() - {'question', 'partial_result'} and len(lines) == 3
            and lines[0] == reference_question(checked) and _valid_remaining(lines[1])
            and lines[2] == _REFERENCE_REPLY_HINT)


def _reference_anchor(steps):
    index = len(steps) - 1
    rejected = []
    while index >= 0 and steps[index]['action']['tool'] == 'invalid':
        step = steps[index]
        observation = step['observation']
        failed = observation.get('failed_action', {})
        if (observation.get('error_phase') != 'execution' or observation.get('error') != _MISSING_REFERENCE_MARKER
                or failed.get('tool') != 'ask' or not isinstance(failed.get('args'), dict)):
            return None
        rejected.append(step)
        index -= 1
    if index < 0 or reference_question(steps[index]) is None:
        return None
    checked = steps[index]
    rejected.reverse()
    if any(step['sequence'] != checked['sequence'] + offset
           or not _unmarked_reference_ask(step['observation']['failed_action']['args'], checked)
           for offset, step in enumerate(rejected, 1)):
        return None
    return checked, rejected


def reference_question_for_steps(steps):
    anchor = _reference_anchor(steps)
    return reference_question(anchor[0]) if anchor else None


def validate_reference_ask(args, steps, replies=(), guidance=()):
    remaining = args['reference_remaining']
    anchor = _reference_anchor(steps)
    original = reference_question(anchor[0]) if anchor else None
    if (not original or not _valid_remaining(remaining) or args.keys() - {'question', 'partial_result', 'reference_remaining'}
            or args.get('question') != '\n'.join((original, remaining, _REFERENCE_REPLY_HINT))):
        raise ValueError('分項提問須緊接欄名核對，並依指定格式另列一個完整的獨立問句。')
    checked, rejected = anchor
    if rejected:
        if (args['question'] != rejected[-1]['observation']['failed_action']['args']['question']
                or _followup_sources(checked['sequence'], steps[-1]['sequence'] + 1, replies, guidance)):
            raise ValueError('原問句或條件已有改動，請重新核對後再建立分項提問。')
        return {'base_step': checked['sequence'], 'base_sha256': digest(checked),
                'failed_steps': [{'sequence': step['sequence'], 'sha256': digest(step)} for step in rejected],
                'question_sha256': digest(args['question'])}
    return None


def planned_reference_ask(args, steps, replies=(), guidance=()):
    if 'reference_remaining' in args:
        return validate_reference_ask(args, steps, replies, guidance)
    anchor = _reference_anchor(steps)
    if anchor and anchor[1] and not _unmarked_reference_ask(args, anchor[0]):
        raise ValueError('分項規劃修正須保持原問句並補齊標記，改問時請先重新核對。')
    if anchor and _unmarked_reference_ask(args, anchor[0]):
        raise ValueError(_MISSING_REFERENCE_MARKER)
    checked = next((step for step in reversed(steps) if step['action']['tool'] != 'invalid'), None)
    if checked and _unmarked_reference_ask(args, checked):
        raise ValueError(_MISSING_REFERENCE_MARKER)
    return None


def _compound_reply(proof, source):
    question = source.get('question', '').split('\n')
    if (len(question) != 3 or question[0] != _reference_question(proof)
            or not _valid_remaining(question[1]) or question[2] != _REFERENCE_REPLY_HINT):
        return None, None
    answer = _source_text(source)
    lines = re.fullmatch(r'(?P<field>[^\r\n]+)\r?\n(?P<remaining>[^\r\n]+)', answer)
    if lines and _unquote_value(lines['field'].strip()) not in {branch['field'] for branch in proof['branches']}:
        labeled = re.fullmatch(r'欄名[：:] *(?P<field>[^\r\n]+)\r?\n補問[：:] *(?P<remaining>[^\r\n]+)', answer)
        lines = labeled or lines
    if not lines or not lines['field'].strip() or not lines['remaining'].strip():
        raise ValueError('請分兩行保存答覆，第一行為完整欄名，第二行回答獨立補問。')
    return question[1], lines


def _reply_line(source, lines, name):
    start, end = lines.span(name)
    return {'citation': source['citation'], 'source_sha256': source['sha256'],
            'start': start, 'end': end, 'quote': lines[name]}


def _waiting_reference(steps):
    if len(steps) < 2:
        return None
    ask = steps[-1]
    anchor = _reference_anchor(steps[:-1])
    question = reference_question(anchor[0]) if anchor else None
    args = ask['action'].get('args', {})
    if (not question or ask['sequence'] != steps[-2]['sequence'] + 1 or ask['action']['tool'] != 'ask'
            or not isinstance(args, dict) or args.get('question') != ask['observation'].get('question')):
        return None
    if 'reference_remaining' in args:
        expected = validate_reference_ask(args, steps[:-1])
        if ask['observation'].get('reference_repair') != expected:
            raise ValueError('分項規劃修正與原核對或失敗紀錄不一致。')
        return ask
    return ask if not anchor[1] and args.get('question') == question else None


def _apply_waiting_recheck(args, previous, sequence, steps, guidance):
    ask = _waiting_reference(steps)
    anchor = _reference_anchor(steps[:-1]) if ask else None
    if not ask or ask['sequence'] != sequence - 1 or previous is None or previous[0] != anchor[0]['sequence']:
        raise ValueError('等待更正須緊接只確認原欄名的提問及分支核對。')
    if any(previous[0] <= source['steps_seen'] < ask['sequence'] and not _unadopted_note(source) for source in guidance):
        raise ValueError('提問前另有尚未核對的補充，請先確認其影響。')
    sources = [source for source in guidance if source['steps_seen'] == ask['sequence']]
    if not sources or sources[-1]['citation'] != args['field_reference']:
        raise ValueError('等待更正須引用目前問題最後保存的主動更正。')
    base_step, plan = previous
    changes = []
    for source in sources:
        plan, observation = _recheck_reference(base_step, plan, source)
        changes.append(observation['field_reference']['recheck'])
    waiting = {'ask_sequence': ask['sequence'], 'changes': changes}
    if 'reference_remaining' in ask['action']['args']:
        waiting['remaining_question'] = ask['action']['args']['reference_remaining']
    proof = {**observation['field_reference'], 'waiting_recheck': waiting}
    if 'pending_field_reference' in plan:
        plan = {**plan, 'pending_field_reference': proof}
    return plan, {**observation, 'field_reference': proof}


def compound_reference_ask(steps):
    ask = _waiting_reference(steps)
    return ask if ask and 'reference_remaining' in ask['action']['args'] else None


def _natural_partial_span(answer, remaining, fields):
    if '\r' in answer or '\n' in answer:
        return None  # Preserve the original two-line route.
    clauses = list(re.finditer(r'[^,，。；;]+', answer))
    deferred = [(index, match['item']) for index, clause in enumerate(clauses)
                if (match := _DEFERRED_REPLY.fullmatch(clause[0].strip()))]
    if not deferred:
        return None
    if len(deferred) != 1 or not _PARTIAL_REPLY_CLAUSES.fullmatch(answer):
        raise ValueError('請先明確回答一項並標明另一項待查，原題及草稿仍保留。')
    index, pending_item = deferred[0]
    answer_clause = clauses[1 - index]
    statement = answer_clause[0].strip()
    offset = answer_clause.start() + len(answer_clause[0]) - len(answer_clause[0].lstrip())
    aliases, formats = next(((aliases, formats) for question, aliases, formats in _NATURAL_REMAINING
                             if question.fullmatch(remaining)), ((), ()))
    if pending_item.endswith('欄名'):
        for pattern in formats:
            if (match := pattern.fullmatch(statement)) and not _UNCERTAIN_REMAINING.search(match['value']):
                start, end = match.span('value')
                return 'remaining', offset + start, offset + end
        raise ValueError('請先確認獨立補問的一個明確答案，原題及草稿仍保留。')
    if pending_item == '補問' or pending_item in aliases:
        if _unquote_value(statement) in fields:
            return 'field', offset, offset + len(statement)
        match = _NAMED_FIELD_REPLY.fullmatch(statement)
        if match and _unquote_value(match['value']) in fields:
            start, end = match.span('value')
            return 'field', offset + start, offset + end
    raise ValueError('請先確認第一筆改值所指的完整欄名及哪一項待查，原題及草稿仍保留。')


def _partial_reply_span(answer, remaining, fields):
    if match := _PARTIAL_REFERENCE_REPLY.fullmatch(answer):
        kind = 'field' if match['label'] == '欄名' else 'remaining'
        return kind, *match.span('value')
    return _natural_partial_span(answer, remaining, fields)


def partial_reply_kind(steps, answer):
    ask = compound_reference_ask(steps)
    if not ask:
        return None
    proof = _reference_anchor(steps[:-1])[0]['observation']['field_reference']
    span = _partial_reply_span(answer, ask['action']['args']['reference_remaining'],
                               {branch['field'] for branch in proof['branches']})
    return span[0] if span else None


def _apply_partial_reply(args, previous, sequence, steps, replies, guidance):
    if previous is None or not steps or steps[-1]['sequence'] != sequence - 1:
        raise ValueError('分項回答須緊接原分項提問或等待重核。')
    base_step, base = previous
    pending = base.get('pending_field_reference')
    last = steps[-1]
    rechecked = last['action']['tool'] == 'check_decision' and last['action']['args'].get('waiting_recheck')
    ask = compound_reference_ask(steps[:-1] if rechecked else steps)
    anchor = _reference_anchor((steps[:-1] if rechecked else steps)[:-1]) if ask else None
    if (not ask or not pending or pending['status'] != 'needs_field'
            or base_step != (last['sequence'] if rechecked else anchor[0]['sequence'])):
        raise ValueError('分項回答須引用仍需要兩項答覆的原提問。')
    if not rechecked and any(base_step <= item['steps_seen'] <= ask['sequence']
                             and not _unadopted_note(item) for item in guidance):
        raise ValueError('原提問另有尚未核對的更正，請先確認其影響。')
    source = next((item for item in replies if item['ask_sequence'] == ask['sequence']), None)
    if not source or args['field_reference'] != source['citation']:
        raise ValueError('分項回答須使用原提問保存的答覆。')
    remaining = ask['action']['args']['reference_remaining']
    fields = {branch['field'] for branch in pending['branches']}
    answer = source['answer']
    span = _partial_reply_span(answer, remaining, fields)
    if not span or not (value := answer[span[1]:span[2]].strip()):
        raise ValueError('請用「欄名：完整欄名」或「補問：你的答案」明確回答一項。')
    kind, start, end = span
    field_question = _reference_question(pending)
    receipt = {'citation': source['citation'], 'source_sha256': source['sha256'],
               'start': start, 'end': end, 'quote': answer[start:end]}
    if kind == 'field':
        # Existing prefixed receipts keep their exact unquoted-label behavior.
        if not _PARTIAL_REFERENCE_REPLY.fullmatch(answer):
            value = _unquote_value(value)
        if value not in fields:
            raise ValueError('請回覆原題候選中的完整欄名。')
        plan, result = _apply_reference_choice(base, value)
        partial = {**receipt, 'item': 'field', 'question': field_question, 'next_question': remaining}
        proof = {**pending, 'status': 'applied', 'field': value, 'confirmation': receipt, 'partial_reply': partial}
        return plan, {**result, 'field_reference': proof}
    partial = {**receipt, 'item': 'remaining', 'question': remaining, 'next_question': field_question}
    proof = {**pending, 'partial_reply': partial}
    return {**base, 'pending_field_reference': proof}, {'status': 'needs_check', 'max_questions': 1, 'field_reference': proof}


def _answered_waiting(steps):
    if len(steps) < 2:
        return None
    checked, ask = steps[-2:]
    if (checked['action']['tool'] != 'check_decision' or not checked['action']['args'].get('partial_reply')
            or ask['action']['tool'] != 'ask' or ask['sequence'] != checked['sequence'] + 1):
        return None
    proof = checked['observation']['field_reference']
    question = proof['partial_reply']['next_question']
    if ask['action']['args'] == {'question': question} and ask['observation'].get('question') == question:
        return proof, ask
    return None


def _answered_target(target, partial):
    if partial['item'] == 'field':
        return target in ('已答欄名', '第一筆欄名', '欄名')
    return target == '已答補問' or any(question.fullmatch(partial['question']) and target in aliases
                                    for question, aliases, _ in _NATURAL_REMAINING)


def _answered_change(content, partial):
    match = _ANSWERED_CHANGE.fullmatch(content)
    context = []
    complete = True
    if match is None:
        offset = 0
        for token in _ANSWERED_NOTE_TOKEN.finditer(content):
            if not _ANSWERED_SEPARATORS.fullmatch(content[offset:token.start()]):
                complete = False
                break
            if token['correction'] is not None:
                if match is not None:
                    complete = False
                    break
                match = token
            else:
                context.append(token)
            offset = token.end()
        complete = complete and bool(_ANSWERED_SEPARATORS.fullmatch(content[offset:]))
    if match is None:
        return None
    if _answered_target(match['target'], partial):
        return {'match': match, 'context': context, 'complete': complete}
    return None


def answered_revision_requested(steps, content, replies=(), guidance=()):
    waiting = _answered_waiting(steps)
    if not waiting:
        return False
    if _answered_change(content, waiting[0]['partial_reply']):
        return True
    original = _recheck_saved(steps[:-2], replies, guidance)[0][1]
    parts = _combined_parts(content, waiting[0], original, waiting=True)
    return bool(parts and parts[0] is not None)


def answered_revision_question(steps):
    return _answered_waiting(steps)[0]['partial_reply']['question']


def _deferred_target(item, partial):
    aliases = next((aliases for question, aliases, _ in _NATURAL_REMAINING
                    if question.fullmatch(partial['next_question'])), ())
    return item.endswith('欄名') if partial['item'] == 'remaining' else item == '補問' or item in aliases


def _answered_context(parsed, partial, source):
    if not parsed['complete']:
        raise ValueError('更正還含未核對的內容，請分開說明已答項的新值及其他補充。')
    retained = []
    for token in parsed['context']:
        kind = 'proposal' if token['proposal'] is not None else 'deferred'
        if kind == 'deferred' and not _deferred_target(token['item'], partial):
            raise ValueError('待查文字須對應原本尚未回答的問題。')
        retained.append({'kind': kind, **_reply_line(source, token, kind)})
    return retained


def _answered_value(match, proof):
    partial = proof['partial_reply']
    raw = match['value'].strip()
    value = _unquote_value(raw)
    if partial['item'] == 'field':
        candidates = {row['field'] for row in proof['branches']}
        if raw in candidates:
            return raw
        if value in candidates:
            return value
        raise ValueError('更正欄名須是原表候選中的一個完整欄名。')
    for question, aliases, formats in _NATURAL_REMAINING:
        if question.fullmatch(partial['question']):
            if any(pattern.fullmatch(f'{aliases[0]}為{raw}') for pattern in formats) and not _UNCERTAIN_REMAINING.search(raw):
                return value
            raise ValueError('已答補問須更正為與原問題相符的一個明確值。')
    if raw != value and not _UNCERTAIN_REMAINING.search(value):
        _label(value)
        return value
    raise ValueError('請以引號標明已答補問的新值。')


def _apply_answered_revision(args, previous, sequence, steps, replies, guidance):
    waiting = _answered_waiting(steps)
    if not waiting or previous is None or previous[0] != sequence - 2 or waiting[1]['sequence'] != sequence - 1:
        raise ValueError('已答項更正須緊接原分項紀錄及其待答問句。')
    proof, ask = waiting
    partial = proof['partial_reply']
    sources = [item for item in guidance if item['steps_seen'] == ask['sequence'] and not _unadopted_note(item)]
    if not sources or sources[-1]['citation'] != args['field_reference']:
        raise ValueError('已答項更正須引用目前問題最後保存的正式更正。')
    base_step, original_plan = _recheck_saved(steps[:-2], replies, guidance)[0]
    parts = [_combined_parts(source['content'], proof, original_plan, waiting=True) for source in sources]
    if any(part and part[2] for part in parts) and any(part and part[0] is not None for part in parts):
        return _apply_answered_conditions(proof, ask, base_step, original_plan, sources, parts)
    value = proof['field'] if partial['item'] == 'field' else _unquote_value(partial['quote'])
    original = _recheck_saved(steps[:-2], replies, guidance)[0][1] if partial['item'] == 'field' else None
    changes = []
    for source in sources:
        parsed = _answered_change(source['content'], partial)
        if not parsed:
            raise ValueError('另有尚未核對的補充，請先確認其影響。')
        retained = _answered_context(parsed, partial, source)
        match = parsed['match']
        if partial['item'] == 'remaining' and match['target'] in previous[1]['conditions']:
            raise ValueError('這個名稱也在決策表中，請明寫「已答補問正式改為」以指定已答項。')
        new_value = _answered_value(match, proof)
        if value == new_value:
            raise ValueError('新值與目前已答值相同，無須更正。')
        change = {'before': value, 'after': new_value, 'evidence': _reply_line(source, match, 'value')}
        if retained:
            change['retained'] = retained
        changes.append(change)
        value = new_value
        if original is not None:
            plan, result = _apply_reference_choice(original, value)
    revision = {'ask_sequence': ask['sequence'], 'item': partial['item'], 'question': partial['question'],
                'value': value, 'changes': changes}
    proof = {**proof, 'answered_revision': revision}
    if original is not None:
        return plan, {**result, 'field_reference': {**proof, 'field': value}}
    return {**previous[1], 'pending_field_reference': proof}, {
        'status': 'needs_check', 'max_questions': 1, 'field_reference': proof}


def _apply_answered_conditions(proof, ask, base_step, plan, sources, parts):
    partial = proof['partial_reply']
    value = proof['field'] if partial['item'] == 'field' else _unquote_value(partial['quote'])
    changes, condition_changes, retained_notes = [], [], []
    for source, parsed in zip(sources, parts, strict=True):
        if parsed is None:
            raise ValueError('另有尚未核對的補充，請先確認其影響。')
        correction, _, updates, retained = parsed
        kept = []
        for kind, token in retained:
            if kind == 'deferred' and not _deferred_target(token['item'], partial):
                raise ValueError('待查文字須對應原本尚未回答的問題。')
            kept.append({'kind': kind, **_reply_line(source, token, 0)})
        if correction is not None:
            if partial['item'] == 'remaining' and correction['target'] in plan['conditions']:
                raise ValueError('這個名稱也在決策表中，請明寫已答補問的新值。')
            new_value = _answered_value(correction, proof)
            if value == new_value:
                raise ValueError('新值與目前已答值相同，無須更正。')
            change = {'before': value, 'after': new_value, 'evidence': _reply_line(source, correction, 'value')}
            if kept:
                change['retained'] = kept
            changes.append(change)
            value = new_value
        else:
            retained_notes.extend(kept)
        for token, update in updates:
            plan, checked = _update_reference(base_step, plan, update, _reply_line(source, token, 'clause'))
            pending = checked['field_reference']
            condition_changes.append(pending['recheck'])
            plan = {**plan, 'pending_field_reference': pending}
    revision = {'ask_sequence': ask['sequence'], 'item': partial['item'], 'question': partial['question'],
                'value': value, 'changes': changes, 'condition_changes': condition_changes,
                'source': sources[-1]['citation']}
    if retained_notes:
        revision['retained_notes'] = retained_notes
    pending = {**plan['pending_field_reference'], 'partial_reply': partial, 'answered_revision': revision}
    if partial['item'] == 'field':
        current, result = _apply_reference_choice(plan, value)
        return current, {**result, 'field_reference': {**pending, 'status': 'applied', 'field': value}}
    if pending['status'] in ('agreement', 'superseded'):
        revision['withdraws_question'] = True
    if pending['status'] == 'superseded':
        current = {key: item for key, item in plan.items() if key != 'pending_field_reference'}
        return current, {**_current_result(current), 'field_reference': pending}
    current = {key: item for key, item in plan.items() if key != 'pending_field_reference'}
    _, results, _ = _reference_results(current, [branch['field'] for branch in pending['branches']],
                                       pending['value'], pending.get('overwritten_fields', ()))
    return _pending_reference(current, pending, results)


def answered_reference_review(steps, replies, guidance):
    waiting = _answered_waiting(steps)
    if not waiting:
        return None
    proof, ask = waiting
    source = next((item for item in reversed(guidance) if item['steps_seen'] == ask['sequence']
                   and not _unadopted_note(item)), None)
    if source is None:
        return None
    previous, _, _ = _recheck_saved(steps, replies, guidance)
    try:
        args = {'field_reference': source['citation'], 'answered_revision': True}
        return _apply_answered_revision(args, previous, ask['sequence'] + 1, steps, replies, guidance)[1]
    except ValueError:
        return None


def describe_answered_review(review):
    revision = review['field_reference']['answered_revision']
    source = revision['changes'][-1]['evidence']['citation']
    results = '、'.join(review.get('possible_results', ()))
    result = f'目前核對結果為{results}。' if results else ''
    if revision.get('condition_changes') and review['field_reference']['status'] == 'needs_field':
        result = '、'.join(f"原先指{row['field']}時為{'／'.join(row.get('possible_results') or ['待查'])}"
                          for row in review['field_reference']['branches']) + '。'
    if revision.get('withdraws_question'):
        return f'已依 {source} 更正已答項為「{revision["value"]}」。{result}原欄名已不影響結果，可接續交付。'
    return f'已依 {source} 更正已答項為「{revision["value"]}」。{result}原待答問題仍保留。'


def _combined_answer(content, start, end, proof):
    partial = proof['partial_reply']
    if partial['item'] == 'remaining':
        match = _NAMED_FIELD_REPLY.fullmatch(content, start, end)
        fields = {branch['field'] for branch in proof['branches']}
        return match if match and _unquote_value(match['value']) in fields else None
    formats = next((formats for question, _, formats in _NATURAL_REMAINING
                    if question.fullmatch(partial['next_question'])), ())
    return next((match for pattern in formats if (match := pattern.fullmatch(content, start, end))
                 and not _UNCERTAIN_REMAINING.search(match['value'])), None)


def _combined_parts(content, proof, base, *, waiting=False):
    offset = 0
    corrections, answers, updates, retained, unknown = [], [], [], [], []
    while offset < len(content):
        offset = _ANSWERED_SEPARATORS.match(content, offset).end()
        if offset == len(content):
            break
        proposal = _UNADOPTED_SENTENCE.match(content, offset)
        correction = _COMBINED_CORRECTION.match(content, offset) if proposal is None else None
        deferred = _DEFERRED_REPLY.match(content, offset) if waiting and proposal is None else None
        if proposal:
            retained.append(('proposal', proposal))
            offset = proposal.end()
        elif correction and _answered_target(correction['target'], proof['partial_reply']):
            corrections.append(correction)
            offset = correction.end()
        elif deferred:
            retained.append(('deferred', deferred))
            offset = deferred.end()
        else:
            clause = _COMBINED_CLAUSE.match(content, offset)
            if clause is None:
                unknown.append(content[offset:])
                break
            update = _natural_condition(base, clause['clause'].strip())
            end = clause.end() - len(clause['clause']) + len(clause['clause'].rstrip())
            answer = _combined_answer(content, clause.start(), end, proof)
            if update and update[1] is not None:
                updates.append((clause, update))
            elif answer:
                answers.append(answer)
            else:
                unknown.append(clause['clause'])
            offset = clause.end()
    if waiting:
        if not corrections and not updates:
            return None
        if len(corrections) > 1 or answers or unknown:
            raise ValueError('請明確更正已答項及原表完整欄位，待查文字只對應原待答問題。'
                             '已知道剩餘答案時請使用回答目前問題，其他內容先分開確認。')
    elif not corrections:
        return None
    elif len(corrections) != 1 or len(answers) != 1 or unknown:
        partial = proof['partial_reply']
        choices = '、'.join(branch['field'] for branch in proof['branches'])
        hint = f'請確認一個完整候選欄名（{choices}）。' if partial['item'] == 'remaining' else '請給一個明確的補問答案。'
        raise ValueError(f'請先回答原待答問題「{partial["next_question"]}」。{hint}'
                         '同則只更正已答項一次，其他未核對內容請先分開確認。原問題及草稿仍保留。')
    if len({update[0] for _, update in updates}) != len(updates):
        raise ValueError('同則正式欄位更正不能重複指定同一欄，請先確認一個新值。')
    return (corrections[0] if corrections else None), (answers[0] if answers else None), updates, retained


def combined_reply_requested(steps, answer, replies=(), guidance=()):
    waiting = _answered_waiting(steps)
    if not waiting:
        return False
    original = _recheck_saved(steps[:-2], replies, guidance)[0][1]
    return _combined_parts(answer, waiting[0], original) is not None


def _apply_combined_reply(args, previous, sequence, steps, replies, guidance):
    waiting = _answered_waiting(steps)
    if not waiting or previous is None or previous[0] != sequence - 2 or waiting[1]['sequence'] != sequence - 1:
        raise ValueError('同則更正與答覆須緊接原分項紀錄及剩餘問題。')
    proof, ask = waiting
    source = next((item for item in replies if item['ask_sequence'] == ask['sequence']), None)
    if not source or source['citation'] != args['field_reference'] or source['question'] != ask['action']['args']['question']:
        raise ValueError('同則更正與答覆須引用原問題保存的真實答覆。')
    base_step, plan = _recheck_saved(steps[:-2], replies, guidance)[0]
    parts = _combined_parts(source['answer'], proof, plan)
    if parts is None:
        raise ValueError('此答覆沒有明確更正原先已答的項目。')
    correction, answer, updates, retained = parts
    partial = proof['partial_reply']
    changes = []
    condition_changes = []
    prior = [item for item in guidance if item['steps_seen'] == ask['sequence'] and not _unadopted_note(item)]
    value = proof['field'] if partial['item'] == 'field' else _unquote_value(partial['quote'])
    if prior:
        _, checked = _apply_answered_revision({'field_reference': prior[-1]['citation']}, previous, sequence, steps, replies, guidance)
        revision = checked['field_reference']['answered_revision']
        changes = revision['changes']
        value = revision['value']
        for update in revision.get('condition_changes', ()):
            plan, checked = _update_reference(base_step, plan, (update['change']['field'], update['change']['after']),
                                               update['evidence'])
            pending = checked['field_reference']
            condition_changes.append(pending['recheck'])
            plan = {**plan, 'pending_field_reference': pending}
    if partial['item'] == 'remaining' and correction['target'] in plan['conditions']:
        raise ValueError('這個名稱也在決策表中，請明寫「已答補問改為」以指定已答項。')
    new_value = _answered_value(correction, proof)
    change = {'before': value, 'after': new_value, 'evidence': _reply_line(source, correction, 'value')}
    if retained:
        change['retained'] = [{'kind': kind, **_reply_line(source, token, 0)} for kind, token in retained]
    changes = [*changes, change]
    for token, update in updates:
        plan, checked = _update_reference(base_step, plan, update, _reply_line(source, token, 'clause'))
        pending = checked['field_reference']
        condition_changes.append(pending['recheck'])
        plan = {**plan, 'pending_field_reference': pending}
    remaining = {'question': partial['next_question'], 'value': _unquote_value(answer['value']),
                 **_reply_line(source, answer, 'value')}
    field = new_value if partial['item'] == 'field' else remaining['value']
    current, result = _apply_reference_choice(plan, field)
    revision = {'ask_sequence': ask['sequence'], 'item': partial['item'], 'question': partial['question'],
                'value': new_value, 'changes': changes}
    combined = {'ask_sequence': ask['sequence'], 'remaining_reply': remaining, 'condition_changes': condition_changes}
    proof = {**plan['pending_field_reference'], 'status': 'applied', 'field': field, 'partial_reply': partial,
             'answered_revision': revision, 'combined_reply': combined,
             'confirmation': change['evidence'] if partial['item'] == 'field' else _reply_line(source, answer, 'value')}
    return current, {**result, 'field_reference': proof}


def waiting_reference_review(steps, replies, guidance):
    ask = _waiting_reference(steps)
    if not ask:
        return None
    source = next((item for item in reversed(guidance) if item['steps_seen'] == ask['sequence']), None)
    if source is None:
        return None
    previous, _, _ = _recheck_saved(steps, replies, guidance)
    args = {'field_reference': source['citation'], 'waiting_recheck': True}
    try:
        return _apply_waiting_recheck(args, previous, ask['sequence'] + 1, steps, guidance)[1]
    except ValueError:
        return None  # Mixed or unsupported notes stay on the ordinary guidance path.


def describe_waiting_review(review):
    proof = review['field_reference']
    source = proof['recheck']['evidence']['citation']
    if proof['status'] != 'needs_field':
        if proof['waiting_recheck'].get('remaining_question'):
            return f'依 {source} 重核後，原欄名已不影響目前結果，獨立補問仍需回答。'
        return f'依 {source} 重核後，原欄名已不影響目前結果，可依該更正撤下原問題。'
    branches = '、'.join(f"原先指{row['field']}時為{'／'.join(row.get('possible_results') or ['待查'])}"
                        for row in proof['branches'])
    return f'依 {source} 重核後，{branches}。原欄名仍待確認。'


def _validate_recheck_position(args, steps, sequence, guidance):
    if (not steps or steps[-1]['sequence'] != sequence - 1
            or not reference_report(steps, steps[-1]['observation'].get('report'))
            or not any(source['citation'] == args['field_reference'] and source['steps_seen'] == sequence - 1
                       for source in guidance)):
        raise ValueError('成果更正須緊接已交付的共同結果，並引用當時保存的主動更正。')


def _apply_field_reference(args, previous, sequence, replies, guidance, what_if_sources):
    if previous is None or 'domains' not in previous[1]:
        raise ValueError('欄位指代核對須先有完整選項的原決策表。')
    base_step, base = previous
    citation = args['field_reference']
    pending = base.get('pending_field_reference', {})
    waiting = pending.get('waiting_recheck', {})
    partial = pending.get('partial_reply', {})
    carried_ask = pending.get('answered_revision', {}).get('ask_sequence', waiting.get('ask_sequence'))
    if args.get('resolve') and partial.get('item') == 'remaining' and citation == partial['citation']:
        raise ValueError('這筆答覆只對應獨立補問。')
    if args.get('resolve') and carried_ask is not None and citation == f'[補充#{carried_ask}]':
        carried = next((item for item in replies if item['ask_sequence'] == carried_ask), None)
        if carried is None or _followup_sources(base_step, sequence, replies, guidance):
            raise ValueError('須使用重核後仍適用的原問題答覆。')
        return _choose_reference(base, carried)
    if args.get('resolve') and partial.get('item') == 'remaining':
        guidance = tuple(item for item in guidance if not _unadopted_note(item))
    source = _natural_update_source(citation, base_step, sequence, replies, guidance, what_if_sources)
    if args.get('resolve'):
        if (waiting.get('remaining_question') and source.get('ask_sequence') == waiting['ask_sequence'] + 2
                and source['question'] == waiting['remaining_question']):
            raise ValueError('這筆答覆只對應保留的獨立補問。')
        return _choose_reference(base, source)
    if args.get('recheck'):
        return _recheck_reference(base_step, base, source)
    if 'pending_field_reference' in base:
        raise ValueError('先確認已保存的候選欄位。')
    value, evidence = _reference_value(source)
    candidates = [field for field in base['conditions']
                  if value in base['domains'][field] and base['known'].get(field) != value]
    if not candidates:
        raise ValueError('更正值找不到可改的原表欄位。')
    plans, results, branches = _reference_results(base, candidates, value)
    proof = {'base_step': base_step, 'base_sha256': digest(base), 'value': value, **evidence,
             'branches': branches}
    if len(candidates) == 1:
        if results[0]['status'] == 'no_match':
            raise ValueError('更正值與目前可行情境不相容。')
        return plans[0], {**results[0], 'field_reference': {**proof, 'status': 'applied', 'field': candidates[0]}}
    return _pending_reference(base, proof, results)


def _bind_alternatives(alternatives, plan, source):
    cases, quote = alternatives['cases'], alternatives['quote']
    if not cases[0].keys() <= set(plan['conditions']):
        raise ValueError('本輪配對須使用原表欄位。')
    if not _PAIR_LIMIT.search(quote) or _SCENARIO_START.match(quote) or quote.startswith(('「', '『', '引用', '引述')):
        raise ValueError('本輪配對須引用明確限制目前可能組合的原話。')
    return {**alternatives, **_source_excerpt(source, quote)}


def _apply_known_update(args, previous, sequence, replies, guidance):
    if previous is None:
        raise ValueError('已知欄位重算需有同一目標已保存的決策核對。')
    base_step, base = previous
    sources = _followup_sources(base_step, sequence, replies, guidance)
    source, positions = _latest_source(args['source'], sources)
    if _source_text(source).startswith(('核對#', '取代｜')):
        raise ValueError('正式更正須以原話的正式格式重算。')
    plan, changes = _updated_plan(base, args['known_update'])
    if 'alternatives' in args:
        plan['current_alternatives'] = _bind_alternatives(args['alternatives'], plan, source)
        plan.pop('current_row_rules', None)
    result = _current_result(plan, require_all='alternatives' in args)
    receipt = {'base_step': base_step, 'base_sha256': digest(base), 'citation': args['source'],
               'source_sha256': source['sha256'], 'changes': changes}
    if 'evidence' in args:
        receipt['evidence'] = _bind_update_evidence(args['evidence'], sources, positions)
    if args.get('what_if'):
        return base, {**_current_result(base), 'known_update': receipt, 'what_if_result': result}
    if result['status'] == 'no_match':
        raise ValueError('更新值與原表的可行情境不相容，原核對保留。')
    if 'scenario' in args:
        scenario = args['scenario']
        trial_evidence = _bind_update_evidence(scenario['evidence'], sources, positions)
        _separate_scenario_evidence(receipt['evidence'], trial_evidence)
        trial_plan, trial_changes = _updated_plan(plan, scenario['updates'])
        result = {**result, 'what_if_result': _current_result(trial_plan),
                  'what_if_update': {'changes': trial_changes, 'evidence': trial_evidence}}
    return plan, {**result, 'known_update': receipt}


def _latest_source(citation, sources):
    positions = {key: source.get('steps_seen', source.get('ask_sequence')) for key, source in sources.items()}
    latest = max(positions.values(), default=-1)
    if [key for key, position in positions.items() if position == latest] != [citation]:
        raise ValueError('已知欄位重算須引用本次最新且可辨認的補充。')
    return sources[citation], positions


def _separate_scenario_evidence(official, trial):
    official = [excerpts[-1] for excerpts in official.values()]
    trial = [excerpts[-1] for excerpts in trial.values()]
    if any(_SCENARIO_START.match(row['quote']) for row in official):
        raise ValueError('正式更正須引用獨立正式原話，不可引用試算短句。')
    if any(not _SCENARIO_START.match(row['quote']) for row in trial):
        raise ValueError('試算短句須從原文的假設或試算標記開始。')
    if any(left['start'] < right['end'] and right['start'] < left['end'] for left in official for right in trial):
        raise ValueError('正式更新與試算須引用同一則補充中互不重疊的原話。')


def _bind_update_evidence(evidence, sources, positions):
    bound = {}
    for field, excerpts in evidence.items():
        previous = -1
        bound[field] = []
        for excerpt in excerpts:
            citation, quote = excerpt['citation'], excerpt['quote']
            position = positions.get(citation, -1)
            if position <= previous:
                raise ValueError('欄位證據須依序引用原核對後的同一目標補充。')
            bound[field].append(_source_excerpt(sources[citation], quote))
            previous = position
    return bound


def _source_excerpt(source, quote):
    text = _source_text(source)
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0 or text.startswith(('核對#', '取代｜')):
        raise ValueError('欄位證據須逐字引用自然補充中可唯一定位的短句。')
    return {'citation': source['citation'], 'quote': quote, 'source_sha256': source['sha256'],
            'start': start, 'end': start + len(quote)}


def failed_revision(step):
    observation = step['observation']
    failed = observation.get('failed_action')
    if (step['action'].get('tool') != 'invalid' or observation.get('error_phase') != 'execution'
            or not isinstance(failed, dict) or failed.get('tool') != 'check_decision'):
        return None
    args = failed.get('args')
    return args if isinstance(args, dict) and 'revision' in args else None


def _verified_rejection(step, previous, replies, guidance, rejected, what_if_sources):
    args = failed_revision(step)
    if args is None:
        return rejected
    try:
        validate_tool(args)
        _apply_revision(args, previous, step['sequence'], replies, guidance, rejected, what_if_sources)
    except _RejectedRevision as exc:
        if step['observation'].get('error') == str(exc):
            return {'citations': exc.citations, 'receipt': {'step': step['sequence'],
                    'citation': exc.source['citation'], 'source_sha256': exc.source['sha256'], 'error': str(exc)}}
    except ValueError:
        pass
    return rejected


def _apply_transition(args, previous, sequence, replies, guidance, rejected, what_if_sources, steps):
    if args.get('combined_reply'):
        return _apply_combined_reply(args, previous, sequence, steps, replies, guidance)
    if args.get('answered_revision'):
        return _apply_answered_revision(args, previous, sequence, steps, replies, guidance)
    if args.get('partial_reply'):
        return _apply_partial_reply(args, previous, sequence, steps, replies, guidance)
    if args.get('waiting_recheck'):
        return _apply_waiting_recheck(args, previous, sequence, steps, guidance)
    if 'field_reference' in args:
        return _apply_field_reference(args, previous, sequence, replies, guidance, what_if_sources)
    if not _TRANSITION_FIELDS & args.keys():
        return args, calculate(args)
    if previous is not None and 'pending_field_reference' in previous[1]:
        raise ValueError('已保存的欄位指代仍待確認，請先確認原候選欄名。')
    if 'known_update' in args:
        return _apply_known_update(args, previous, sequence, replies, guidance)
    if 'row_confirmation' in args:
        return _apply_natural_row_confirmation(args, previous, sequence, replies, guidance, what_if_sources)
    return _apply_revision(args, previous, sequence, replies, guidance, rejected, what_if_sources)


def _recheck_saved(steps, replies, guidance):
    previous = rejected = None
    what_if_sources = set()
    for index, step in enumerate(steps):
        action = step['action']
        ask_args = action.get('args')
        if action.get('tool') == 'ask' and isinstance(ask_args, dict) and 'reference_remaining' in ask_args:
            if (not index or steps[index-1]['sequence'] != step['sequence'] - 1
                    or ask_args.get('question') != step['observation'].get('question')):
                raise ValueError('保存的分項提問與原欄名核對不一致。')
            expected = validate_reference_ask(ask_args, steps[:index], replies, guidance)
            if step['observation'].get('reference_repair') != expected:
                raise ValueError('保存的分項規劃修正與原核對或失敗紀錄不一致。')
        if action.get('tool') == 'invalid':
            rejected = _verified_rejection(step, previous, replies, guidance, rejected, what_if_sources)
        if action.get('tool') != 'check_decision':
            continue
        args, sequence = action['args'], step['sequence']
        validate_tool(args)
        if args.get('recheck'):
            _validate_recheck_position(args, steps[:index], sequence, guidance)
        plan, expected = _apply_transition(args, previous, sequence, replies, guidance, rejected, what_if_sources, steps[:index])
        actual = {key: value for key, value in step['observation'].items() if key not in _STEP_METADATA}
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError('決策表結果與保存的可行情境不一致。')
        if args.get('what_if'):
            what_if_sources.add(args['source'])
            continue
        previous = sequence, plan
        rejected = None
        what_if_sources.clear()
    return previous, rejected, what_if_sources


def observe(args, steps, replies=(), guidance=()):
    if not _TRANSITION_FIELDS & args.keys():
        return calculate(args)
    validate_tool(args)
    previous, rejected, what_if_sources = _recheck_saved(steps, replies, guidance)
    sequence = len(steps) + 1
    if args.get('recheck'):
        _validate_recheck_position(args, steps, sequence, guidance)
    return _apply_transition(args, previous, sequence, replies, guidance, rejected, what_if_sources, steps)[1]


def _validate_followup_question(by_sequence, step, question, status, current, count):
    successor = by_sequence.get(step['sequence'] + 1)
    if (not successor or successor['action']['tool'] != 'ask' or successor['action']['args'] != {'question': question}
            or successor['observation'].get('question') != question):
        raise ValueError('原分項提問缺少相符的保留補問。')
    if status == 'needs_input' and successor['sequence'] == count and current != question:
        raise ValueError('目前等待的問題與已保存的保留補問不一致。')


def _validate_waiting_reply(db, gid, ask, source, replies_present):
    reply = db.execute('SELECT notes_start FROM autonomous_reply_sources WHERE goal_id=? AND ask_sequence=?',
                       (gid, ask)).fetchone() if replies_present else None
    end = source['notes_start'] + len(goal_guidance_sources.note(source['content'], ask, source['recorded_at']))
    if reply is None or reply[0] < end:
        raise ValueError('原問題答覆須保存在等待期間的更正之後。')


def validate_saved(db, *, replies_present=False, guidance_present=False):
    for gid, notes, status, question in db.execute(
            "SELECT DISTINCT g.id,g.notes,g.status,g.question FROM autonomous_goals g JOIN autonomous_steps s ON g.id=s.goal_id "
            "WHERE s.action_json LIKE '%\"check_decision\"%'"):
        steps = [{'sequence': sequence, 'action': json.loads(action), 'observation': json.loads(observation)}
                 for sequence, action, observation in db.execute(
                     "SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? "
                     "AND (action_json LIKE '%\"check_decision\"%' OR action_json LIKE '%\"invalid\"%' "
                     "OR action_json LIKE '%\"finish\"%' OR action_json LIKE '%\"check_source_decision\"%' "
                     "OR action_json LIKE '%\"ask\"%') "
                     "ORDER BY sequence", (gid,))]
        revised = any(step['action'].get('tool') == 'check_decision'
                      and step['action']['args'].keys() & _TRANSITION_FIELDS for step in steps)
        replies = goal_reply_sources.read(db, gid, notes) if revised and replies_present else []
        count = db.execute('SELECT COUNT(*) FROM autonomous_steps WHERE goal_id=?', (gid,)).fetchone()[0] if revised else 0
        guidance = []
        if revised and guidance_present:
            guidance = goal_guidance_sources.read(db, gid, notes, count)
        _recheck_saved(steps, replies, guidance)
        by_sequence = {step['sequence']: step for step in steps}
        for step in steps:
            if step['action']['tool'] != 'check_decision':
                continue
            args = step['action']['args']
            if args.get('partial_reply'):
                remaining = step['observation']['field_reference']['partial_reply']['next_question']
                _validate_followup_question(by_sequence, step, remaining, status, question, count)
            if args.get('answered_revision'):
                proof = step['observation']['field_reference']['answered_revision']
                source = next(item for item in guidance if item['citation'] == args['field_reference'])
                if proof.get('withdraws_question'):
                    row = db.execute('SELECT guidance_number,guidance_sha256 FROM autonomous_question_withdrawals '
                                     'WHERE goal_id=? AND ask_sequence=?', (gid, proof['ask_sequence'])).fetchone()
                    if row != (source['number'], source['sha256']):
                        raise ValueError('已答項與條件重核缺少相符的原問題撤下紀錄。')
                else:
                    _validate_waiting_reply(db, gid, proof['ask_sequence'], source, replies_present)
            if args.get('combined_reply'):
                ask = step['observation']['field_reference']['combined_reply']['ask_sequence']
                prior = [source for source in guidance if source['steps_seen'] == ask]
                if prior:
                    _validate_waiting_reply(db, gid, ask, prior[-1], replies_present)
            if not args.get('waiting_recheck'):
                continue
            proof = step['observation']['field_reference']
            ask = proof['waiting_recheck']['ask_sequence']
            source = next(item for item in guidance if item['citation'] == step['action']['args']['field_reference'])
            if proof['status'] == 'needs_field':
                _validate_waiting_reply(db, gid, ask, source, replies_present)
            else:
                has_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_question_withdrawals'").fetchone()
                withdrawn = db.execute('SELECT guidance_number,guidance_sha256 FROM autonomous_question_withdrawals '
                                       'WHERE goal_id=? AND ask_sequence=?', (gid, ask)).fetchone() if has_table else None
                if withdrawn != (source['number'], source['sha256']):
                    raise ValueError('等待重核缺少與更正相符的原提問撤下紀錄。')
                if remaining := proof['waiting_recheck'].get('remaining_question'):
                    _validate_followup_question(by_sequence, step, remaining, status, question, count)
