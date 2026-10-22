"""The words the speech matcher (``speech_match.py``) understands, as data.

Every word becomes a *concept*: ``A:`` an action, ``J:`` a modifier (colour,
size, ...), ``N:`` a thing, ``R:`` a relation word ("same", "from"), ``T`` the
separator between two steps ("then"), and the two veto concepts ``NEG``
("don't") and ``STOP`` ("halt"). English and Chinese surface forms that name
the same concept are interchangeable, which is how a Chinese sentence matches an
English allowed prompt. Plain inflections ("plates", "stacked") are handled by
the matcher, not listed here.

Three tables are *guesses about speech recognition mistakes*, not vocabulary:
``EN_CONFUSIONS``, ``ZH_HOMOPHONES`` and ``ZH_COLOR_SOUNDALIKES``. A word
repaired through them is shown to the person as a correction and keeps the
result out of the pre-selected tier A.

The tables are built once at import from literals (no files, no network). A
surface form listed under two concepts raises ``ValueError`` at import, so a
lexicon edit that contradicts itself fails every test at once.
"""

from __future__ import annotations

Rows = tuple[tuple[str, tuple[str, ...]], ...]


def _table(rows: Rows) -> dict[str, str]:
    table: dict[str, str] = {}
    for concept, forms in rows:
        for form in forms:
            if table.get(form, concept) != concept:
                raise ValueError(
                    f"lexicon: {form!r} is listed under {table[form]} and {concept}"
                )
            table[form] = concept
    return table


# fmt: off
# ---------------------------------------------------------------- English ----
EN_CONCEPTS: dict[str, str] = _table((
    ("A:MOVE", ("pick", "put", "place", "take", "grab", "move", "bring", "set", "lift", "carry", "fetch", "transfer", "drop", "get", "pickup", "sort", "arrange")),
    ("A:STACK", ("stack", "pile")),
    ("A:POUR", ("pour",)),
    ("A:ASSEMBLE", ("assemble", "mount")),
    ("A:INSERT", ("insert",)),
    ("A:TIDY", ("tidy", "clean up", "organize", "organise")),
    ("T", ("then", "afterwards", "next", "after that", "after", "later", "followed by")),
    ("R:SAME", ("same", "identical", "matching", "equal")),
    ("R:DIFF", ("different", "differing", "distinct", "mismatched", "unlike")),
    ("R:COLOR", ("color", "colour", "colored", "coloured")),
    ("R:SIZE", ("size",)),
    ("R:TOGETHER", ("together",)),
    ("R:FROM", ("from", "out of", "off", "away from")),
    ("R:AWAY", ("away",)),
    ("R:OVER", ("over",)),
    ("R:CORR", ("corresponding",)),
    ("J:BLUE", ("blue",)),
    ("J:RED", ("red",)),
    ("J:WHITE", ("white",)),
    ("J:GREEN", ("green",)),
    ("J:BROWN", ("brown",)),
    ("J:PINK", ("pink",)),
    ("J:BIG", ("big", "bigger", "large", "larger")),
    ("J:SMALL", ("small", "smaller", "little")),
    ("J:EMPTY", ("empty",)),
    ("J:UPSIDE", ("upside down",)),
    ("J:CURRENT", ("current",)),
    ("J:ANOTHER", ("another",)),
    ("N:PLATE", ("plate", "plates", "dish", "saucer")),
    ("N:CUP", ("cup", "mug")),
    ("N:BOWL", ("bowl",)),
    ("N:BASKET", ("basket",)),
    ("N:DRAWER", ("drawer",)),
    ("N:BREAD", ("bread",)),
    ("N:BOX", ("box",)),
    ("N:SLOT", ("slot",)),
    ("N:SCREW", ("screw",)),
    ("N:EGGPLANT", ("eggplant", "aubergine")),
    ("N:TOMATO", ("tomato",)),
    ("N:CARROT", ("carrot",)),
    ("N:APPLE", ("apple",)),
    ("N:ORANGE", ("orange",)),
    ("N:PEACH", ("peach",)),
    ("N:CUCUMBER", ("cucumber",)),
    ("N:MUSHROOM", ("mushroom",)),
    ("N:KIWI", ("kiwifruit", "kiwi", "kiwi fruit")),
    ("N:POTATO", ("potato",)),
    ("N:PEPPER", ("pepper",)),
    ("N:CROISSANT", ("croissant",)),
    ("N:TENNIS", ("tennis ball",)),
    ("N:FORK", ("fork",)),
    ("N:SPOON", ("spoon",)),
    ("N:KNIFE", ("knife",)),
    ("N:GLUE", ("glue",)),
    ("N:CHARGER", ("charger",)),
    ("N:MEDBOTTLE", ("medicine bottle",)),
    ("N:PLASBOTTLE", ("plastic bottle",)),
    ("N:MARKER", ("mark pen", "marker", "markers")),
    ("N:PIKACHU", ("pikachu",)),
    ("N:PLUSH", ("plush toy", "plush")),
    ("N:TUBE", ("tube", "test tube")),
    ("N:WATER", ("water",)),
    ("N:PIPE", ("pipe",)),
    ("N:BASE", ("base",)),
    # Words that cancel or halt: the matcher never offers a task for them.
    ("NEG", ("dont", "don't", "do not", "never", "not", "cancel", "without", "no", "nope", "didn't", "doesn't", "won't", "shouldn't", "can't", "cannot")),
    ("STOP", ("stop", "halt", "wait", "pause", "freeze", "abort", "emergency", "danger", "dangerous", "help", "slow", "estop", "e stop", "careful", "hold on", "hold it", "whoa", "quit", "enough", "cut it out")),
))

