import os, sys
os.environ.setdefault("BOT_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot as b

cases = [
    "salı 18-21 müsaitim", "Hafta içi akşam", "cmt 10'dan sonra", "paz 14'e kadar",
    "Salı ve Perşembe 18:00-21:00", "çarşamba müsait değilim", "cumartesi", "pazartesi 9-12, pazar öğleden sonra",
    "18-21", "salı 18-21 ve 14-16", "Cuma akşam, cumartesi tüm gün", "pzt 19dan 22ye",
]
for c in cases:
    mode, slots = b.parse_tr(c)
    print(f"{c!r:45} -> {mode} | {b.fmt_schedule(slots)}")

s = {(1, 18), (1, 19), (6, 21), (0, 9)}
code = b.encode(s)
assert b.decode(code) == s, (code, b.decode(code))
print("encode/decode ok:", code, len(code))
assert b.encode(set()) == "0" * len(code)
print("heatmap:\n" + b.heatmap([(1, "A"), (2, "B"), (3, "C")], [s, s, {(1, 18), (0, 9)}]))
print("next:", b.next_lesson_dt(1, 19))
