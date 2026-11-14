"""Finding FASTQ files in a folder and grouping them into samples, for the bulk and single-cell pages.

scan(target, folder) lists *.fastq.gz / *.fq.gz up to a few levels deep on the server that will run the job (so the
check is made from where the files are read). group_bulk() pairs reads (R1/R2, _1/_2) and lanes into samples with an
editable name; guess_group() proposes RNA-seq groups (the name without its replicate number); find_controls() pairs
ChIP-seq samples with an input / IgG control. group_10x() groups Cell Ranger FASTQs (<prefix>_S1_L001_R1_001.fastq.gz)
by prefix and folder. Standard library only.
"""

import os
import re
import shlex
from collections import OrderedDict

FASTQ = re.compile(r"\.f(?:ast)?q\.gz$", re.I)
ILLUMINA = re.compile(r"^(?P<prefix>.+?)_S\d+(?:_L(?P<lane>\d{3}))?_(?P<read>[RI][1-3])_\d{3}\.f(?:ast)?q\.gz$", re.I)
GENERIC = re.compile(r"^(?P<prefix>.+?)(?:[._-]L(?P<lane>\d{1,3}))?[._-](?P<read>R?[12])(?:_\d{3})?\.f(?:ast)?q\.gz$", re.I)
CONTROL = re.compile(r"(?:^|[_\-. ])(input|inp|igg|ctrl|control|mock|wce)(?=[_\-. \d]|$)", re.I)
REPLICATE = re.compile(r"[_\-. ]*(?:rep(?:licate)?|bio|r|n)?[_\-. ]*\d+$", re.I)
SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(text, fallback="sample"):
    name = SAFE.sub("_", str(text)).strip("._-")
    return name[:80] or fallback


def scan(target, folder, depth=4, limit=20000):
    """[{path, size}] of the FASTQ files under folder, as the target server sees them."""
    command = ("d={0}; [ -d \"$d\" ] || {{ echo 'NOFOLDER'; exit 0; }}; [ -r \"$d\" ] && [ -x \"$d\" ] || {{ echo 'NOACCESS'; exit 0; }}; "
               "find -L \"$d\" -maxdepth {1} -type f \\( -iname '*.fastq.gz' -o -iname '*.fq.gz' \\) -printf '%s\\t%p\\n' 2>/dev/null "
               "| head -n {2}").format(shlex.quote(folder.rstrip("/") or "/"), int(depth), int(limit))
    code, out, err = target.run(command, timeout=90)
    if out.startswith("NOFOLDER"):
        raise FileNotFoundError("{} does not exist on {}".format(folder, target.server["name"]))
    if out.startswith("NOACCESS"):
        raise PermissionError("{} cannot read {}".format(target.describe(), folder))
    files = []
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        if path and size.isdigit():
            files.append({"path": path, "size": int(size)})
    files.sort(key=lambda f: f["path"])
    return files


def parse_name(name):
    """(prefix, lane, read) of a FASTQ file name; read is R1/R2/I1/I2/R3 or "" for single-end."""
    m = ILLUMINA.match(name)
    if m:
        return m["prefix"], m["lane"] or "", m["read"].upper()
    m = GENERIC.match(name)
    if m:
        read = m["read"].upper()
        return m["prefix"], m["lane"] or "", read if read.startswith("R") else "R" + read
    return FASTQ.sub("", name), "", ""


def group_bulk(files):
    """Samples from FASTQ files: [{id, name, dir, r1: [paths], r2: [paths], paired, size, lanes}]."""
    groups = OrderedDict()
    for f in files:
        folder, name = os.path.split(f["path"])
        prefix, lane, read = parse_name(name)
        if read.startswith("I"):
            continue  # index reads are not used by the bulk pipelines
        g = groups.setdefault((folder, prefix), {"dir": folder, "prefix": prefix, "R1": [], "R2": [], "SE": [], "size": 0})
        g["R2" if read in ("R2", "R3") else ("R1" if read == "R1" else "SE")].append(f["path"])
        g["size"] += f["size"]
    # one folder per sample (Novogene raw_data/<sample>/...): files of several flowcells/lanes join under the folder name
    by_dir = OrderedDict()
    for g in groups.values():
        by_dir.setdefault(g["dir"], []).append(g)
    samples = []
    for folder, items in by_dir.items():
        base = os.path.basename(folder)
        if len(items) > 1 and base and all(g["prefix"].startswith(base) for g in items):
            merged = {"dir": folder, "prefix": base, "R1": [], "R2": [], "SE": [], "size": 0}
            for g in items:
                for k in ("R1", "R2", "SE"):
                    merged[k] += g[k]
                merged["size"] += g["size"]
            items = [merged]
        for g in items:
            r1 = sorted(g["R1"] or g["SE"])
            r2 = sorted(g["R2"])
            name = g["prefix"]
            if base and g["prefix"].startswith(base) and len(base) >= 3 and len(items) == 1 and len(g["prefix"]) > len(base) + 12:
                name = base  # long provider names (sample_library_flowcell): the folder is the sample
            samples.append({"prefix": g["prefix"], "name": safe_name(name), "dir": folder, "r1": r1, "r2": r2,
                            "paired": bool(r2) and len(r1) == len(r2), "size": g["size"], "lanes": len(r1),
                            "problem": None if (not r2 or len(r1) == len(r2)) else "R1 and R2 file counts differ"})
    # unique names
    seen = {}
    for s in samples:
        n = s["name"]
        if n in seen:
            seen[n] += 1
            s["name"] = "{}_{}".format(n, seen[n])
        else:
            seen[n] = 1
    for i, s in enumerate(samples):
        s["id"] = "s{}".format(i + 1)
    return samples


