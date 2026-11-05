"""Exact scales for the versioned document conversion receipt."""
from fractions import Fraction


# BIPM SI Brochure, tables 7 and 8. Scales use grams, metres, litres, and seconds.
_GROUPS = {
    'mass': ((1_000_000, 't 公噸'), (1000, 'kg 公斤'), (1, 'g 克 公克'), ('1/1000', 'mg 毫克')),
    'length': ((1000, 'km 公里 千米'), (1, 'm 公尺 米'), ('1/10', 'dm 分米'),
               ('1/100', 'cm 公分 厘米'), ('1/1000', 'mm 公釐 毫米')),
    'volume': ((1, 'L l 公升 升'), ('1/1000', 'mL ml 毫升')),
    'duration': ((3600, 'h 小時'), (60, 'min 分鐘'), (1, 's 秒')),
}
SCALES = {unit: (dimension, Fraction(scale)) for dimension, scales in _GROUPS.items()
          for scale, aliases in scales for unit in aliases.split()}
