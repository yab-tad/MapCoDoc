"""Exhaustive per-member fidelity auditor.

Loads EVERY structured(.json)+preprocessed(.txt) pair from the collected_fidelity
tree for a library/source and computes grounding metrics that the span-level
fidelity evaluator cannot surface - in particular whether the preprocessed doc is
actually ABOUT the member (wrong-member / contamination hidden inside 'clean').

Usage:  python audit_pairs.py <library> <source>
Outputs: tests/<library>_<source>_audit.csv  + prints members needing manual read.
"""
import csv, json, os, re, sys
from rapidfuzz import fuzz

COLLECT_BASE = os.environ.get("MAPCODOC_COLLECT_BASE", "collected_fidelity")
TESTS = os.path.dirname(__file__)

_PLACE = re.compile(r"\(?url_placeholder_\d+\)?", re.I)
_ALNUM = re.compile(r"[^a-z0-9]+")

def norm(s: str) -> str:
    s = (s or "").lower()
    s = _PLACE.sub(" ", s)
    return _ALNUM.sub("", s)

def norm_kept_space(s: str) -> str:
    s = (s or "").lower()
    s = _PLACE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

TRIVIAL = {"", "n/a", "na", "none"}

def leaves(obj):
    out = []
    if isinstance(obj, str):
        if obj.strip().lower() not in TRIVIAL:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(leaves(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(leaves(v))
    return out

def is_empty(j):
    sig = (j.get("module_member_signature") or "").strip().lower()
    if sig not in TRIVIAL:
        return False
    return len(leaves({k: v for k, v in j.items() if k != "module_member_signature"})) == 0

def short_name(stem):
    base = re.sub(r"-(function|class)$", "", stem)
    return base.split(".")[-1]

def audit(library, source):
    root = os.path.join(COLLECT_BASE, library, source)
    rows = []
    for bucket in sorted(os.listdir(root)):
        bdir = os.path.join(root, bucket)
        if not os.path.isdir(bdir):
            continue
        for fn in os.listdir(bdir):
            if not fn.endswith(".json"):
                continue
            stem = fn[:-5]
            jpath = os.path.join(bdir, fn)
            tpath = os.path.join(bdir, stem + ".txt")
            try:
                j = json.load(open(jpath, encoding="utf-8"))
            except Exception as e:
                rows.append(dict(stem=stem, bucket=bucket, flag="json_error", detail=str(e)))
                continue
            doc = open(tpath, encoding="utf-8").read() if os.path.exists(tpath) else ""
            doc_n, doc_ns = norm(doc), norm_kept_space(doc)

            empty = is_empty(j)
            nm = short_name(stem)
            nm_n = norm(nm)
            name_present = bool(nm_n) and (nm_n in doc_n or fuzz.partial_ratio(nm_n, doc_n) >= 90)

            purpose = ((j.get("module_member_description") or {}).get("purpose") or "")
            p_ns = norm_kept_space(purpose)[:300]
            purpose_grounded = (not p_ns) or (len(p_ns) < 8) or fuzz.partial_ratio(p_ns, doc_ns) >= 80

            lv = [l for l in leaves(j) if len(norm(l)) >= 12]
            grounded = 0
            for l in lv:
                ln = norm(l)[:400]
                if ln in doc_n or fuzz.partial_ratio(ln, doc_n) >= 85:
                    grounded += 1
            ratio = round(grounded / len(lv), 3) if lv else 1.0

            flags = []
            if empty:
                flags.append("empty")
            if not name_present:
                flags.append("name_absent")
            if not purpose_grounded:
                flags.append("purpose_ungrounded")
            if ratio < 0.6:
                flags.append(f"low_ground({ratio})")
            if not doc.strip():
                flags.append("no_preprocessed")

            rows.append(dict(stem=stem, bucket=bucket,
                             empty=empty, name_present=name_present,
                             purpose_grounded=purpose_grounded, ground_ratio=ratio,
                             n_fields=len(lv), doc_lines=doc.count("\n") + 1 if doc else 0,
                             flag="|".join(flags), purpose_head=norm_kept_space(purpose)[:70]))
    return rows

def main():
    library, source = sys.argv[1], sys.argv[2]
    rows = audit(library, source)
    out = os.path.join(TESTS, f"{library}_{source}_audit.csv")
    cols = ["stem","bucket","empty","name_present","purpose_grounded","ground_ratio","n_fields","doc_lines","flag","purpose_head"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    flagged = [r for r in rows if r.get("flag")]
    print(f"{library}/{source}: {len(rows)} members audited, {len(flagged)} need manual read")
    from collections import Counter
    fc = Counter(f for r in flagged for f in r["flag"].split("|"))
    print("flag counts:", dict(fc))
    print("\nFLAGGED (read these):")
    for r in sorted(flagged, key=lambda x: (x["bucket"], x["stem"])):
        print(f"  [{r['bucket']}] {r['stem']}  -> {r['flag']}  | purpose: {r.get('purpose_head','')}")

if __name__ == "__main__":
    main()
