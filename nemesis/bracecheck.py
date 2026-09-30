import re, sys

for path in sys.argv[1:]:
    src = open(path, encoding="utf-8", errors="replace").read()
    s = re.sub(r"//[^\n]*", "", src)                      # line comments
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)           # block comments
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)             # string literals
    s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)             # char literals
    ob, cb, op, cp = s.count("{"), s.count("}"), s.count("("), s.count(")")
    ok = "OK " if (ob == cb and op == cp) else "BAD"
    print(f"{ok} {path}: braces {ob}/{cb}  parens {op}/{cp}")