# Words that carry no task content. A word of an allowed prompt that is neither
# here nor in EN_CONCEPTS is "unknown" to the matcher (invariant I1).
FILLERS_EN: frozenset[str] = frozenset("""
    a an the it its them they this that these those please robot hey hi hello
    okay ok um uh er erm hmm mm so like just now let lets let's could can would
    will you me my your i to for of with by at as is are be been do does did and
    or but also first up down in into on onto inside upon top
    thank thanks thanx yeah yes right well actually maybe kindly go ahead try
    again here there one ones thing things task want need wanna gonna should may
    might must have has had all every each both some any really very quite what
    which who where how when if because i'd i'm i'll i've it's that's we'll
    we're you'd you'll you're he's she's there's""".split())  # noqa: SIM905

# Likely recognition mistakes for a lexicon word: heard form -> lexicon form.
# Apply only to a token that is neither a lexicon word nor a filler.
EN_CONFUSIONS: dict[str, str] = {
    "blew": "blue", "blu": "blue", "bloo": "blue", "read": "red",
    "blades": "plates", "blade": "plate", "plait": "plate", "plays": "plates", "play": "plate",
    "collar": "color", "screens": "screws", "poor": "pour", "pore": "pour", "stock": "stack",
}

# ---------------------------------------------------------------- Chinese ----
ZH_CONCEPTS: dict[str, str] = _table((
    ("A:MOVE", ("放到", "放进", "放入", "放在", "放", "拿起", "拿", "抓起", "抓取", "抓", "捡起", "捡", "夹起", "夹", "取", "搬", "挪", "移动", "递", "运")),
    ("A:STACK", ("叠", "摞", "堆", "垛", "码")),
    ("A:POUR", ("倒入", "倒进", "倒")),
    ("A:ASSEMBLE", ("组装", "装配", "安装")),
    ("A:INSERT", ("插入", "插")),
    ("A:TIDY", ("整理", "收拾")),
    ("T", ("然后", "接着", "之后", "随后", "接下来", "再")),
    ("R:SAME", ("相同", "同样", "一样", "相等", "同")),
    ("R:DIFF", ("不同", "不一样", "不一致")),
    ("R:COLOR", ("颜色", "色")),
    ("R:SIZE", ("大小", "尺寸")),
    ("R:TOGETHER", ("在一起", "一起")),
    ("R:FROM", ("从",)),
    ("R:AWAY", ("远离", "离开")),
    ("R:OVER", ("越过", "跨过")),
    ("R:CORR", ("对应", "相应")),
    ("J:BLUE", ("蓝色", "蓝")),
    ("J:RED", ("红色", "红")),
    ("J:WHITE", ("白色", "白")),
    ("J:GREEN", ("绿色", "绿")),
    ("J:BROWN", ("棕色", "褐色", "咖啡色", "棕")),
    ("J:PINK", ("粉红色", "粉红", "粉色", "粉")),
    ("J:BIG", ("大",)),
    ("J:SMALL", ("小",)),
    ("J:EMPTY", ("空的", "空")),
    ("J:UPSIDE", ("倒扣", "倒置")),
    ("J:CURRENT", ("当前", "目前")),
    ("J:ANOTHER", ("另一个", "另外一个", "另一")),
    ("N:PLATE", ("盘子", "盘", "碟子", "碟")),
    ("N:CUP", ("杯子", "水杯", "杯")),
    ("N:BOWL", ("碗",)),
    ("N:BASKET", ("篮子", "筐")),
    ("N:DRAWER", ("抽屉",)),
    ("N:BREAD", ("面包",)),
    ("N:BOX", ("盒子", "盒")),
    ("N:SLOT", ("插槽", "空位", "槽位", "槽")),
    ("N:SCREW", ("螺丝", "螺钉")),
    ("N:EGGPLANT", ("茄子",)),
    ("N:TOMATO", ("西红柿", "番茄")),
    ("N:CARROT", ("胡萝卜", "萝卜")),
    ("N:APPLE", ("苹果",)),
    ("N:ORANGE", ("橙子", "橘子", "桔子")),
    ("N:PEACH", ("桃子",)),
    ("N:CUCUMBER", ("黄瓜",)),
    ("N:MUSHROOM", ("蘑菇",)),
    ("N:KIWI", ("猕猴桃",)),
    ("N:POTATO", ("土豆", "马铃薯")),
    ("N:PEPPER", ("辣椒", "青椒")),
    ("N:CROISSANT", ("羊角包", "牛角包", "可颂")),
    ("N:TENNIS", ("网球",)),
    ("N:FORK", ("叉子",)),
    ("N:SPOON", ("勺子", "汤匙")),
    ("N:KNIFE", ("刀子", "刀")),
    ("N:GLUE", ("胶水",)),
    ("N:CHARGER", ("充电器",)),
    ("N:MEDBOTTLE", ("药瓶",)),
    ("N:PLASBOTTLE", ("塑料瓶",)),
    ("N:MARKER", ("马克笔", "记号笔")),
    ("N:PIKACHU", ("皮卡丘",)),
    ("N:PLUSH", ("毛绒玩具", "玩偶")),
    ("N:TUBE", ("试管",)),
    ("N:WATER", ("水",)),
    ("N:PIPE", ("管子", "管道")),
    ("N:BASE", ("底座",)),
    ("NEG", ("不要", "不用", "无需", "别", "不", "没", "勿", "莫", "甭", "取消", "禁止", "严禁")),
    ("STOP", ("停止", "停下", "停", "暂停", "紧急", "急停", "危险", "救命", "住手", "等一下", "等等", "等", "慢点", "慢", "小心", "够了", "别动", "别再动", "别乱动", "不要动", "不许动")),
))