def guess_group(name):
    """RNA-seq group from a sample name: without its replicate number (WT_1 -> WT, KO-rep2 -> KO)."""
    g = REPLICATE.sub("", name).strip("._- ")
    return safe_name(g if g else name)


def is_control(name):
    return bool(CONTROL.search(name))


def tokens(name):
    return [t.lower() for t in re.split(r"[_\-. ]+", name) if t and not CONTROL.fullmatch("_" + t)]


def find_controls(samples):
    """{treatment name: control name or ""}: each treatment gets the control sharing most name parts (and the same
    replicate number); a lone control is used for every treatment."""
    controls = [s["name"] for s in samples if is_control(s["name"])]
    result = {}
    for s in samples:
        if s["name"] in controls:
            continue
        if not controls:
            result[s["name"]] = ""
            continue
        mine = tokens(s["name"])
        rep = re.search(r"(\d+)$", s["name"])
        def score(c):
            theirs = tokens(c)
            shared = len(set(mine) & set(theirs))
            crep = re.search(r"(\d+)$", c)
            return shared + (0.5 if rep and crep and rep.group(1) == crep.group(1) else 0)
        best = max(controls, key=score)
        result[s["name"]] = best if (len(controls) == 1 or score(best) > 0) else ""
    return result


def group_10x(files):
    """Cell Ranger samples: [{id, prefix, name, dirs: [folders], reads: sorted read types, size, kind_guess}]."""
    groups = OrderedDict()
    for f in files:
        folder, name = os.path.split(f["path"])
        m = ILLUMINA.match(name)
        if not m:
            continue
        g = groups.setdefault(m["prefix"], {"prefix": m["prefix"], "dirs": [], "reads": set(), "size": 0, "files": 0})
        if folder not in g["dirs"]:
            g["dirs"].append(folder)
        g["reads"].add(m["read"].upper())
        g["size"] += f["size"]
        g["files"] += 1
    out = []
    for i, g in enumerate(groups.values()):
        reads = sorted(g["reads"])
        text = (g["prefix"] + " " + " ".join(g["dirs"])).lower()
        # ATAC libraries have R3 (or I2 + R1/R2/R3); GEX R1/R2; the name often says
        if re.search(r"atac|arc_a|chromatin", text) or "R3" in g["reads"]:
            kind = "atac"
        elif re.search(r"gex|rna|_ge_|cdna", text):
            kind = "gex"
        else:
            kind = "gex"
        out.append({"id": "x{}".format(i + 1), "prefix": g["prefix"], "name": safe_name(g["prefix"]), "dirs": g["dirs"],
                    "reads": reads, "size": g["size"], "files": g["files"], "kind": kind})
    return out


def pair_multiome(samples):
    """[(gex sample, atac sample)] matched by name once library words (GEX, ATAC, RNA, ARC) are removed."""
    def base(s):
        return re.sub(r"[_\-.]*(gex|atac|rna|arc|ge|ca|chromatin|expression)(?=[_\-.]|$)", "", s["prefix"], flags=re.I).strip("_-.")

    def key(s):
        return base(s).lower()
    gex = [s for s in samples if s["kind"] == "gex"]
    atac = [s for s in samples if s["kind"] == "atac"]
    pairs, used = [], set()
    for g in gex:
        match = next((a for a in atac if a["id"] not in used and key(a) == key(g)), None)
        if match:
            used.add(match["id"])
        pairs.append({"gex": g["id"], "atac": match["id"] if match else "", "name": safe_name(base(g) or g["prefix"])})
    return pairs