# Single characters with no task content (particles, pronouns, polite words).
FILLERS_ZH: frozenset[str] = frozenset(
    "把将请帮我的了吧啊呢嘛呀嗯呃哦那个这个好现在谢一下到在里中内上面进入起来去给让用能可以吗就先首点"
    "这那它们他她你您么什麻烦全部所有都，。！？、；：,.!?;:"
)

# Characters read the same (toneless pinyin) as the others in their group, so a
# recogniser may write one for another: "茄籽" for "茄子". Used only to derive
# repair spellings of two-character-or-longer noun and colour words.
ZH_HOMOPHONES: tuple[str, ...] = (
    "子籽紫仔字自姿资", "盘磐盼判叛", "碟叠蝶谍迭", "杯悲碑背贝北倍", "篮蓝兰栏拦",
    "抽愁丑臭", "屉提替题", "面棉免", "包宝保报饱", "盒合和河何核", "叉插茶察查",
    "槽草操", "空孔恐控", "位为未味", "螺罗萝锣逻", "丝思司私死", "钉订定顶",
    "茄切且", "西希析息", "红洪宏虹", "柿是市事式试", "番翻凡烦", "胡湖壶户",
    "卜博播", "苹平瓶评屏", "果过国锅", "橙成城程", "橘桔举巨句具", "桃淘逃讨",
    "黄皇慌", "瓜刮挂", "蘑磨摩末", "菇孤姑估", "猕迷米秘", "猴后候厚", "土图突",
    "豆斗逗", "马妈麻码", "铃零灵领", "薯数书树", "辣拉啦", "椒角叫教胶交",
    "青轻清请情", "羊洋样", "牛扭", "可克刻客", "颂送松", "网王往", "球求秋丘",
    "勺少烧", "汤糖堂", "匙吃迟持", "刀倒到道导", "水谁睡", "充冲虫", "电店点",
    "器气起七", "药要摇", "塑素速", "料聊", "笔比必毕", "记几机己", "号好",
    "皮批", "毛猫帽", "绒容", "玩碗晚万", "偶藕", "管官观关", "底地第", "座做坐作",
    "色涩", "白百摆", "绿律率", "棕综宗", "粉分纷", "扣口", "置至制治",
)

# One-character colour words are too short for the groups above (the other
# characters of a group are everyday words); these are the safe soundalikes.
ZH_COLOR_SOUNDALIKES: dict[str, str] = {
    "兰": "蓝", "拦": "蓝", "栏": "蓝", "洪": "红", "宏": "红", "虹": "红",
    "综": "棕", "宗": "棕", "纷": "粉", "律": "绿", "滤": "绿",
}

# Traditional -> simplified for the characters the tables above use (pairs of
# characters: traditional first). Not a full converter; extend it with the
# lexicon.
_T2S_PAIRS = (
    "進进撿捡夾夹動动遞递運运疊叠碼码組组裝装後后隨随來来樣样顏颜從从藍蓝紅红綠绿盤盘"
    "籃篮屜屉麵面麪面絲丝釘钉蘿萝蔔卜蘋苹黃黄獼猕馬马鈴铃頌颂網网湯汤膠胶電电藥药筆笔"
    "記记號号絨绒試试無无別别沒没暫暂緊紧險险點点夠够這这請请幫帮將将個个現现謝谢裡里"
    "裏里內内給给讓让嗎吗麼么煩烦對对開开關关著着當当應应"
)
TRADITIONAL_TO_SIMPLIFIED = str.maketrans(
    {_T2S_PAIRS[i]: _T2S_PAIRS[i + 1] for i in range(0, len(_T2S_PAIRS), 2)}
)

# How a concept is named in the "said / expected" lines of a result, where the
# lower-cased tail of the concept name is not the natural word.
LABELS: dict[str, str] = {
    "J:UPSIDE": "upside down", "R:FROM": "from", "R:CORR": "corresponding", "R:DIFF": "different",
    "N:KIWI": "kiwifruit", "N:TENNIS": "tennis ball", "N:MEDBOTTLE": "medicine bottle",
    "N:PLASBOTTLE": "plastic bottle", "N:MARKER": "marker", "N:PLUSH": "plush toy",
    "N:TUBE": "test tube", "A:MOVE": "pick/put/place",
}
# fmt: on

COLOR_MODIFIERS = frozenset(
    {"J:BLUE", "J:RED", "J:WHITE", "J:GREEN", "J:BROWN", "J:PINK"}
)
SIZE_MODIFIERS = frozenset({"J:BIG", "J:SMALL"})


def label(concept: str) -> str:
    """The word to show for ``concept`` (``N:EGGPLANT`` -> ``eggplant``)."""
    return LABELS.get(concept) or concept.split(":")[-1].lower()


def zh_repair_spellings() -> dict[str, tuple[str, str]]:
    """Repair spelling -> (concept, lexicon spelling) for Chinese.

    Each noun or colour key of two or more characters is respelled with one
    character swapped for a homophone; a one-character colour key gets the
    soundalikes of ``ZH_COLOR_SOUNDALIKES``. A respelling that is itself a
    lexicon key, or that two different concepts would claim, is dropped.
    """
    group_of: dict[str, str] = {}
    for group in ZH_HOMOPHONES:
        for ch in group:
            group_of.setdefault(ch, group)
    claimed: dict[str, set[tuple[str, str]]] = {}
    for key, concept in ZH_CONCEPTS.items():
        if concept[0] not in "NJ":
            continue
        if len(key) == 1:
            for soundalike, target in ZH_COLOR_SOUNDALIKES.items():
                if target == key:
                    claimed.setdefault(soundalike, set()).add((concept, key))
            continue
        for at, ch in enumerate(key):
            for other in group_of.get(ch, ""):
                if other != ch:
                    variant = key[:at] + other + key[at + 1 :]
                    claimed.setdefault(variant, set()).add((concept, key))
    return {
        variant: min(owners)
        for variant, owners in claimed.items()
        if variant not in ZH_CONCEPTS and len({c for c, _ in owners}) == 1
    }
